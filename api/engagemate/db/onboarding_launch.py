"""Atomic launch policy for a fully configured Instagram account."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from engagemate.db.text import text
from engagemate.domain.session_policy import INSTAGRAM_SESSION_MAX_AGE
from psycopg_pool import ConnectionPool


class OnboardingLaunchRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def launch(
        self,
        *,
        user_id: UUID,
        ig_account_id: UUID,
        timezone_name: str,
    ) -> dict[str, Any]:
        """Launch or resume one fully configured account.

        First launch starts warm-up and applies the browser timezone.
        Saved maxima stay separate from the worker's temporary warm-up ceiling.
        Resume repeats readiness checks while preserving established settings.
        Both transitions share one transaction and lock the readiness rows.
        """

        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT config.persona, config.launched_at, account.health_status,
                       locked_session.id IS NOT NULL AS has_session,
                       locked_binding.ig_account_id IS NOT NULL AS has_proxy,
                       locked_target.id IS NOT NULL AS has_target,
                       locked_profile.id IS NOT NULL AS has_approved_profile,
                       COALESCE(locked_profile.source_approved, false) AS has_approved_sources
                  FROM agent_configs AS config
                  JOIN ig_accounts AS account
                    ON account.id = config.ig_account_id
                   AND account.user_id = config.user_id
                  LEFT JOIN LATERAL (
                       SELECT session.id
                         FROM ig_sessions AS session
                        WHERE session.user_id = config.user_id
                          AND session.ig_account_id = config.ig_account_id
                          AND (
                                session.expires_at IS NULL
                                OR session.expires_at > now()
                              )
                          AND session.captured_at >= now() - %(max_session_age)s
                        ORDER BY session.id
                        LIMIT 1
                          FOR UPDATE
                  ) AS locked_session ON true
                  LEFT JOIN LATERAL (
                       SELECT binding.ig_account_id
                         FROM proxy_bindings AS binding
                         JOIN proxies AS proxy ON proxy.id = binding.proxy_id
                        WHERE binding.user_id = config.user_id
                          AND binding.ig_account_id = config.ig_account_id
                          AND binding.released_at IS NULL
                          AND proxy.health_status = 'active'
                        LIMIT 1
                          FOR UPDATE OF binding, proxy
                  ) AS locked_binding ON true
                  LEFT JOIN LATERAL (
                       SELECT target.id
                         FROM targets AS target
                        WHERE target.user_id = config.user_id
                          AND target.ig_account_id = config.ig_account_id
                          AND target.enabled = true
                          AND target.source <> 'source-followers'
                          AND target.ig_user_id IS NOT NULL
                        ORDER BY target.id
                        LIMIT 1
                          FOR UPDATE
                  ) AS locked_target ON true
                  LEFT JOIN LATERAL (
                       SELECT profile.id, (profile.follower_audience_approved_at IS NOT NULL
                           AND profile.status = 'complete' AND EXISTS (
                               SELECT 1 FROM brand_reference_accounts source
                               WHERE source.brand_profile_id = profile.id
                           )) AS source_approved
                         FROM brand_profiles AS profile
                        WHERE profile.user_id = config.user_id
                          AND profile.ig_account_id = config.ig_account_id
                          AND ((profile.follower_audience_approved_at IS NOT NULL
                                AND profile.status = 'complete' AND EXISTS (
                                    SELECT 1 FROM brand_reference_accounts source
                                    WHERE source.brand_profile_id = profile.id
                                )) OR EXISTS (
                              SELECT 1
                                FROM discovery_candidates AS candidate
                               WHERE candidate.brand_profile_id = profile.id
                                 AND candidate.status = 'imported'
                          ))
                        ORDER BY profile.id
                        LIMIT 1
                          FOR UPDATE
                  ) AS locked_profile ON true
                 WHERE config.user_id = %(user_id)s
                   AND config.ig_account_id = %(account_id)s
                 FOR UPDATE OF config, account
                """,
                {
                    "user_id": user_id,
                    "account_id": ig_account_id,
                    "max_session_age": INSTAGRAM_SESSION_MAX_AGE,
                },
            ).fetchone()
            if row is None:
                raise LookupError("Instagram account is not owned by this user")
            if text(row["health_status"]) != "active" or not row["has_session"]:
                raise ValueError("reconnect a healthy Instagram session before launch")
            if not row["has_proxy"]:
                raise ValueError("reconnect Instagram from this browser before launch")
            if not text(row["persona"] or "").strip():
                raise ValueError("save the account voice before launch")
            if not row["has_target"] and not row.get("has_approved_sources", False):
                raise ValueError("approve your source audience or at least one target before launch")
            if not row["has_approved_profile"]:
                raise ValueError(
                    "complete brand strategy for this Instagram account before launch"
                )

            # The budget policy refuses to run without a durable warm-up
            # origin. First launch starts it now. A legacy launched account
            # with no warm-up value inherits its original launch time so a
            # resume cannot reset it to day one.
            connection.execute(
                """
                UPDATE ig_accounts
                   SET warmup_started_at = CASE
                       WHEN %(launched_at)s::timestamptz IS NULL THEN now()
                       ELSE COALESCE(warmup_started_at, %(launched_at)s::timestamptz)
                   END
                 WHERE user_id = %(user_id)s
                   AND id = %(account_id)s
                """,
                {
                    "launched_at": row["launched_at"],
                    "user_id": user_id,
                    "account_id": ig_account_id,
                },
            )

            launched = connection.execute(
                """
                UPDATE agent_configs
                   SET enabled = true,
                       timezone = CASE
                           WHEN launched_at IS NULL THEN %(timezone)s
                           ELSE timezone
                       END,
                       launched_at = COALESCE(launched_at, now()),
                       updated_at = now()
                 WHERE user_id = %(user_id)s
                   AND ig_account_id = %(account_id)s
                RETURNING enabled, timezone, schedule_hour, daily_like_limit,
                          daily_reply_limit, daily_dm_limit, daily_view_limit
                """,
                {
                    "timezone": timezone_name,
                    "user_id": user_id,
                    "account_id": ig_account_id,
                },
            ).fetchone()
            if launched is None:
                raise RuntimeError("account launch update failed")
            return dict(launched)
