"""Tenant-scoped persistence for onboarding brand research."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from engagemate.onboarding.brand import MAX_REFERENCE_ACCOUNTS
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


def _text(value: object) -> str:
    """Decode a text column that may arrive as bytes.

    The pool's row factory returns bytes for text columns on this driver, and a
    caller comparing that to a str gets a silent mismatch rather than an error.
    """

    if isinstance(value, bytes):
        return value.decode()
    return "" if value is None else str(value)


def _reference_evidence(scraped: Any) -> dict[str, Any]:
    """Keep a bounded, non-executable record of what the scrape showed."""

    profile = scraped if isinstance(scraped, dict) else {}
    return {
        "biography": str(profile.get("biography") or "")[:500],
        "followers_count": profile.get("followersCount") or profile.get("followers"),
        "posts_count": profile.get("postsCount"),
        "is_verified": profile.get("isVerified"),
        "is_private": profile.get("isPrivate"),
    }


def _candidate_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    """Keep bounded scoring evidence and the sources that found a candidate."""

    profile = candidate.get("profile")
    profile = profile if isinstance(profile, dict) else {}
    raw_sources = candidate.get("sources")
    sources = raw_sources if isinstance(raw_sources, list) else []
    return {
        "reasons": candidate.get("reasons", []),
        "sources": sorted(
            {
                str(source).strip().lower()[:64]
                for source in sources[:MAX_REFERENCE_ACCOUNTS]
                if str(source).strip()
            }
        ),
        "biography": str(profile.get("biography") or "")[:500],
        "is_verified": profile.get("isVerified"),
        "is_private": profile.get("isPrivate"),
        "posts_count": profile.get("postsCount"),
    }


class BrandProfileRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def _write_candidates(
        self,
        connection,
        profile_id: UUID,
        candidates: list[dict[str, Any]],
    ) -> None:
        connection.execute(
            "DELETE FROM discovery_candidates WHERE brand_profile_id = %s",
            (profile_id,),
        )
        for candidate in candidates[:50]:
            if not candidate.get("candidate_id"):
                candidate["candidate_id"] = uuid4()
            profile = candidate.get("profile")
            profile = profile if isinstance(profile, dict) else {}
            connection.execute(
                """
                INSERT INTO discovery_candidates (
                    id, brand_profile_id, username, ig_user_id, profile_url,
                    follower_count, score, engagement_rate_bps, us_signal,
                    evidence, status
                ) VALUES (
                    %(candidate_id)s, %(profile_id)s, %(username)s,
                    %(ig_user_id)s, %(profile_url)s, %(followers)s,
                    %(score)s, %(rate)s, %(us_signal)s, %(evidence)s, 'suggested'
                )
                ON CONFLICT (brand_profile_id, username) DO UPDATE SET
                    ig_user_id = excluded.ig_user_id,
                    profile_url = excluded.profile_url,
                    follower_count = excluded.follower_count,
                    score = excluded.score,
                    engagement_rate_bps = excluded.engagement_rate_bps,
                    us_signal = excluded.us_signal,
                    evidence = excluded.evidence,
                    updated_at = now()
                """,
                {
                    "profile_id": profile_id,
                    "candidate_id": candidate["candidate_id"],
                    "username": candidate["username"],
                    "ig_user_id": profile.get("id") or profile.get("pk"),
                    "profile_url": f"https://www.instagram.com/{candidate['username']}/",
                    "followers": profile.get("followersCount") or profile.get("followers"),
                    "score": candidate["score"],
                    "rate": round(float(candidate["engagement_rate"]) * 10_000),
                    "us_signal": candidate["us_signal"],
                    "evidence": Jsonb(_candidate_evidence(candidate)),
                },
            )

    # How long a profile may sit at 'pending' before a reader treats it as
    # failed. Candidate discovery runs as an in-process background task, so an
    # API restart mid-run strands the row at 'pending' with nothing left to
    # finish it. Without this the onboarding UI would poll a spinner forever on
    # a job that no longer exists. Generous, because the work legitimately takes
    # a minute or two: a SERP search plus one browser fetch per candidate.
    PENDING_STALE_AFTER = timedelta(minutes=10)

    def complete_discovery(
        self,
        profile_id: UUID,
        *,
        expected_revision: object,
        candidates: list[dict[str, Any]],
    ) -> bool:
        """Store candidates only when this analysis revision is still current."""

        with self.pool.connection() as connection, connection.transaction():
            current = connection.execute(
                """
                SELECT 1
                  FROM brand_profiles
                 WHERE id = %s AND status = 'pending' AND updated_at = %s
                 FOR UPDATE
                """,
                (profile_id, expected_revision),
            ).fetchone()
            if current is None:
                return False
            self._write_candidates(connection, profile_id, candidates)
            connection.execute(
                """
                UPDATE brand_profiles
                   SET status = 'complete', updated_at = clock_timestamp()
                 WHERE id = %s
                """,
                (profile_id,),
            )
        return True

    def fail_discovery(self, profile_id: UUID, *, expected_revision: object) -> bool:
        """Mark only the matching analysis revision as failed."""

        with self.pool.connection() as connection:
            failed = connection.execute(
                """
                UPDATE brand_profiles
                   SET status = 'failed', updated_at = clock_timestamp()
                 WHERE id = %s AND status = 'pending' AND updated_at = %s
                RETURNING id
                """,
                (profile_id, expected_revision),
            ).fetchone()
        return failed is not None

    def _profile_status(self, row: dict[str, Any]) -> str:
        status = _text(row["status"])
        is_draft = row.get("has_analysis") is False or (
            "analysis" in row and row["analysis"] is None
        )
        if status == "pending" and not is_draft and row["updated_at"] is not None:
            age = datetime.now(timezone.utc) - row["updated_at"]
            if age > self.PENDING_STALE_AFTER:
                return "failed"
        return status

    def load_status_for_user(
        self,
        user_id: UUID,
        ig_account_id: UUID,
    ) -> dict[str, Any] | None:
        """Load discovery state and completed candidates for the polling route."""

        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT id, ig_account_id, status, updated_at, follower_audience_approved_at,
                       analysis IS NOT NULL AS has_analysis
                  FROM brand_profiles
                 WHERE user_id = %s AND ig_account_id = %s
                """,
                (user_id, ig_account_id),
            ).fetchone()
            if row is None:
                return None
            status = self._profile_status(row)
            candidate_rows = []
            if status == "complete":
                candidate_rows = connection.execute(
                    """
                    SELECT id, username, score, engagement_rate_bps,
                           us_signal, evidence, status
                      FROM discovery_candidates
                     WHERE brand_profile_id = %s
                       AND ig_user_id IS NOT NULL
                       AND btrim(ig_user_id) <> ''
                     ORDER BY score DESC, username ASC
                    """,
                    (row["id"],),
                ).fetchall()
        return {
            "profile_id": row["id"],
            "ig_account_id": row["ig_account_id"],
            "status": status,
            "follower_audience_approved": row.get("follower_audience_approved_at") is not None,
            "candidates": [
                {
                    "candidate_id": candidate["id"],
                    "username": _text(candidate["username"]),
                    "score": candidate["score"],
                    "engagement_rate": (
                        candidate["engagement_rate_bps"] or 0
                    )
                    / 10_000,
                    "us_signal": candidate["us_signal"],
                    "reasons": (candidate["evidence"] or {}).get("reasons", []),
                    "accepted": True,
                    "imported": _text(candidate["status"]) == "imported",
                }
                for candidate in candidate_rows
            ],
        }

    def load_for_user(
        self,
        user_id: UUID,
        ig_account_id: UUID,
    ) -> dict[str, Any] | None:
        """Load the saved strategy and candidate list for one owned account."""

        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT id, ig_account_id, status, onboarding_answers,
                       analysis, updated_at, follower_audience_approved_at
                  FROM brand_profiles
                 WHERE user_id = %s AND ig_account_id = %s
                """,
                (user_id, ig_account_id),
            ).fetchone()
            if row is None:
                return None
            status = self._profile_status(row)
            reference_rows = connection.execute(
                """
                SELECT profile_url
                  FROM brand_reference_accounts
                 WHERE brand_profile_id = %s
                 ORDER BY position ASC
                """,
                (row["id"],),
            ).fetchall()
            rows = connection.execute(
                """
                SELECT id, username, profile_url, follower_count, score,
                       engagement_rate_bps, us_signal, evidence, status
                  FROM discovery_candidates
                 WHERE brand_profile_id = %s
                   AND ig_user_id IS NOT NULL
                   AND btrim(ig_user_id) <> ''
                 ORDER BY score DESC, username ASC
                """,
                (row["id"],),
            ).fetchall()
        return {
            "profile_id": row["id"],
            "ig_account_id": row["ig_account_id"],
            "status": status,
            "follower_audience_approved": row.get("follower_audience_approved_at") is not None,
            "reference_urls": [_text(reference["profile_url"]) for reference in reference_rows],
            "answers": row["onboarding_answers"] or {},
            "analysis": row["analysis"],
            "candidates": [
                {
                    "candidate_id": r["id"],
                    "username": _text(r["username"]),
                    "profile_url": _text(r["profile_url"]),
                    "follower_count": r["follower_count"],
                    "score": r["score"],
                    "engagement_rate": (r["engagement_rate_bps"] or 0) / 10_000,
                    "us_signal": r["us_signal"],
                    # `reasons` lives inside evidence, and a stored candidate is
                    # one that was accepted -- select_balanced_candidates drops
                    # the rest before anything reaches this table.
                    "reasons": (r["evidence"] or {}).get("reasons", []),
                    "accepted": True,
                    "imported": _text(r["status"]) == "imported",
                    "evidence": r["evidence"],
                }
                for r in rows
            ],
        }

    def save(
        self,
        *,
        user_id: UUID,
        ig_account_id: UUID,
        references: list[dict[str, Any]],
        answers: dict[str, str],
        analysis: dict[str, Any] | None,
        candidates: list[dict[str, Any]],
        status: str = "complete",
        expected_revision: datetime | None = None,
    ) -> tuple[UUID, datetime]:
        """Persist the profile. `status` is 'pending' while candidates are still
        being discovered, so the caller can return the analysis immediately and
        fill the candidate list afterwards."""

        if status not in ("pending", "complete", "failed"):
            raise ValueError(f"unknown brand profile status: {status}")
        if not references:
            raise ValueError("at least one reference account is required")
        if len(references) > MAX_REFERENCE_ACCOUNTS:
            raise ValueError(
                f"at most {MAX_REFERENCE_ACCOUNTS} reference accounts are allowed"
            )
        primary = references[0]
        reference_url = primary["url"]
        reference_username = primary["username"]
        with self.pool.connection() as connection, connection.transaction():
            account = connection.execute(
                "SELECT id FROM ig_accounts WHERE id = %s AND user_id = %s FOR UPDATE",
                (ig_account_id, user_id),
            ).fetchone()
            if account is None:
                raise ValueError("Instagram account is not owned by this user")
            if expected_revision is not None:
                current = connection.execute(
                    "SELECT updated_at FROM brand_profiles WHERE user_id = %s AND ig_account_id = %s FOR UPDATE",
                    (user_id, ig_account_id),
                ).fetchone()
                if current is None or current["updated_at"] != expected_revision:
                    raise ValueError("source accounts changed; analyze the current saved draft")
            row = connection.execute(
                """
                INSERT INTO brand_profiles (
                    user_id, ig_account_id, reference_url, reference_username,
                    onboarding_answers, analysis, status
                ) VALUES (
                    %(user_id)s, %(ig_account_id)s, %(url)s, %(username)s,
                    %(answers)s, %(analysis)s, %(status)s
                )
                ON CONFLICT (user_id, ig_account_id) DO UPDATE SET
                    reference_url = excluded.reference_url,
                    reference_username = excluded.reference_username,
                    onboarding_answers = excluded.onboarding_answers,
                    analysis = excluded.analysis,
                    status = %(status)s,
                    follower_audience_approved_at = NULL,
                    updated_at = clock_timestamp()
                RETURNING id, updated_at
                """,
                {
                    "user_id": user_id,
                    "ig_account_id": ig_account_id,
                    "url": reference_url,
                    "username": reference_username,
                    "answers": Jsonb(answers),
                    "analysis": Jsonb(analysis) if analysis is not None else None,
                    "status": status,
                },
            ).fetchone()
            profile_id = row["id"]
            connection.execute(
                "DELETE FROM brand_reference_accounts WHERE brand_profile_id = %s",
                (profile_id,),
            )
            for position, reference in enumerate(references):
                connection.execute(
                    """
                    INSERT INTO brand_reference_accounts (
                        brand_profile_id, username, profile_url, position, scraped
                    ) VALUES (
                        %(profile_id)s, %(username)s, %(url)s, %(position)s, %(scraped)s
                    )
                    """,
                    {
                        "profile_id": profile_id,
                        "username": reference["username"],
                        "url": reference["url"],
                        "position": position,
                        "scraped": Jsonb(_reference_evidence(reference["scraped"]) if reference.get("scraped") is not None else {}),
                    },
                )
            self._write_candidates(connection, profile_id, candidates)
        return profile_id, row["updated_at"]

    def save_sources(
        self, *, user_id: UUID, ig_account_id: UUID,
        reference_urls: list[str], answers: dict[str, str],
    ) -> tuple[UUID, datetime]:
        """Persist an unapproved source draft before profile capture or analysis."""
        from engagemate.onboarding.brand import (
            canonicalize_reference_urls,
            reference_username,
        )

        urls = canonicalize_reference_urls(reference_urls)
        return self.save(
            user_id=user_id, ig_account_id=ig_account_id,
            references=[{"url": url, "username": reference_username(url)} for url in urls],
            answers=answers, analysis=None, candidates=[], status="pending",
        )

    def approve_sources(
        self, *, user_id: UUID, profile_id: UUID, ig_account_id: UUID,
        reference_urls: list[str],
    ) -> None:
        """Approve the current source list, rejecting stale or foreign reviews."""
        with self.pool.connection() as connection, connection.transaction():
            profile = connection.execute(
                """
                SELECT id FROM brand_profiles
                 WHERE id = %s AND user_id = %s AND ig_account_id = %s
                   AND status = 'complete' AND analysis IS NOT NULL
                 FOR UPDATE
                """, (profile_id, user_id, ig_account_id),
            ).fetchone()
            if profile is None:
                raise ValueError("complete brand analysis before approving sources")
            sources = connection.execute(
                """
                SELECT profile_url FROM brand_reference_accounts
                 WHERE brand_profile_id = %s ORDER BY position, id
                """, (profile_id,),
            ).fetchall()
            if not sources or [_text(row["profile_url"]) for row in sources] != reference_urls:
                raise ValueError("source accounts changed; review the current direction")
            connection.execute(
                """
                UPDATE brand_profiles SET follower_audience_approved_at = now()
                 WHERE id = %s AND user_id = %s AND ig_account_id = %s
                """, (profile_id, user_id, ig_account_id),
            )

    def approve_candidates(
        self,
        *,
        user_id: UUID,
        profile_id: UUID,
        candidate_ids: list[UUID],
        ig_account_id: UUID,
    ) -> int:
        """Import explicitly approved suggestions into one owned target list."""

        if not candidate_ids:
            return 0
        with self.pool.connection() as connection:
            account = connection.execute(
                "SELECT 1 FROM ig_accounts WHERE id = %s AND user_id = %s",
                (ig_account_id, user_id),
            ).fetchone()
            if account is None:
                raise ValueError("Instagram account is not owned by this user")
            rows = connection.execute(
                """
                SELECT candidate.id, candidate.username, candidate.ig_user_id
                  FROM discovery_candidates AS candidate
                  JOIN brand_profiles AS profile
                    ON profile.id = candidate.brand_profile_id
                 WHERE candidate.id = ANY(%(candidate_ids)s)
                   AND candidate.brand_profile_id = %(profile_id)s
                   AND profile.user_id = %(user_id)s
                   AND profile.ig_account_id = %(account_id)s
                   AND candidate.status IN ('suggested', 'approved', 'imported')
                   FOR UPDATE OF candidate
                """,
                {
                    "candidate_ids": candidate_ids,
                    "profile_id": profile_id,
                    "user_id": user_id,
                    "account_id": ig_account_id,
                },
            ).fetchall()
            if len(rows) != len(set(candidate_ids)):
                raise ValueError("one or more discovery candidates are unavailable")
            if any(
                row["ig_user_id"] is None or not _text(row["ig_user_id"]).strip()
                for row in rows
            ):
                raise ValueError(
                    "one or more discovery candidates have no Instagram identity"
                )
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO targets (
                        user_id, ig_account_id, username, ig_user_id, tier, source, enabled
                    ) VALUES (%(user_id)s, %(account_id)s, %(username)s, %(ig_user_id)s,
                              'discovery', 'hermes-discovery', true)
                    ON CONFLICT (ig_account_id, username) DO UPDATE SET
                        ig_user_id = excluded.ig_user_id,
                        tier = 'discovery',
                        source = 'hermes-discovery',
                        enabled = true,
                        updated_at = now()
                    """,
                    {
                        "user_id": user_id,
                        "account_id": ig_account_id,
                        "username": _text(row["username"]),
                        "ig_user_id": _text(row["ig_user_id"]),
                    },
                )
            connection.execute(
                """
                UPDATE discovery_candidates AS candidate
                   SET status = 'imported', updated_at = now()
                  FROM brand_profiles AS profile
                 WHERE candidate.id = ANY(%(candidate_ids)s)
                   AND candidate.brand_profile_id = %(profile_id)s
                   AND profile.id = candidate.brand_profile_id
                   AND profile.user_id = %(user_id)s
                   AND profile.ig_account_id = %(account_id)s
                """,
                {
                    "candidate_ids": candidate_ids,
                    "profile_id": profile_id,
                    "user_id": user_id,
                    "account_id": ig_account_id,
                },
            )
        return len(rows)
