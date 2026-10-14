"""Durable, sender-scoped follower pages and candidate provenance."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from engagemate.worker.story_types import DiscoveryContext, DiscoveryTarget

_HANDLE = re.compile(r"[a-zA-Z0-9._]{1,30}")
_OWNER = re.compile(r"[0-9]{1,32}")

# Lock the approval and source rows through the write transaction. Replacing a
# source or revoking consent must wait for an already authorized page to finish.
_APPROVED = """
SELECT source.id
FROM brand_reference_accounts source
JOIN brand_profiles profile ON profile.id = source.brand_profile_id
WHERE profile.user_id = %(user_id)s AND profile.ig_account_id = %(account_id)s
  AND profile.status = 'complete' AND profile.follower_audience_approved_at IS NOT NULL
  AND source.id = ANY(%(source_ids)s)
ORDER BY source.id
FOR SHARE OF source, profile
"""
_PROVENANCE = """
SELECT 1 FROM follower_candidate_sources
WHERE user_id = %(user_id)s AND ig_account_id = %(account_id)s
  AND owner_id = %(owner_id)s AND source_id = ANY(%(source_ids)s)
LIMIT 1
"""
_NEW_PROSPECT = """
 AND NOT EXISTS (SELECT 1 FROM relationship_follows f
   WHERE f.user_id=candidate.user_id AND f.ig_account_id=candidate.ig_account_id
     AND f.owner_id=candidate.owner_id)
 AND NOT EXISTS (SELECT 1 FROM story_engagements e JOIN targets t
   ON t.id=e.target_id AND t.user_id=e.user_id AND t.ig_account_id=e.ig_account_id
   WHERE e.user_id=candidate.user_id AND e.ig_account_id=candidate.ig_account_id
     AND t.ig_user_id=candidate.owner_id)
 AND NOT EXISTS (SELECT 1 FROM hermes_tool_calls tc
   JOIN hermes_execution_runs r ON r.id=tc.run_id
   JOIN targets t ON t.id=tc.target_id AND t.user_id=r.user_id AND t.ig_account_id=r.ig_account_id
   WHERE r.user_id=candidate.user_id AND r.ig_account_id=candidate.ig_account_id
     AND t.ig_user_id=candidate.owner_id AND tc.status IN ('started','confirmed','uncertain'))
"""


def _params(context: DiscoveryContext) -> dict:
    if type(context.user_id) is not UUID or type(context.ig_account_id) is not UUID:
        raise ValueError("invalid follower tenant")
    return {"user_id": context.user_id, "account_id": context.ig_account_id}


def _identity(owner_id: str, handle: str) -> tuple[str, str]:
    if not isinstance(owner_id, str) or not _OWNER.fullmatch(owner_id):
        raise ValueError("invalid follower owner id")
    if not isinstance(handle, str) or not _HANDLE.fullmatch(handle):
        raise ValueError("invalid follower handle")
    return owner_id, handle.lower()


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


class PostgresFollowerStore:
    """Commit pages and cursor progress together; admit approved provenance only."""

    def __init__(self, pool: ConnectionPool, *, require_new_relationship: bool = False) -> None:
        if type(require_new_relationship) is not bool:
            raise ValueError("new prospect selection must be boolean")
        self._pool = pool
        self._require_new_relationship = require_new_relationship

    async def source_state(self, context: DiscoveryContext, source_id: UUID) -> dict:
        return await asyncio.to_thread(self._source_state, context, source_id)

    async def save_page(
        self,
        context: DiscoveryContext,
        source_id: UUID,
        expected_cursor: str | None,
        next_cursor: str | None,
        exhausted: bool,
        candidates: list[dict],
    ) -> bool:
        identities = [_identity(c["owner_id"], c["handle"]) for c in candidates]
        return await asyncio.to_thread(
            self._save_page,
            context,
            source_id,
            expected_cursor,
            next_cursor,
            exhausted,
            identities,
        )

    async def next_candidate(
        self,
        context: DiscoveryContext,
        source_ids: list[UUID],
        now: datetime,
    ) -> dict | None:
        return await asyncio.to_thread(self._next_candidate, context, source_ids, now)

    async def mark_checked(
        self,
        context: DiscoveryContext,
        owner_id: str,
        reason: str,
        now: datetime,
    ) -> None:
        if reason not in {"excluded", "no_story", "unknown", "available"}:
            raise ValueError("invalid follower check result")
        await asyncio.to_thread(self._mark_checked, context, owner_id, reason, now)

    async def admit_target(
        self,
        context: DiscoveryContext,
        source_ids: list[UUID],
        owner_id: str,
        handle: str,
        now: datetime,
    ) -> DiscoveryTarget | None:
        owner_id, handle = _identity(owner_id, handle)
        return await asyncio.to_thread(
            self._admit_target,
            context,
            source_ids,
            owner_id,
            handle,
            now,
        )

    @staticmethod
    def _approved(cursor, params, source_ids):
        cursor.execute(_APPROVED, {**params, "source_ids": source_ids})
        return [row["id"] for row in cursor.fetchall()]

    def _source_state(self, context, source_id):
        params = {**_params(context), "source_id": source_id, "now": context.now}
        with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            if not self._approved(cursor, params, [source_id]):
                return {"cursor": None, "exhausted": True, "initialized": False}
            cursor.execute(
                """
                INSERT INTO follower_source_states (user_id, ig_account_id, source_id, updated_at)
                VALUES (%(user_id)s, %(account_id)s, %(source_id)s, %(now)s)
                ON CONFLICT DO NOTHING
                RETURNING source_id
            """,
                params,
            )
            inserted = cursor.fetchone() is not None
            cursor.execute(
                """
                UPDATE follower_source_states SET cursor = NULL, exhausted = false, updated_at = %(now)s
                WHERE user_id = %(user_id)s AND ig_account_id = %(account_id)s AND source_id = %(source_id)s
                  AND exhausted AND updated_at <= %(now)s - interval '24 hours'
            """,
                params,
            )
            cursor.execute(
                """
                SELECT cursor, exhausted FROM follower_source_states
                WHERE user_id = %(user_id)s AND ig_account_id = %(account_id)s AND source_id = %(source_id)s
            """,
                params,
            )
            row = cursor.fetchone()
            return {
                "cursor": _text(row["cursor"]),
                "exhausted": row["exhausted"],
                "initialized": not inserted,
            }

    def _save_page(
        self, context, source_id, expected, next_cursor, exhausted, candidates
    ):
        params = {
            **_params(context),
            "source_id": source_id,
            "expected": expected,
            "next": next_cursor,
            "exhausted": exhausted,
            "now": context.now,
        }
        with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            if not self._approved(cursor, params, [source_id]):
                return False
            cursor.execute(
                """
                INSERT INTO follower_source_states (user_id, ig_account_id, source_id, updated_at)
                VALUES (%(user_id)s, %(account_id)s, %(source_id)s, %(now)s)
                ON CONFLICT DO NOTHING
            """,
                params,
            )
            cursor.execute(
                """
                UPDATE follower_source_states
                SET cursor = %(next)s, exhausted = %(exhausted)s, updated_at = %(now)s
                WHERE user_id = %(user_id)s AND ig_account_id = %(account_id)s AND source_id = %(source_id)s
                  AND cursor IS NOT DISTINCT FROM %(expected)s AND NOT exhausted
                RETURNING source_id
            """,
                params,
            )
            if cursor.fetchone() is None:
                return False
            for owner_id, handle in candidates:
                item = {**params, "owner_id": owner_id, "handle": handle}
                cursor.execute(
                    """
                    INSERT INTO follower_candidates (user_id, ig_account_id, owner_id, handle, created_at)
                    VALUES (%(user_id)s, %(account_id)s, %(owner_id)s, %(handle)s, %(now)s)
                    ON CONFLICT (user_id, ig_account_id, owner_id) DO UPDATE SET handle = EXCLUDED.handle
                """,
                    item,
                )
                cursor.execute(
                    """
                    INSERT INTO follower_candidate_sources (user_id, ig_account_id, owner_id, source_id)
                    VALUES (%(user_id)s, %(account_id)s, %(owner_id)s, %(source_id)s)
                    ON CONFLICT DO NOTHING
                """,
                    item,
                )
            return True

    def _next_candidate(self, context, source_ids, now):
        params = {**_params(context), "now": now}
        with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            params["source_ids"] = self._approved(cursor, params, source_ids)
            if not params["source_ids"]:
                return None
            cursor.execute(
                """
                SELECT candidate.owner_id, candidate.handle FROM follower_candidates candidate
                WHERE candidate.user_id = %(user_id)s AND candidate.ig_account_id = %(account_id)s
                  AND (candidate.next_check_at IS NULL OR candidate.next_check_at <= %(now)s)
                  AND EXISTS (
                    SELECT 1 FROM follower_candidate_sources provenance
                    WHERE provenance.user_id = candidate.user_id
                      AND provenance.ig_account_id = candidate.ig_account_id
                      AND provenance.owner_id = candidate.owner_id
                      AND provenance.source_id = ANY(%(source_ids)s)
                  )
                """ + (_NEW_PROSPECT if self._require_new_relationship else "") + """
                ORDER BY candidate.last_checked_at NULLS FIRST, candidate.created_at, candidate.owner_id
                LIMIT 1
            """,
                params,
            )
            row = cursor.fetchone()
            return {k: _text(v) for k, v in row.items()} if row else None

    def _mark_checked(self, context, owner_id, reason, now):
        params = {
            **_params(context),
            "owner_id": owner_id,
            "reason": reason,
            "now": now,
            "next": now + timedelta(hours=24 if reason == "excluded" else 1),
        }
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE follower_candidates SET status = %(reason)s, last_checked_at = %(now)s, next_check_at = %(next)s
                WHERE user_id = %(user_id)s AND ig_account_id = %(account_id)s AND owner_id = %(owner_id)s
                  AND (last_checked_at IS NULL OR last_checked_at <= %(now)s)
            """,
                params,
            )

    def _admit_target(self, context, source_ids, owner_id, handle, now):
        params = {
            **_params(context),
            "owner_id": owner_id,
            "handle": handle,
            "now": now,
        }
        with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            # Serialize admission for one sender, including aliases of an owner.
            cursor.execute(
                """
                SELECT id FROM ig_accounts WHERE id = %(account_id)s AND user_id = %(user_id)s FOR UPDATE
            """,
                params,
            )
            if cursor.fetchone() is None:
                return None
            params["source_ids"] = self._approved(cursor, params, source_ids)
            if not params["source_ids"]:
                return None
            cursor.execute(_PROVENANCE, params)
            if cursor.fetchone() is None:
                return None
            cursor.execute(
                """
                SELECT id, ig_user_id, username, tier, enabled FROM targets
                WHERE user_id = %(user_id)s AND ig_account_id = %(account_id)s
                  AND (ig_user_id = %(owner_id)s OR lower(username) = %(handle)s)
                FOR UPDATE
            """,
                params,
            )
            rows = cursor.fetchall()
            if rows:
                # An ambiguous username, a disabled target, or duplicate identity
                # cannot confer permission to engage this follower.
                if (
                    len(rows) != 1
                    or not rows[0]["enabled"]
                    or _text(rows[0]["ig_user_id"]) != owner_id
                ):
                    return None
                row = rows[0]
            else:
                cursor.execute(
                    """
                    INSERT INTO targets (user_id, ig_account_id, username, ig_user_id, tier, source, enabled, created_at, updated_at)
                    VALUES (%(user_id)s, %(account_id)s, %(handle)s, %(owner_id)s, 'discovery', 'source-followers', true, %(now)s, %(now)s)
                    ON CONFLICT DO NOTHING
                    RETURNING id, ig_user_id, username, tier, enabled
                """,
                    params,
                )
                row = cursor.fetchone()
                if row is None:
                    return None
            return DiscoveryTarget(
                id=row["id"],
                user_id=context.user_id,
                ig_account_id=context.ig_account_id,
                owner_id=_text(row["ig_user_id"]),
                handle=_text(row["username"]),
                tier=_text(row["tier"]),
                enabled=row["enabled"],
            )
