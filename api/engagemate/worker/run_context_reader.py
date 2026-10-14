"""Read every row one run needs, in one read-only transaction.

`RunContextLoader` owns the tenant checks and the decryption order; this module
owns only the reading. Both halves are deliberately separate so the loader's
safety rules can be tested without a database, and these queries can be tested
without a browser.

The transaction is `READ ONLY REPEATABLE READ` so a run cannot write through
its own context load, and so every row it sees comes from one consistent
snapshot rather than five separately-timed reads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from engagemate.domain.budgets import WARMUP_RESET_EVENT, daily_window_start
from engagemate.worker.story_admission_usage import UNMATCHED_STORY_CALLS
from engagemate.worker.run_context import (
    AccountRow,
    HistoryRow,
    RunContextError,
    RunContextRows,
    SessionRow,
    TargetRow,
    UsageRow,
)

_MAX_SENT_MESSAGES = 20
_NO_HISTORY_SUMMARY = "no recorded interactions with this target set"
# Only a reconnect after at least 30 days paused restarts warm-up. Breaker
# events still stop unsafe execution but never change the ramp by themselves.
_WARMUP_RESET_EVENTS = [WARMUP_RESET_EVENT]

_ACCOUNT_SQL = """
SELECT account.id,
       account.user_id,
       account.username,
       account.health_status,
       account.paused_at,
       account.warmup_started_at,
       (SELECT max(event.created_at)
          FROM account_health_events event
         WHERE event.ig_account_id = account.id
           AND event.user_id = account.user_id
           AND event.event_type = ANY(%(warmup_reset_events)s)
       ) AS last_restricted_at,
       account.login_country,
       account.login_region,
       account.login_city,
       account_user.plan,
       config.enabled,
       config.version,
       config.profile,
       config.timezone,
       config.schedule_hour,
       config.growth_goal,
       config.persona,
       config.daily_like_limit,
       config.daily_reply_limit,
       config.daily_dm_limit,
       config.daily_reel_message_limit,
       config.warmup_exempt,
       config.daily_view_limit,
       config.follow_after_engagement,
       config.follow_reel_message_enabled,
       config.banned_phrases,
       config.expected_language
FROM ig_accounts account
JOIN users account_user ON account_user.id = account.user_id
LEFT JOIN agent_configs config
       ON config.ig_account_id = account.id
      AND config.user_id = account.user_id
WHERE account.id = %(account_id)s
  AND account.user_id = %(user_id)s
"""

_SESSION_SQL = """
SELECT user_id,
       ig_account_id,
       encrypted_cookie_blob,
       captured_user_agent,
       captured_at,
       expires_at
FROM ig_sessions
WHERE ig_account_id = %(account_id)s
  AND user_id = %(user_id)s
ORDER BY captured_at DESC, id DESC
LIMIT 1
"""

_TARGETS_SQL = """
SELECT id, user_id, ig_account_id, ig_user_id, username, tier, source, enabled
FROM targets
WHERE ig_account_id = %(account_id)s
  AND user_id = %(user_id)s
  AND enabled
  AND source <> 'source-followers'
  AND ig_user_id IS NOT NULL
ORDER BY created_at, id
"""

_USAGE_SQL = """
SELECT
    count(*) FILTER (WHERE action = 'like') AS likes,
    count(*) FILTER (WHERE action = 'reply') AS replies,
    count(*) FILTER (WHERE action = 'dm') +
      (SELECT count(*) FROM follow_reel_messages m
       WHERE m.user_id = %(user_id)s AND m.ig_account_id = %(account_id)s
         AND m.status IN ('pending','running','sent','uncertain')
         AND ((m.reserved_at >= %(utc_midnight)s AND m.reserved_at <= %(now)s)
           OR (m.attempted_at >= %(utc_midnight)s AND m.attempted_at <= %(now)s))) AS dms,
    count(*) FILTER (WHERE action = 'view') AS views,
    (SELECT count(*) FROM follow_reel_messages m
       WHERE m.user_id = %(user_id)s AND m.ig_account_id = %(account_id)s
         AND m.status IN ('pending','running','sent','uncertain')
         AND ((m.reserved_at >= %(utc_midnight)s AND m.reserved_at <= %(now)s)
           OR (m.attempted_at >= %(utc_midnight)s AND m.attempted_at <= %(now)s))) AS reel_messages
FROM (
SELECT action FROM story_engagements
WHERE user_id = %(user_id)s
  AND ig_account_id = %(account_id)s
  AND status = 'sent'
  AND executed_at >= %(utc_midnight)s
  AND executed_at <= %(now)s
UNION ALL
""" + UNMATCHED_STORY_CALLS + """
) admission_usage
"""

_MESSAGES_SQL = """
SELECT generated_text
FROM story_engagements
WHERE user_id = %(user_id)s
  AND ig_account_id = %(account_id)s
  AND status = 'sent'
  AND generated_text IS NOT NULL
ORDER BY created_at DESC, id DESC
LIMIT %(limit)s
"""

_SOURCES_SQL = """
SELECT source.id, source.username, source.position, profile.follower_audience_approved_at
FROM brand_reference_accounts source
JOIN brand_profiles profile ON profile.id = source.brand_profile_id
WHERE profile.user_id = %(user_id)s
  AND profile.ig_account_id = %(account_id)s
  AND profile.status = 'complete'
ORDER BY source.position, source.id
"""


_FEEDBACK_SQL = """
SELECT f.owner_id, f.confirmed_follow_at, f.unfollowed_at, f.evidence,
       e.action
FROM relationship_follows f
JOIN story_engagements e ON e.id = f.prerequisite_engagement_id
 AND e.user_id = f.user_id AND e.ig_account_id = f.ig_account_id
 AND e.target_id = f.target_id
WHERE f.user_id = %(user_id)s AND f.ig_account_id = %(account_id)s
 AND f.confirmed_follow_at >= %(feedback_since)s
 AND f.confirmed_follow_at <= %(now)s
 AND e.status = 'sent' AND e.executed_at IS NOT NULL
 AND e.executed_at <= f.confirmed_follow_at
 AND e.action IN ('like', 'reply')
ORDER BY f.confirmed_follow_at, f.owner_id
LIMIT 5001
"""


def _timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        return parsed.astimezone(timezone.utc) if isinstance(parsed, datetime) and parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def learning_feedback(rows: list[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """Summarize observations without sending identities or raw evidence to Hermes."""
    groups = {action: {"mature": 0, "checked": 0, "follow_backs": 0, "unknown": 0}
              for action in ("like", "reply")}
    owners: dict[str, list[tuple[str, bool | None]]] = {}
    for row in rows[:5000]:
        follow_at = _timestamp(row.get("confirmed_follow_at"))
        evidence = row.get("evidence")
        owner = _text(row.get("owner_id"))
        action = _text(row.get("action"))
        if (not owner or action not in groups or follow_at is None
                or not now - timedelta(days=30) <= follow_at <= now - timedelta(hours=72)
                or not isinstance(evidence, dict)):
            continue
        baseline = evidence.get("follow")
        if not isinstance(baseline, dict) or baseline.get("outcome") != "confirmed" or baseline.get("followed_by") is not False:
            continue
        observations = []
        for key in ("first_follow_back", "reconciliation", "unfollow"):
            observation = evidence.get(key)
            if not isinstance(observation, dict):
                continue
            at = _timestamp(observation.get("observed_at") if key != "unfollow" else row.get("unfollowed_at"))
            if (observation.get("identity_matches") is True and observation.get("owner_id") == owner
                    and type(observation.get("followed_by")) is bool
                    and at is not None and follow_at < at <= now):
                observations.append((at, observation["followed_by"]))
        # Preserve a historical positive even if a later snapshot is negative.
        positive = any(value for _, value in observations)
        fresh_negative = any(not value and at >= now - timedelta(hours=24) for at, value in observations)
        owners.setdefault(owner, []).append((action, True if positive else False if fresh_negative else None))
    for observations in owners.values():
        actions = {action for action, _ in observations}
        if len(actions) != 1:  # Conflicting cohorts cannot support a comparison.
            continue
        group = groups[next(iter(actions))]
        outcome = True if any(value is True for _, value in observations) else False if any(value is False for _, value in observations) else None
        group["mature"] += 1
        group["checked"] += outcome is not None
        group["follow_backs"] += outcome is True
        group["unknown"] += outcome is None
    ready = len(rows) <= 5000 and all(g["checked"] >= 20 and g["unknown"] == 0 for g in groups.values())
    preferred = None
    if ready:
        rates = {action: g["follow_backs"] / g["checked"] for action, g in groups.items()}
        if rates["like"] != rates["reply"]:
            preferred = max(rates, key=rates.get)
    return {"version": 1, "status": "observational_comparison" if ready else "insufficient_evidence",
            "window_days": 30, "maturity_hours": 72, "negative_freshness_hours": 24,
            "minimum_checked_per_group": 20, "groups": groups, "preferred_action": preferred,
            "comment_outcomes": None, "message_response_outcomes": None}

class PostgresRunContextReader:
    """Load one run's rows from a single consistent snapshot."""

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._pool = pool
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return "PostgresRunContextReader(pool=<configured>)"

    async def read(self, user_id: UUID, ig_account_id: UUID) -> RunContextRows:
        """Return every row for one account, or fail closed with a reason."""

        if type(user_id) is not UUID or type(ig_account_id) is not UUID:
            raise RunContextError("invalid_job_tenant")
        now = self._now()
        return await asyncio.to_thread(self._read_sync, user_id, ig_account_id, now)

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise RunContextError("invalid_clock")
        return now.astimezone(timezone.utc)

    def _read_sync(
        self, user_id: UUID, ig_account_id: UUID, now: datetime
    ) -> RunContextRows:
        params = {
            "user_id": user_id,
            "account_id": ig_account_id,
            "now": now,
            "utc_midnight": now.replace(hour=0, minute=0, second=0, microsecond=0),
            "feedback_since": now - timedelta(days=30),
            "warmup_reset_events": _WARMUP_RESET_EVENTS,
        }
        with self._pool.connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
            # Must be the transaction's first statement to take effect.
            cursor.execute(
                "SET TRANSACTION READ ONLY, ISOLATION LEVEL REPEATABLE READ"
            )
            account = cursor.execute(_ACCOUNT_SQL, params).fetchone()
            if account is None:
                raise RunContextError("account_missing")
            if account["enabled"] is None:
                raise RunContextError("config_missing")
            try:
                params["utc_midnight"] = daily_window_start(now, _text(account["timezone"]))
            except (ValueError, TypeError, KeyError):
                raise RunContextError("config_missing") from None
            session = cursor.execute(_SESSION_SQL, params).fetchone()
            if session is None:
                raise RunContextError("session_missing")
            targets = cursor.execute(_TARGETS_SQL, params).fetchall()
            sources = cursor.execute(_SOURCES_SQL, params).fetchall()
            usage = cursor.execute(_USAGE_SQL, params).fetchone()
            feedback = learning_feedback(cursor.execute(_FEEDBACK_SQL, params).fetchall(), now)
            messages = cursor.execute(
                _MESSAGES_SQL, {**params, "limit": _MAX_SENT_MESSAGES}
            ).fetchall()

        return RunContextRows(
            account=_account_row(
                account,
                targets,
                sources,
                user_id,
                ig_account_id,
                feedback,
            ),
            session=_session_row(session, user_id, ig_account_id),
            targets=tuple(
                _target_row(row, user_id, ig_account_id) for row in targets
            ),
            usage=_usage_row(usage, user_id, ig_account_id),
            history=_history_row(messages, user_id, ig_account_id),
        )


def _account_row(
    row: Mapping[str, Any],
    targets: list[Mapping[str, Any]],
    sources: list[Mapping[str, Any]],
    user_id: UUID,
    ig_account_id: UUID,
    feedback: Mapping[str, Any] | None = None,
) -> AccountRow:
    # A row with no agent_config joins as NULLs, which is a configuration gap
    # rather than a decodable account.
    if row["enabled"] is None:
        raise RunContextError("config_missing")
    if not row["enabled"]:
        raise RunContextError("agent_disabled")

    persona = _text(row["persona"])
    if persona is None or not persona.strip():
        raise RunContextError("config_missing")
    version = row["version"]
    profile = row["profile"]
    if type(version) is not int or version < 1 or not isinstance(profile, dict):
        raise RunContextError("config_missing")

    banned_phrases = tuple(
        phrase
        for phrase in (_text(item) for item in row["banned_phrases"] or ())
        if phrase is not None and phrase.strip()
    )
    snapshot = {
        "follow_after_engagement": row.get("follow_after_engagement") is True,
        "follow_reel_message_enabled": row.get("follow_reel_message_enabled") is True,
        "learning_feedback": dict(feedback or {}),
        "version": version,
        "profile": dict(profile),
        "persona": persona,
        "schedule": {
            "enabled": bool(row["enabled"]),
            "timezone": _required_text(row["timezone"], "config_missing"),
            "hour": _count(row["schedule_hour"]),
        },
        "limits": {
            "reel_messages": row.get("daily_reel_message_limit"),
            "warmup_exempt": row.get("warmup_exempt") is True,
            "likes": _count(row["daily_like_limit"]),
            "replies": _count(row["daily_reply_limit"]),
            "dms": _count(row["daily_dm_limit"]),
            "views": _count(row["daily_view_limit"]),
            "growth_goal": (
                None if row["growth_goal"] is None else _count(row["growth_goal"])
            ),
        },
        "policy": {
            "banned_phrases": list(banned_phrases),
            "expected_language": _required_text(
                row["expected_language"], "config_missing"
            ),
        },
        "pause_state": {
            "health": _required_text(row["health_status"], "account_unreadable"),
            "paused": row["paused_at"] is not None,
        },
        "targets": [
            {
                "id": str(target["id"]),
                "username": _required_text(target["username"], "target_unreadable"),
                "tier": _required_text(target["tier"], "target_unreadable"),
                "source": _required_text(target["source"], "target_unreadable"),
                "enabled": bool(target["enabled"]),
            }
            for target in targets
        ],
        "follower_sources": [
            {
                "id": str(source["id"]),
                "username": _required_text(source["username"], "config_missing"),
            }
            for source in sources
            if source.get("follower_audience_approved_at") is not None
        ],
        "source_accounts": [
            {
                "username": _required_text(source["username"], "config_missing"),
                "position": _count(source["position"]),
            }
            for source in sources
        ],
    }

    return AccountRow(
        user_id=user_id,
        ig_account_id=ig_account_id,
        username=_required_text(row["username"], "account_unreadable"),
        health=_required_text(row["health_status"], "account_unreadable"),
        paused_at=row["paused_at"],
        warmup_started_at=row["warmup_started_at"],
        last_restricted_at=row["last_restricted_at"],
        geo_country=_required_text(row["login_country"], "account_geo_missing"),
        geo_region=_required_text(row["login_region"], "account_geo_missing"),
        geo_city=_required_text(row["login_city"], "account_geo_missing"),
        persona_text=persona,
        plan=_required_text(row["plan"], "account_unreadable"),
        daily_like_limit=_count(row["daily_like_limit"]),
        daily_reply_limit=_count(row["daily_reply_limit"]),
        daily_dm_limit=_count(row["daily_dm_limit"]),
        daily_view_limit=_count(row["daily_view_limit"]),
        banned_phrases=banned_phrases,
        expected_language=_required_text(row["expected_language"], "config_missing"),
        config_version=version,
        config_snapshot=snapshot,
        follow_after_engagement=row.get("follow_after_engagement") is True,
        follow_reel_message_enabled=row.get("follow_reel_message_enabled") is True,
        daily_reel_message_limit=(None if row.get("daily_reel_message_limit") is None
                                 else _count(row["daily_reel_message_limit"])),
        warmup_exempt=row.get("warmup_exempt") is True,
    )


def _session_row(
    row: Mapping[str, Any], user_id: UUID, ig_account_id: UUID
) -> SessionRow:
    return SessionRow(
        user_id=_uuid(row["user_id"], user_id),
        ig_account_id=_uuid(row["ig_account_id"], ig_account_id),
        ciphertext=_required_text(row["encrypted_cookie_blob"], "session_missing"),
        captured_user_agent=_required_text(
            row["captured_user_agent"], "session_missing"
        ),
        captured_at=row["captured_at"],
        expires_at=row["expires_at"],
    )


def _target_row(
    row: Mapping[str, Any], user_id: UUID, ig_account_id: UUID
) -> TargetRow:
    return TargetRow(
        user_id=_uuid(row["user_id"], user_id),
        ig_account_id=_uuid(row["ig_account_id"], ig_account_id),
        id=row["id"],
        owner_id=_required_text(row["ig_user_id"], "target_unreadable"),
        handle=_required_text(row["username"], "target_unreadable"),
        tier=_required_text(row["tier"], "target_unreadable"),
        enabled=bool(row["enabled"]),
    )


def _usage_row(
    row: Mapping[str, Any] | None, user_id: UUID, ig_account_id: UUID
) -> UsageRow:
    counts = row or {}
    return UsageRow(
        user_id=user_id,
        ig_account_id=ig_account_id,
        likes=_count(counts.get("likes", 0)),
        replies=_count(counts.get("replies", 0)),
        dms=_count(counts.get("dms", 0)),
        views=_count(counts.get("views", 0)),
        reel_messages=_count(counts.get("reel_messages", 0)),
    )


def _history_row(
    messages: list[Mapping[str, Any]],
    user_id: UUID,
    ig_account_id: UUID,
) -> HistoryRow:
    sent = tuple(
        text
        for text in (_text(row["generated_text"]) for row in messages)
        if text is not None and text.strip()
    )

    # IntelligenceHistory rejects a blank summary, so never emit one.
    summary = (
        f"{len(sent)} recent messages sent from this account"
        if sent
        else _NO_HISTORY_SUMMARY
    )
    return HistoryRow(
        user_id=user_id,
        ig_account_id=ig_account_id,
        recent_interaction_summary=summary,
        sent_messages=sent,
    )


def _uuid(value: object, expected: UUID) -> UUID:
    return value if type(value) is UUID else expected


def _text(value: object) -> str | None:
    """Decode text from UTF-8 and SQL_ASCII PostgreSQL clusters alike."""

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return value if type(value) is str else None


def _required_text(value: object, reason: str) -> str:
    text = _text(value)
    if text is None or not text.strip():
        raise RunContextError(reason)
    return text


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunContextError("invalid_count")
    return value
