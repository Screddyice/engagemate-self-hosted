"""Tenant-scoped read and write models for the customer control MCP."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from engagemate.db.text import text
from engagemate.domain.session_policy import INSTAGRAM_SESSION_MAX_AGE


@dataclass(frozen=True, slots=True)
class AccountStatus:
    id: UUID
    username: str
    health_status: str
    automation_enabled: bool
    session_captured_at: datetime | None
    session_expires_at: datetime | None
    proxy_bound: bool


@dataclass(frozen=True, slots=True)
class StatsOverview:
    period_start: datetime
    period_end: datetime
    timezone: str
    snapshot_captured_at: datetime | None
    followers: int | None
    following: int | None
    likes: int
    replies: int
    dms: int
    views: int
    failed: int


_CONFIG_COLUMNS = {
    "enabled": "enabled",
    "timezone": "timezone",
    "schedule_hour": "schedule_hour",
    "daily_like_limit": "daily_like_limit",
    "daily_reply_limit": "daily_reply_limit",
    "daily_dm_limit": "daily_dm_limit",
    "daily_reel_message_limit": "daily_reel_message_limit",
    "warmup_exempt": "warmup_exempt",
    "daily_view_limit": "daily_view_limit",
    "persona": "persona",
    "banned_phrases": "banned_phrases",
    "expected_language": "expected_language",
    "profile": "profile",
}


def _safe_json(value: Any) -> Any:
    if isinstance(value, (UUID, datetime)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    return value


class ControlRepository:
    """Execute only tenant-qualified control-plane queries."""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def list_accounts(self, user_id: UUID) -> list[AccountStatus]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT account.id, account.username, account.health_status,
                       COALESCE(config.enabled, false) AS automation_enabled,
                       session.captured_at AS session_captured_at,
                       session.expires_at AS session_expires_at,
                       EXISTS (
                           SELECT 1 FROM proxy_bindings AS binding
                           WHERE binding.user_id = account.user_id
                             AND binding.ig_account_id = account.id
                             AND binding.released_at IS NULL
                       ) AS proxy_bound
                FROM ig_accounts AS account
                LEFT JOIN agent_configs AS config
                  ON config.user_id = account.user_id
                 AND config.ig_account_id = account.id
                LEFT JOIN LATERAL (
                    SELECT captured_at, expires_at
                    FROM ig_sessions
                    WHERE user_id = account.user_id AND ig_account_id = account.id
                    ORDER BY captured_at DESC LIMIT 1
                ) AS session ON true
                WHERE account.user_id = %s
                ORDER BY account.created_at, account.id
                """,
                (user_id,),
            ).fetchall()
        return [self._account_status(row) for row in rows]

    def account_status(self, user_id: UUID, account_id: UUID) -> AccountStatus | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT account.id, account.username, account.health_status,
                       COALESCE(config.enabled, false) AS automation_enabled,
                       session.captured_at AS session_captured_at,
                       session.expires_at AS session_expires_at,
                       EXISTS (
                           SELECT 1 FROM proxy_bindings AS binding
                           WHERE binding.user_id = account.user_id
                             AND binding.ig_account_id = account.id
                             AND binding.released_at IS NULL
                       ) AS proxy_bound
                FROM ig_accounts AS account
                LEFT JOIN agent_configs AS config
                  ON config.user_id = account.user_id
                 AND config.ig_account_id = account.id
                LEFT JOIN LATERAL (
                    SELECT captured_at, expires_at
                    FROM ig_sessions
                    WHERE user_id = account.user_id AND ig_account_id = account.id
                    ORDER BY captured_at DESC LIMIT 1
                ) AS session ON true
                WHERE account.user_id = %s AND account.id = %s
                """,
                (user_id, account_id),
            ).fetchone()
        return None if row is None else self._account_status(row)

    @staticmethod
    def _account_status(row: dict[str, Any]) -> AccountStatus:
        return AccountStatus(
            id=row["id"],
            username=str(row["username"]),
            health_status=str(row["health_status"]),
            automation_enabled=bool(row["automation_enabled"]),
            session_captured_at=row["session_captured_at"],
            session_expires_at=row["session_expires_at"],
            proxy_bound=bool(row["proxy_bound"]),
        )

    def stats_overview(
        self,
        user_id: UUID,
        account_id: UUID,
        period_start: datetime,
        period_end: datetime,
    ) -> StatsOverview | None:
        params = {
            "user_id": user_id,
            "account_id": account_id,
            "period_start": period_start,
            "period_end": period_end,
        }
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT config.timezone,
                       snapshot.captured_at AS snapshot_captured_at,
                       snapshot.follower_count AS followers,
                       snapshot.following_count AS following,
                       COALESCE(activity.likes, 0) AS likes,
                       COALESCE(activity.replies, 0) AS replies,
                       COALESCE(activity.dms, 0) AS dms,
                       COALESCE(activity.views, 0) AS views,
                       COALESCE(activity.failed, 0) AS failed
                FROM ig_accounts AS account
                JOIN agent_configs AS config
                  ON config.user_id = account.user_id
                 AND config.ig_account_id = account.id
                LEFT JOIN LATERAL (
                    SELECT captured_at, follower_count, following_count
                    FROM account_growth_snapshots
                    WHERE user_id = account.user_id
                      AND ig_account_id = account.id
                      AND status IN ('complete', 'partial')
                    ORDER BY captured_at DESC LIMIT 1
                ) AS snapshot ON true
                LEFT JOIN LATERAL (
                    SELECT
                      count(*) FILTER (WHERE action = 'like' AND status = 'sent') AS likes,
                      count(*) FILTER (WHERE action = 'reply' AND status = 'sent') AS replies,
                      count(*) FILTER (WHERE action = 'dm' AND status = 'sent') AS dms,
                      count(*) FILTER (WHERE action = 'view' AND status = 'sent') AS views,
                      count(*) FILTER (WHERE status = 'failed') AS failed
                    FROM story_engagements
                    WHERE user_id = account.user_id
                      AND ig_account_id = account.id
                      AND created_at >= %(period_start)s
                      AND created_at < %(period_end)s
                ) AS activity ON true
                WHERE account.user_id = %(user_id)s AND account.id = %(account_id)s
                """,
                params,
            ).fetchone()
        if row is None:
            return None
        return StatsOverview(
            period_start=period_start,
            period_end=period_end,
            timezone=str(row["timezone"]),
            snapshot_captured_at=row["snapshot_captured_at"],
            followers=row["followers"],
            following=row["following"],
            likes=int(row["likes"]),
            replies=int(row["replies"]),
            dms=int(row["dms"]),
            views=int(row["views"]),
            failed=int(row["failed"]),
        )

    def growth_snapshots(
        self,
        user_id: UUID,
        account_id: UUID,
        period_start: datetime,
        period_end: datetime,
    ) -> list[dict[str, Any]]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT captured_at, follower_count, following_count,
                       media_count, engagement_rate_bps, status
                FROM account_growth_snapshots
                WHERE user_id = %s AND ig_account_id = %s
                  AND captured_at >= %s AND captured_at < %s
                ORDER BY captured_at
                """,
                (user_id, account_id, period_start, period_end),
            ).fetchall()
        return [dict(row) for row in rows]

    def activity(
        self, user_id: UUID, account_id: UUID, period_start: datetime, period_end: datetime
    ) -> list[dict[str, Any]]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, action, status, failure_code, created_at, executed_at
                FROM story_engagements
                WHERE user_id = %s AND ig_account_id = %s
                  AND created_at >= %s AND created_at < %s
                ORDER BY created_at DESC, id DESC
                LIMIT 500
                """,
                (user_id, account_id, period_start, period_end),
            ).fetchall()
        return [_safe_json(dict(row)) for row in rows]

    def get_config(self, user_id: UUID, account_id: UUID) -> dict[str, Any] | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT config.*, account.warmup_started_at, users.plan
                FROM agent_configs AS config
                JOIN ig_accounts AS account
                  ON account.id = config.ig_account_id AND account.user_id = config.user_id
                JOIN users ON users.id = config.user_id
                WHERE config.user_id = %s AND config.ig_account_id = %s
                """,
                (user_id, account_id),
            ).fetchone()
        return None if row is None else dict(row)

    def apply_config_patch(
        self,
        user_id: UUID,
        account_id: UUID,
        patch: dict[str, Any],
        actor_type: str,
        actor_id: str,
        tool_name: str,
    ) -> dict[str, Any]:
        unknown = set(patch) - set(_CONFIG_COLUMNS)
        if not patch or unknown:
            raise ValueError("invalid configuration patch")
        if "warmup_exempt" in patch and (actor_type != "system" or type(patch["warmup_exempt"]) is not bool):
            raise ValueError("warm-up exemption requires an operator")
        with self.pool.connection() as connection, connection.transaction():
            before = connection.execute(
                """
                SELECT * FROM agent_configs
                WHERE user_id = %s AND ig_account_id = %s
                FOR UPDATE
                """,
                (user_id, account_id),
            ).fetchone()
            if before is None:
                raise LookupError("account configuration not found")
            assignments = []
            values: list[Any] = []
            for key, value in patch.items():
                assignments.append(f'"{_CONFIG_COLUMNS[key]}" = %s')
                values.append(Jsonb(value) if key == "profile" else value)
            row = connection.execute(
                f"""
                UPDATE agent_configs
                SET {', '.join(assignments)}, version = version + 1, updated_at = now()
                WHERE user_id = %s AND ig_account_id = %s
                RETURNING *
                """,
                (*values, user_id, account_id),
            ).fetchone()
            snapshot = _safe_json(dict(row))
            connection.execute(
                """
                INSERT INTO agent_config_versions (
                    user_id, ig_account_id, version, snapshot, actor_type, actor_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (user_id, account_id, row["version"], Jsonb(snapshot), actor_type, actor_id),
            )
            connection.execute(
                """
                INSERT INTO mcp_audit_events (
                    user_id, ig_account_id, client_id, tool_name, outcome,
                    before_state, after_state
                ) VALUES (%s, %s, %s, %s, 'updated', %s, %s)
                """,
                (
                    user_id,
                    account_id,
                    actor_id if actor_type == "mcp" else None,
                    tool_name,
                    Jsonb(_safe_json(dict(before))),
                    Jsonb(snapshot),
                ),
            )
            return dict(row)

    def list_targets(self, user_id: UUID, account_id: UUID) -> list[dict[str, Any]]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, username, tier, enabled, created_at
                FROM targets
                WHERE user_id = %s AND ig_account_id = %s
                ORDER BY created_at, id
                """,
                (user_id, account_id),
            ).fetchall()
        return [_safe_json(dict(row)) for row in rows]

    def list_sources(self, user_id: UUID, account_id: UUID) -> list[dict[str, Any]]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT source.id, source.username, source.profile_url, source.position
                FROM brand_reference_accounts AS source
                JOIN brand_profiles AS profile ON profile.id = source.brand_profile_id
                WHERE profile.user_id = %s
                  AND profile.ig_account_id = %s
                ORDER BY source.position, source.id
                """,
                (user_id, account_id),
            ).fetchall()
        return [_safe_json(dict(row)) for row in rows]

    def create_pending_change(
        self,
        user_id: UUID,
        account_id: UUID,
        client_id: str,
        kind: str,
        patch: dict[str, Any],
        before: Any,
        after: Any,
        expires_at: datetime,
        tool_name: str,
    ) -> dict[str, Any]:
        with self.pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                INSERT INTO mcp_pending_changes (
                    user_id, ig_account_id, client_id, kind, patch,
                    before_state, after_state, expires_at
                )
                SELECT %s, account.id, %s, %s, %s, %s, %s, %s
                FROM ig_accounts AS account
                WHERE account.user_id = %s AND account.id = %s
                RETURNING id, expires_at, before_state, after_state, patch, kind
                """,
                (
                    user_id,
                    client_id,
                    kind,
                    Jsonb(_safe_json(patch)),
                    Jsonb(_safe_json(before)),
                    Jsonb(_safe_json(after)),
                    expires_at,
                    user_id,
                    account_id,
                ),
            ).fetchone()
            if row is None:
                raise LookupError("account not found")
            connection.execute(
                """
                INSERT INTO mcp_audit_events (
                    user_id, ig_account_id, client_id, tool_name, outcome,
                    before_state, after_state
                ) VALUES (%s, %s, %s, %s, 'pending', %s, %s)
                """,
                (
                    user_id,
                    account_id,
                    client_id,
                    tool_name,
                    Jsonb(_safe_json(before)),
                    Jsonb(_safe_json(after)),
                ),
            )
            return dict(row)

    def confirm_pending_change(
        self,
        user_id: UUID,
        client_id: str,
        change_id: UUID,
        now: datetime,
    ) -> dict[str, Any]:
        with self.pool.connection() as connection, connection.transaction():
            change = connection.execute(
                """
                SELECT id, ig_account_id, kind, patch, before_state, after_state,
                       expires_at, confirmed_at
                FROM mcp_pending_changes
                WHERE id = %s AND user_id = %s AND client_id = %s
                FOR UPDATE
                """,
                (change_id, user_id, client_id),
            ).fetchone()
            if change is None:
                raise LookupError("pending change not found")
            if change["confirmed_at"] is not None:
                raise ValueError("pending change was already confirmed")
            if change["expires_at"] <= now:
                raise ValueError("pending change expired")

            kind = str(change["kind"])
            account_id = change["ig_account_id"]
            patch = dict(change["patch"])
            if kind in {"enable_dm", "raise_limit"}:
                result = self._confirm_limits(
                    connection, user_id, account_id, client_id, patch, change
                )
            elif kind == "broaden_audience":
                result = self._confirm_audience(
                    connection, user_id, account_id, client_id, patch, change
                )
            else:
                raise ValueError("pending change kind is invalid")
            confirmed = connection.execute(
                """
                UPDATE mcp_pending_changes SET confirmed_at = %s
                WHERE id = %s RETURNING confirmed_at
                """,
                (now, change_id),
            ).fetchone()
            return {
                "kind": kind,
                "confirmed_at": confirmed["confirmed_at"],
                "result": result,
            }

    def _confirm_limits(
        self,
        connection: Any,
        user_id: UUID,
        account_id: UUID,
        client_id: str,
        patch: dict[str, Any],
        change: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "daily_like_limit",
            "daily_reply_limit",
            "daily_dm_limit",
            "daily_view_limit",
        }
        if not patch or set(patch) - allowed or any(
            type(value) is not int or value < 0 for value in patch.values()
        ):
            raise ValueError("pending limit patch is invalid")
        current = connection.execute(
            """
            SELECT * FROM agent_configs
            WHERE user_id = %s AND ig_account_id = %s
            FOR UPDATE
            """,
            (user_id, account_id),
        ).fetchone()
        if current is None:
            raise LookupError("account configuration not found")
        current_limits = {key: current[key] for key in allowed}
        if current_limits != change["before_state"]:
            raise ValueError("pending change is stale")
        assignments = [f'"{key}" = %s' for key in patch]
        updated = connection.execute(
            f"""
            UPDATE agent_configs
            SET {', '.join(assignments)}, version = version + 1, updated_at = now()
            WHERE user_id = %s AND ig_account_id = %s
            RETURNING *
            """,
            (*patch.values(), user_id, account_id),
        ).fetchone()
        snapshot = _safe_json(dict(updated))
        connection.execute(
            """
            INSERT INTO agent_config_versions (
                user_id, ig_account_id, version, snapshot, actor_type, actor_id
            ) VALUES (%s, %s, %s, %s, 'mcp', %s)
            """,
            (user_id, account_id, updated["version"], Jsonb(snapshot), client_id),
        )
        connection.execute(
            """
            INSERT INTO mcp_audit_events (
                user_id, ig_account_id, client_id, tool_name, outcome,
                before_state, after_state
            ) VALUES (%s, %s, %s, 'changes.confirm', 'updated', %s, %s)
            """,
            (
                user_id,
                account_id,
                client_id,
                Jsonb(_safe_json(change["before_state"])),
                Jsonb(_safe_json(change["after_state"])),
            ),
        )
        return {"version": updated["version"], "limits": _safe_json(change["after_state"])}

    def _confirm_audience(
        self,
        connection: Any,
        user_id: UUID,
        account_id: UUID,
        client_id: str,
        patch: dict[str, Any],
        change: dict[str, Any],
    ) -> dict[str, Any]:
        username = patch.get("username")
        tier = patch.get("tier")
        if (
            type(username) is not str
            or re.fullmatch(r"[a-z0-9._]{1,64}", username) is None
            or tier not in {"vip", "broad", "discovery"}
            or set(patch) != {"username", "tier"}
        ):
            raise ValueError("pending audience patch is invalid")
        rows = connection.execute(
            """
            SELECT username FROM targets
            WHERE user_id = %s AND ig_account_id = %s
            ORDER BY created_at, id
            FOR UPDATE
            """,
            (user_id, account_id),
        ).fetchall()
        current = [text(row["username"]).lower() for row in rows]
        if current != change["before_state"]:
            raise ValueError("pending change is stale")
        target = connection.execute(
            """
            INSERT INTO targets (
                user_id, ig_account_id, username, ig_user_id, tier, source, enabled
            ) VALUES (%s, %s, %s, NULL, %s, 'mcp', false)
            RETURNING id, username, tier, enabled, created_at
            """,
            (user_id, account_id, username, tier),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO mcp_audit_events (
                user_id, ig_account_id, client_id, tool_name, outcome,
                before_state, after_state
            ) VALUES (%s, %s, %s, 'changes.confirm', 'updated', %s, %s)
            """,
            (
                user_id,
                account_id,
                client_id,
                Jsonb(_safe_json(change["before_state"])),
                Jsonb(_safe_json(change["after_state"])),
            ),
        )
        return {"target": _safe_json(dict(target))}

    def add_source(self, user_id: UUID, account_id: UUID, username: str) -> dict[str, Any]:
        with self.pool.connection() as connection, connection.transaction():
            account = connection.execute(
                "SELECT id FROM ig_accounts WHERE user_id = %s AND id = %s",
                (user_id, account_id),
            ).fetchone()
            if account is None:
                raise LookupError("account not found")
            profile = connection.execute(
                """
                SELECT id FROM brand_profiles
                WHERE user_id = %s AND ig_account_id = %s
                FOR UPDATE
                """,
                (user_id, account_id),
            ).fetchone()
            if profile is None:
                raise LookupError("brand profile not found")
            count = connection.execute(
                "SELECT count(*) AS count FROM brand_reference_accounts WHERE brand_profile_id = %s",
                (profile["id"],),
            ).fetchone()["count"]
            if count >= 5:
                raise ValueError("source limit reached")
            row = connection.execute(
                """
                INSERT INTO brand_reference_accounts (
                    brand_profile_id, username, profile_url, position
                ) SELECT %s, %s, %s, slot
                    FROM generate_series(0, 4) AS slot
                   WHERE NOT EXISTS (
                       SELECT 1 FROM brand_reference_accounts
                        WHERE brand_profile_id = %s AND position = slot
                   )
                   ORDER BY slot LIMIT 1
                RETURNING id, username, profile_url, position
                """,
                (profile["id"], username, f"https://www.instagram.com/{username}/", profile["id"]),
            ).fetchone()
            connection.execute(
                "UPDATE brand_profiles SET follower_audience_approved_at = NULL WHERE id = %s",
                (profile["id"],),
            )
            return _safe_json(dict(row))

    def remove_source(self, user_id: UUID, account_id: UUID, source_id: UUID) -> bool:
        with self.pool.connection() as connection, connection.transaction():
            profile = connection.execute(
                """SELECT id FROM brand_profiles
                   WHERE user_id = %s AND ig_account_id = %s FOR UPDATE""",
                (user_id, account_id),
            ).fetchone()
            if profile is None:
                return False
            row = connection.execute(
                """DELETE FROM brand_reference_accounts
                   WHERE id = %s AND brand_profile_id = %s RETURNING id""",
                (source_id, profile["id"]),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE brand_profiles SET follower_audience_approved_at = NULL WHERE id = %s",
                    (profile["id"],),
                )
        return row is not None

    def remove_target(self, user_id: UUID, account_id: UUID, target_id: UUID) -> bool:
        try:
            with self.pool.connection() as connection:
                row = connection.execute(
                    """
                    DELETE FROM targets
                    WHERE id = %s AND user_id = %s AND ig_account_id = %s
                    RETURNING id
                    """,
                    (target_id, user_id, account_id),
                ).fetchone()
        except ForeignKeyViolation as error:
            if error.diag.constraint_name in {
                "relationship_follows_target_scope_fk",
                "relationship_follows_engagement_scope_fk",
            }:
                raise ValueError(
                    "Target has follow history and must be retained for unfollow tracking"
                ) from None
            raise
        return row is not None

    def pause_automation(self, user_id: UUID, account_id: UUID, client_id: str) -> bool:
        try:
            self.apply_config_patch(
                user_id, account_id, {"enabled": False}, "mcp", client_id, "automation.pause"
            )
        except LookupError:
            return False
        return True

    def resume_automation(
        self,
        user_id: UUID,
        account_id: UUID,
        client_id: str,
        now: datetime,
    ) -> None:
        """Resume an already-launched account after locking every readiness row."""

        with self.pool.connection() as connection, connection.transaction():
            readiness = connection.execute(
                """
                SELECT account.health_status,
                       locked_config.snapshot AS config_snapshot,
                       locked_config.launched_at,
                       locked_config.persona,
                       locked_session.id AS session_id,
                       locked_session.captured_at,
                       locked_session.expires_at,
                       locked_binding.id AS binding_id,
                       locked_target.id AS target_id,
                       locked_sources.id AS source_profile_id
                  FROM ig_accounts AS account
                  LEFT JOIN LATERAL (
                       SELECT to_jsonb(config) AS snapshot,
                              config.launched_at,
                              config.persona
                         FROM agent_configs AS config
                        WHERE config.user_id = account.user_id
                          AND config.ig_account_id = account.id
                        LIMIT 1
                          FOR UPDATE
                  ) AS locked_config ON true
                  LEFT JOIN LATERAL (
                       SELECT session.id, session.captured_at, session.expires_at
                         FROM ig_sessions AS session
                        WHERE session.user_id = account.user_id
                          AND session.ig_account_id = account.id
                        ORDER BY session.captured_at DESC, session.id DESC
                        LIMIT 1
                          FOR UPDATE
                  ) AS locked_session ON true
                  LEFT JOIN LATERAL (
                       SELECT binding.id
                         FROM proxy_bindings AS binding
                         JOIN proxies AS proxy ON proxy.id = binding.proxy_id
                        WHERE binding.user_id = account.user_id
                          AND binding.ig_account_id = account.id
                          AND binding.released_at IS NULL
                          AND proxy.health_status = 'active'
                        ORDER BY binding.bound_at DESC, binding.id DESC
                        LIMIT 1
                          FOR UPDATE OF binding, proxy
                  ) AS locked_binding ON true
                  LEFT JOIN LATERAL (
                       SELECT target.id
                         FROM targets AS target
                        WHERE target.user_id = account.user_id
                          AND target.ig_account_id = account.id
                          AND target.enabled = true
                          AND target.source <> 'source-followers'
                          AND target.ig_user_id IS NOT NULL
                        ORDER BY target.created_at, target.id
                        LIMIT 1
                          FOR UPDATE
                  ) AS locked_target ON true
                  LEFT JOIN LATERAL (
                       SELECT profile.id FROM brand_profiles profile
                        WHERE profile.user_id = account.user_id
                          AND profile.ig_account_id = account.id
                          AND profile.status = 'complete'
                          AND profile.follower_audience_approved_at IS NOT NULL
                          AND EXISTS (SELECT 1 FROM brand_reference_accounts source
                                       WHERE source.brand_profile_id = profile.id)
                        LIMIT 1 FOR UPDATE
                  ) AS locked_sources ON true
                 WHERE account.user_id = %s AND account.id = %s
                 FOR UPDATE OF account
                """,
                (user_id, account_id),
            ).fetchone()
            if readiness is None:
                raise LookupError("account not found")
            if text(readiness["health_status"]) != "active":
                raise ValueError("account health does not allow resume")

            if readiness["session_id"] is None or readiness["captured_at"] is None or (
                readiness["expires_at"] is not None
                and readiness["expires_at"] <= now
            ) or readiness["captured_at"] < now - INSTAGRAM_SESSION_MAX_AGE:
                raise ValueError("Instagram session is unavailable")

            if readiness["binding_id"] is None:
                raise ValueError("account proxy is unavailable")
            before = readiness["config_snapshot"]
            if before is None:
                raise LookupError("account configuration not found")
            if readiness["launched_at"] is None:
                raise ValueError("complete onboarding launch before using resume")
            if not text(readiness["persona"] or "").strip():
                raise ValueError("save the account voice before resume")

            if readiness["target_id"] is None and readiness.get("source_profile_id") is None:
                raise ValueError("approve your source audience or enable at least one target before resume")

            updated = connection.execute(
                """
                UPDATE agent_configs
                SET enabled = true, version = version + 1, updated_at = now()
                WHERE user_id = %s AND ig_account_id = %s
                RETURNING *
                """,
                (user_id, account_id),
            ).fetchone()
            snapshot = _safe_json(dict(updated))
            connection.execute(
                """
                INSERT INTO agent_config_versions (
                    user_id, ig_account_id, version, snapshot, actor_type, actor_id
                ) VALUES (%s, %s, %s, %s, 'mcp', %s)
                """,
                (user_id, account_id, updated["version"], Jsonb(snapshot), client_id),
            )
            connection.execute(
                """
                INSERT INTO mcp_audit_events (
                    user_id, ig_account_id, client_id, tool_name, outcome,
                    before_state, after_state
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    account_id,
                    client_id,
                    "automation.resume",
                    "updated",
                    Jsonb(_safe_json(dict(before))),
                    Jsonb(snapshot),
                ),
            )
