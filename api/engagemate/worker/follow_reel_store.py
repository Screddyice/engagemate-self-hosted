"""Lease one post-follow reel message and retain its external outcome."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from engagemate.db.text import text
from engagemate.worker.follow_reels import compose_message
from engagemate.domain.budgets import WARMUP_RESET_EVENT
from engagemate.worker.reel_budget import reel_capacity


# These failures happen before the message transport is armed. Keep the
# reservation available while the proxy or browser recovers; the existing
# twenty-four-hour reservation TTL still bounds retries.
_RETRYABLE_UNATTEMPTED_PREFLIGHT_REASONS = frozenset({
    "proxy_access_denied",
    "connection_failed",
    "account_browser_busy",
    "message_session_unavailable",
    "sender_unavailable",
})


def _retryable_unattempted(evidence: dict) -> bool:
    return (
        evidence.get("preflight_reason") in _RETRYABLE_UNATTEMPTED_PREFLIGHT_REASONS
        or evidence.get("reason") in _RETRYABLE_UNATTEMPTED_PREFLIGHT_REASONS
    )


@dataclass(frozen=True, slots=True)
class ReelMessageStoreScope:
    """One owner and one exact new follow reservation or message."""

    user_id: UUID
    account_id: UUID
    relationship_follow_id: UUID | None = None
    message_id: UUID | None = None
    first_attempt_only: bool = False

    def __post_init__(self):
        if (type(self.first_attempt_only) is not bool
                or type(self.user_id) is not UUID or type(self.account_id) is not UUID
                or (self.relationship_follow_id is None) == (self.message_id is None)
                or any(value is not None and type(value) is not UUID
                       for value in (self.relationship_follow_id, self.message_id))):
            raise ValueError("reel scope requires an owner and exactly one UUID selector")


@dataclass(frozen=True, slots=True)
class ReelMessageClaim:
    id: UUID
    user_id: UUID
    ig_account_id: UUID
    relationship_follow_id: UUID
    recipient_id: str
    handle: str
    sender_id: str
    sender_handle: str
    rotation_slot: int
    lease_token: UUID
    message_template: str = ""
    reel_url: str | None = None
    body: str | None = None


class PostgresFollowReelStore:
    def __init__(self, pool, *, lease_seconds: int = 180,
                 scope: ReelMessageStoreScope | None = None):
        if not 30 <= lease_seconds <= 600:
            raise ValueError("invalid reel message lease")
        if scope is not None and type(scope) is not ReelMessageStoreScope:
            raise ValueError("invalid reel message scope")
        self.pool, self.lease_seconds = pool, lease_seconds
        self._scope = scope

    @property
    def scope(self):
        return self._scope

    def _scope_filter(self):
        if self._scope is None:
            return "", ()
        field = "relationship_follow_id" if self._scope.relationship_follow_id else "id"
        return (
            f" AND m.user_id=%s AND m.ig_account_id=%s AND m.{field}=%s",
            (self._scope.user_id, self._scope.account_id,
             self._scope.relationship_follow_id or self._scope.message_id),
        )

    async def claim_next(self, worker_id: str, now: datetime) -> ReelMessageClaim | None:
        return await asyncio.to_thread(self._claim, worker_id, now)

    async def prepare(self, claim: ReelMessageClaim, reel_url: str, body: str, now: datetime) -> bool:
        return await asyncio.to_thread(self._prepare, claim, reel_url, body, now)

    async def mark_attempting(self, claim: ReelMessageClaim, now: datetime) -> bool:
        return await asyncio.to_thread(self._attempt, claim, now)

    async def finish(self, claim: ReelMessageClaim, outcome: str, evidence: dict, now: datetime) -> bool:
        return await asyncio.to_thread(self._finish, claim, outcome, evidence, now)

    @staticmethod
    def _time(now):
        if type(now) is not datetime or now.tzinfo is None:
            raise ValueError("message time must be timezone aware")
        return now.astimezone(UTC)

    def _claim(self, worker_id, now):
        now = self._time(now)
        if type(worker_id) is not str or not 1 <= len(worker_id) <= 200:
            raise ValueError("invalid message worker")
        restriction, scope_params = self._scope_filter()
        first_only = self._scope is not None and self._scope.first_attempt_only
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """WITH expired AS (
                     SELECT m.id FROM follow_reel_messages m
                     WHERE m.status='running' AND m.lease_expires_at<=%s
                     """ + restriction + """
                     FOR UPDATE SKIP LOCKED
                   )
                   UPDATE follow_reel_messages m SET status=CASE WHEN m.attempted_at IS NULL THEN 'pending' ELSE 'uncertain' END,claimed_by=NULL,
                     lease_token=NULL,lease_expires_at=NULL,
                     next_attempt_at=CASE WHEN m.attempted_at IS NULL THEN %s + interval '5 minutes' ELSE NULL END,
                     updated_at=%s,
                     evidence=evidence || '{"reason":"lease_expired"}'::jsonb
                   FROM expired e WHERE m.id=e.id""",
                (now,) + scope_params + (now, now),
            )
            cur.execute("""UPDATE follow_reel_messages m SET status='cancelled',updated_at=%s,
                evidence=evidence || jsonb_build_object(
                    'deferred_reason',evidence->>'reason',
                    'reason',CASE WHEN claim_count>=5 THEN 'retry_exhausted' ELSE 'pending_expired' END)
                WHERE status='pending' AND (
                    created_at<%s
                    OR (claim_count>=5 AND (
                        attempted_at IS NOT NULL
                        OR NOT (
                            COALESCE(evidence->>'preflight_reason','') = ANY(%s)
                            OR COALESCE(evidence->>'reason','') = ANY(%s)
                        )
                    ))
                )""" + restriction,
                (now, now-timedelta(hours=24),
                 list(_RETRYABLE_UNATTEMPTED_PREFLIGHT_REASONS),
                 list(_RETRYABLE_UNATTEMPTED_PREFLIGHT_REASONS)) + scope_params)
            cur.execute(
                """SELECT m.*,c.follow_reel_message_template,t.username AS handle,a.ig_user_id AS sender_id,
                          a.username AS sender_handle
                   FROM follow_reel_messages m
                   JOIN relationship_follows f ON f.id=m.relationship_follow_id
                     AND f.user_id=m.user_id AND f.ig_account_id=m.ig_account_id
                     AND f.target_id=m.target_id AND f.owner_id=m.recipient_id
                   JOIN ig_accounts a ON a.id=m.ig_account_id AND a.user_id=m.user_id
                   JOIN targets t ON t.id=m.target_id AND t.user_id=m.user_id
                     AND t.ig_account_id=m.ig_account_id
                   JOIN agent_configs c ON c.user_id=a.user_id AND c.ig_account_id=a.id
                   WHERE m.status='pending' AND m.rotation_slot IS NOT NULL
                     AND (m.next_attempt_at IS NULL OR m.next_attempt_at<=%s)
                     AND f.status='following' AND f.confirmed_follow_at IS NOT NULL
                     AND a.health_status='active' AND a.paused_at IS NULL AND c.enabled
                     AND c.follow_reel_message_enabled AND c.follow_reel_message_enabled_at<=m.reserved_at
                     AND NOT EXISTS (SELECT 1 FROM instagram_execution_controls x
                       WHERE x.blocked AND (x.scope='system'
                         OR (x.scope='action' AND x.action='dm')
                         OR (x.scope='tenant' AND x.user_id=a.user_id)
                         OR (x.scope='account' AND x.user_id=a.user_id AND x.ig_account_id=a.id)))
                     AND t.ig_user_id=m.recipient_id
                     AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.user_id=a.user_id
                       AND j.ig_account_id=a.id AND j.status='running'
                       AND j.lease_expires_at>%s)
                   """ + restriction + (" AND m.claim_count=0 AND m.attempted_at IS NULL" if first_only else "") + """
                   ORDER BY m.created_at,m.id
                   FOR UPDATE OF a,m SKIP LOCKED""",
                (now, now) + scope_params,
            )
            candidates = cur.fetchall()
            row = next((candidate for candidate in candidates if self._policy(cur, candidate, now)), None)
            if row is None:
                return None
            token = uuid4()
            cur.execute(
                """UPDATE follow_reel_messages SET status='running',claimed_by=%s,
                   lease_token=%s,lease_expires_at=%s,updated_at=%s,claim_count=claim_count+1,reserved_at=%s WHERE id=%s""",
                (worker_id, token, now + timedelta(seconds=self.lease_seconds), now, now, row["id"]),
            )
            return ReelMessageClaim(
                row["id"], row["user_id"], row["ig_account_id"],
                row["relationship_follow_id"], text(row["recipient_id"]), text(row["handle"]),
                text(row["sender_id"]), text(row["sender_handle"]), row["rotation_slot"], token,
                text(row["follow_reel_message_template"]), text(row["reel_url"]), text(row["body"]),
            )

    @staticmethod
    def _policy(cur, row, now, claim=None):
        cur.execute("""SELECT (SELECT u.plan FROM users u WHERE u.id=a.user_id) AS plan,c.daily_reply_limit,c.daily_dm_limit,c.daily_reel_message_limit,c.warmup_exempt,c.timezone,a.warmup_started_at,
              (SELECT max(h.created_at) FROM account_health_events h WHERE h.user_id=a.user_id
                AND h.ig_account_id=a.id AND h.event_type=ANY(%s)) AS restricted_at
            FROM follow_reel_messages m
            JOIN ig_accounts a ON a.id=m.ig_account_id AND a.user_id=m.user_id
            JOIN agent_configs c ON c.user_id=a.user_id AND c.ig_account_id=a.id
            JOIN relationship_follows f ON f.id=m.relationship_follow_id AND f.user_id=m.user_id
              AND f.ig_account_id=m.ig_account_id AND f.owner_id=m.recipient_id AND f.target_id=m.target_id
            JOIN targets t ON t.id=m.target_id AND t.user_id=m.user_id AND t.ig_account_id=m.ig_account_id
            WHERE m.id=%s AND a.health_status='active' AND a.paused_at IS NULL AND c.enabled
              AND c.follow_reel_message_enabled AND c.follow_reel_message_enabled_at<=m.reserved_at
              AND m.created_at>=%s AND m.claim_count<=5 AND m.attempted_at IS NULL
              AND f.status='following' AND f.confirmed_follow_at IS NOT NULL
              AND t.enabled AND t.ig_user_id=m.recipient_id
              AND (%s::text IS NULL OR (a.ig_user_id=%s AND a.username=%s AND t.username=%s))
              AND EXISTS (SELECT 1 FROM proxy_bindings b JOIN proxies p ON p.id=b.proxy_id
                WHERE b.user_id=a.user_id AND b.ig_account_id=a.id AND b.released_at IS NULL AND p.health_status='active')
              AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.user_id=a.user_id AND j.ig_account_id=a.id
                AND j.status='running' AND j.lease_expires_at>%s)
              AND NOT EXISTS (SELECT 1 FROM relationship_follows other WHERE other.user_id=a.user_id
                AND other.ig_account_id=a.id AND other.status IN ('follow_running','unfollow_running') AND other.lease_expires_at>%s)
              AND NOT EXISTS (SELECT 1 FROM relationship_reconciliation_attempts r WHERE r.user_id=a.user_id
                AND r.ig_account_id=a.id AND r.outcome='pending' AND r.lease_expires_at>%s)
              AND NOT EXISTS (SELECT 1 FROM follow_reel_messages other WHERE other.user_id=a.user_id
                AND other.ig_account_id=a.id AND other.id<>m.id AND other.status='running' AND other.lease_expires_at>%s)
              AND NOT EXISTS (SELECT 1 FROM instagram_execution_controls x WHERE x.blocked AND
                (x.scope='system' OR (x.scope='action' AND x.action='dm') OR (x.scope='tenant' AND x.user_id=a.user_id)
                 OR (x.scope='account' AND x.user_id=a.user_id AND x.ig_account_id=a.id)))
            FOR SHARE OF c,t""", ([WARMUP_RESET_EVENT],row['id'],now-timedelta(hours=24),
              claim.sender_id if claim else None, claim.sender_id if claim else None,
              claim.sender_handle if claim else None, claim.handle if claim else None,now,now,now,now))
        policy=cur.fetchone()
        if not policy or policy['warmup_started_at'] is None:
            return False
        start=max(policy['warmup_started_at'],policy['restricted_at'] or policy['warmup_started_at'])
        if start>now:
            return False
        return reel_capacity(cur, policy, row["user_id"], row["ig_account_id"], now,
                             message_id=row["id"])

    def _locked(self, cur, claim, now):
        restriction, scope_params = self._scope_filter()
        if self._scope is not None and self._scope.first_attempt_only:
            restriction += " AND m.claim_count=1"
        cur.execute(
            """SELECT m.* FROM follow_reel_messages m
               JOIN ig_accounts a ON a.id=m.ig_account_id AND a.user_id=m.user_id
               WHERE m.id=%s AND m.user_id=%s AND m.ig_account_id=%s
                 AND m.relationship_follow_id=%s AND m.recipient_id=%s
                 AND m.status='running' AND m.lease_token=%s AND m.lease_expires_at>%s
               """ + restriction + """
               FOR UPDATE OF a,m""",
            (claim.id, claim.user_id, claim.ig_account_id, claim.relationship_follow_id,
             claim.recipient_id, claim.lease_token, now) + scope_params,
        )
        return cur.fetchone()

    def _prepare(self, claim, reel_url, body, now):
        now = self._time(now)
        if type(reel_url) is not str or type(body) is not str or not body.endswith("\n" + reel_url):
            return False
        try:
            if compose_message(reel_url, body[:-len(reel_url)-1]) != body:
                return False
        except ValueError:
            return False
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = self._locked(cur, claim, now)
            if row is None or row["attempted_at"] is not None:
                return False
            if not self._policy(cur, row, now, claim):
                return False
            if row["reel_url"] is not None and (text(row["reel_url"]) != reel_url or text(row["body"]) != body):
                return False
            cur.execute(
                """UPDATE follow_reel_messages SET reel_url=%s,body=%s,updated_at=%s WHERE id=%s""",
                (reel_url, body, now, claim.id),
            )
            return True

    def _attempt(self, claim, now):
        now = self._time(now)
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = self._locked(cur, claim, now)
            if row is None or row["attempted_at"] is not None or not row["reel_url"] or not row["body"]:
                return False
            # Read the clock after acquiring the account and message locks.
            cur.execute("SELECT clock_timestamp() AS current_time")
            current_time = max(now, cur.fetchone()["current_time"])
            if row["lease_expires_at"] <= current_time:
                return False
            if not self._policy(cur, row, current_time, claim):
                return False
            cur.execute("SELECT clock_timestamp() AS current_time")
            current_time = max(now, cur.fetchone()["current_time"])
            cur.execute(
                """UPDATE follow_reel_messages SET attempted_at=%s,updated_at=%s WHERE id=%s
                   AND user_id=%s AND ig_account_id=%s AND lease_token=%s
                   AND status='running' AND attempted_at IS NULL
                   AND lease_expires_at>clock_timestamp()
                   AND created_at>=clock_timestamp()-interval '24 hours'""",
                (current_time, current_time, claim.id, claim.user_id, claim.ig_account_id, claim.lease_token),
            )
            return cur.rowcount == 1

    def _finish(self, claim, outcome, evidence, now):
        now = self._time(now)
        if outcome not in {"sent", "uncertain", "deferred"} or type(evidence) is not dict:
            raise ValueError("invalid reel message outcome")
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = self._locked(cur, claim, now)
            if row is None or (outcome == "sent" and row["attempted_at"] is None):
                return False
            if outcome == "sent" and (evidence.get("recipient_id") != claim.recipient_id
                                      or evidence.get("sender_id") != claim.sender_id
                                      or not isinstance(evidence.get("item_id"), str)
                                      or not re.fullmatch(r"[0-9]{1,100}", evidence["item_id"])
                                      or not re.fullmatch(r"[0-9]{1,100}", str(evidence.get("thread_id", "")))
                                      or evidence.get("server_confirmed") is not True):
                return False
            final = "pending" if outcome == "deferred" and row["attempted_at"] is None else outcome
            if final == "deferred":
                final = "uncertain"
            retry_delay = timedelta(minutes=30) if (
                final == "pending" and row["claim_count"] >= 5
                and _retryable_unattempted(evidence)
            ) else timedelta(minutes=5)
            cur.execute(
                """UPDATE follow_reel_messages SET status=%s,claimed_by=NULL,lease_token=NULL,
                   lease_expires_at=NULL,sent_at=CASE WHEN %s='sent' THEN %s ELSE sent_at END,
                   next_attempt_at=CASE WHEN %s='pending' THEN %s ELSE NULL END,
                   evidence=evidence || %s,updated_at=%s WHERE id=%s""",
                (final, final, now, final, now + retry_delay, Jsonb(evidence), now, claim.id),
            )
            return True
