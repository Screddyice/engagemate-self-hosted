"""Durable, non-billable relationship ownership and delayed unfollow claims."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from engagemate.domain.budgets import WARMUP_RESET_EVENT, daily_window_start
from engagemate.worker.reel_budget import reel_capacity


@dataclass(frozen=True, slots=True)
class FollowStoreScope:
    """One owner's confirmed Story reply, for a bounded new-follow check."""

    user_id: UUID
    account_id: UUID
    prerequisite_engagement_id: UUID

    def __post_init__(self):
        if any(type(value) is not UUID for value in (
            self.user_id, self.account_id, self.prerequisite_engagement_id,
        )):
            raise ValueError("follow scope requires UUID identifiers")


@dataclass(frozen=True, slots=True)
class PendingLikeFollowScope:
    """An owned pending relationship's first follow after a confirmed like."""

    user_id: UUID
    account_id: UUID
    prerequisite_engagement_id: UUID
    relationship_follow_id: UUID

    def __post_init__(self):
        if any(type(value) is not UUID for value in (
            self.user_id, self.account_id, self.prerequisite_engagement_id,
            self.relationship_follow_id,
        )):
            raise ValueError("pending like follow scope requires UUID identifiers")


def pending_like_message(cursor, scope, now, *, status="follow_pending"):
    """Return the exact original reservation only while its first follow is eligible."""
    rows = cursor.execute(
        """SELECT m.id AS message_id FROM relationship_follows f
               JOIN story_engagements e ON e.id=f.prerequisite_engagement_id
                 AND e.user_id=f.user_id AND e.ig_account_id=f.ig_account_id AND e.target_id=f.target_id
               JOIN targets t ON t.id=f.target_id AND t.user_id=f.user_id AND t.ig_account_id=f.ig_account_id
                 AND t.ig_user_id=f.owner_id
               JOIN hermes_tool_calls tc ON tc.target_id=e.target_id
                 AND tc.story_fingerprint=e.story_fingerprint AND tc.tool_name=e.action
               JOIN hermes_execution_runs r ON r.id=tc.run_id AND r.user_id=e.user_id AND r.ig_account_id=e.ig_account_id
               JOIN jobs j ON j.id=r.job_id AND j.user_id=r.user_id AND j.ig_account_id=r.ig_account_id
               JOIN ig_accounts a ON a.id=f.ig_account_id AND a.user_id=f.user_id
               JOIN agent_configs c ON c.ig_account_id=a.id AND c.user_id=a.user_id
               JOIN follow_reel_messages m ON m.relationship_follow_id=f.id AND m.user_id=f.user_id
                 AND m.ig_account_id=f.ig_account_id AND m.target_id=f.target_id AND m.recipient_id=f.owner_id
               WHERE f.id=%(relationship_id)s AND f.user_id=%(user_id)s AND f.ig_account_id=%(account_id)s
                 AND f.prerequisite_engagement_id=%(engagement_id)s AND f.status=%(status)s
                 AND f.attempted_at IS NULL AND f.follow_attempted_at IS NULL
                 AND f.confirmed_follow_at IS NULL AND f.unfollow_attempted_at IS NULL
                 AND e.action='like' AND e.status='sent' AND tc.status='confirmed'
                 AND r.status='completed' AND j.status='completed'
                 AND e.executed_at BETWEEN tc.started_at AND tc.completed_at
                 AND tc.started_at>=r.started_at AND tc.completed_at<=r.completed_at
                 AND e.executed_at BETWEEN %(since)s AND %(now)s
                 AND r.completed_at<=f.created_at AND f.created_at<=%(now)s
                 AND a.health_status='active' AND a.paused_at IS NULL AND t.enabled
                 AND c.enabled AND c.follow_after_engagement AND c.follow_reel_message_enabled
                 AND c.follow_enabled_at<=e.executed_at AND c.follow_reel_message_enabled_at<=m.created_at
                 AND m.status IN ('pending','cancelled') AND m.attempted_at IS NULL
                 AND m.sent_at IS NULL AND m.claim_count=0 AND m.created_at BETWEEN %(since)s AND %(now)s""",
        {"relationship_id":scope.relationship_follow_id,"user_id":scope.user_id,
         "account_id":scope.account_id,"engagement_id":scope.prerequisite_engagement_id,
         "since":now-timedelta(hours=24),"now":now,"status":status},
    ).fetchall()
    return rows[0]["message_id"] if len(rows) == 1 else None


@dataclass(frozen=True, slots=True)
class UnfollowStoreScope:
    """One owner's existing relationship, eligible only for a first due unfollow."""

    user_id: UUID
    account_id: UUID
    relationship_follow_id: UUID

    def __post_init__(self):
        if any(type(value) is not UUID for value in (
            self.user_id, self.account_id, self.relationship_follow_id,
        )):
            raise ValueError("unfollow scope requires UUID identifiers")


@dataclass(frozen=True, slots=True)
class FollowClaim:
    id: UUID
    user_id: UUID
    ig_account_id: UUID
    target_id: UUID
    owner_id: str
    handle: str
    action: Literal["follow", "unfollow"]
    lease_token: UUID
    followed_at: datetime | None = None
    story_reply_first: bool = False
    story_reply_engagement_id: UUID | None = None
    story_reply_job_id: UUID | None = None
    story_reply_worker_id: str | None = None


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


def _time(now):
    if now.tzinfo is None:
        raise ValueError("follow clock must be timezone aware")
    return now.astimezone(UTC)


class _ReplyReservationRejected(Exception):
    """Roll back a just-created prerequisite when the live policy changes."""


# A sent row alone is not proof: require its matching confirmed tool call and
# completed job/run, all scoped to the same tenant, sender and target.
_PREREQUISITE = """
EXISTS (
 SELECT 1 FROM story_engagements e
 JOIN hermes_tool_calls tc ON tc.target_id=e.target_id
   AND tc.story_fingerprint=e.story_fingerprint AND tc.tool_name=e.action
 JOIN hermes_execution_runs r ON r.id=tc.run_id
   AND r.user_id=e.user_id AND r.ig_account_id=e.ig_account_id
 JOIN jobs j ON j.id=r.job_id AND j.user_id=r.user_id AND j.ig_account_id=r.ig_account_id
 WHERE e.id=f.prerequisite_engagement_id AND e.user_id=f.user_id
   AND e.ig_account_id=f.ig_account_id AND e.target_id=f.target_id
   AND e.status='sent' AND e.action IN ('like','reply')
   AND tc.status='confirmed' AND r.status='completed' AND j.status='completed'
   AND e.executed_at >= c.follow_enabled_at
)
"""

_REPLY_PREREQUISITE = """
EXISTS (
 SELECT 1 FROM story_engagements e
 JOIN hermes_tool_calls tc ON tc.target_id=e.target_id
   AND tc.story_fingerprint=e.story_fingerprint AND tc.tool_name=e.action
 JOIN hermes_execution_runs r ON r.id=tc.run_id
   AND r.user_id=e.user_id AND r.ig_account_id=e.ig_account_id
 JOIN jobs j ON j.id=r.job_id AND j.user_id=r.user_id
   AND j.ig_account_id=r.ig_account_id
 WHERE e.id=%(reply_engagement_id)s AND e.user_id=f.user_id
   AND e.ig_account_id=f.ig_account_id AND e.target_id=f.target_id
   AND e.action='reply' AND e.status='skipped' AND e.executed_at IS NULL
   AND tc.status='started'
   AND tc.safe_result->>'phase'='execution_started'
   AND r.status='running' AND j.id=%(reply_job_id)s
   AND j.claimed_by=%(reply_worker_id)s AND j.status='running'
   AND j.lease_expires_at>%(now)s
)
"""


class PostgresFollowStore:
    def __init__(self, pool: ConnectionPool, *, lease_seconds: int = 120,
                 scope: FollowStoreScope | PendingLikeFollowScope | UnfollowStoreScope | None = None):
        if not 10 <= lease_seconds <= 600:
            raise ValueError("follow lease must be between 10 and 600 seconds")
        if scope is not None and type(scope) not in {FollowStoreScope, PendingLikeFollowScope, UnfollowStoreScope}:
            raise ValueError("invalid follow scope")
        self._pool = pool
        self._lease_seconds = lease_seconds
        self._scope = scope

    @property
    def scope(self):
        return self._scope

    def _scope_filter(self, alias, engagement_field):
        if self._scope is None:
            return "", ()
        unfollow = type(self._scope) is UnfollowStoreScope
        restriction, params = (
            (
                f" AND {alias}.user_id=%s AND {alias}.ig_account_id=%s"
                f" AND {alias}.{'id' if unfollow else engagement_field}=%s"
            ),
            (self._scope.user_id, self._scope.account_id,
             self._scope.relationship_follow_id if unfollow
             else self._scope.prerequisite_engagement_id),
        )
        if type(self._scope) is PendingLikeFollowScope:
            restriction += f" AND {alias}.id=%s"
            params += (self._scope.relationship_follow_id,)
        return restriction, params

    async def enqueue_confirmed(self, now: datetime) -> int:
        return await asyncio.to_thread(self._enqueue, _time(now))

    async def reserve_story_reply_follow(
        self,
        *,
        user_id: UUID,
        account_id: UUID,
        target_id: UUID,
        owner_id: str,
        engagement_id: UUID,
        job_id: UUID,
        worker_id: str,
        now: datetime,
    ) -> FollowClaim | None:
        """Reserve the follow that must precede one Story reply.

        The normal maintenance lane deliberately waits for a completed Story
        job.  A reply-first policy cannot use that lane: the current job must
        own a follow lease while its Story engagement is still reserved, then
        the reply may proceed only after the relationship receipt is durable.
        """
        if type(worker_id) is not str or not 1 <= len(worker_id) <= 200:
            raise ValueError("invalid follow worker id")
        if type(owner_id) is not str or not owner_id.isdecimal() or len(owner_id) > 32:
            raise ValueError("invalid follow owner id")
        return await asyncio.to_thread(
            self._reserve_story_reply_follow,
            user_id,
            account_id,
            target_id,
            owner_id,
            engagement_id,
            job_id,
            worker_id,
            _time(now),
        )

    def _reserve_story_reply_follow(
        self, user_id, account_id, target_id, owner_id, engagement_id,
        job_id, worker_id, now,
    ):
        try:
            with (
                self._pool.connection() as conn,
                conn.transaction(),
                conn.cursor(row_factory=dict_row) as cur,
            ):
                row = cur.execute(
                    """
                    SELECT a.health_status,a.paused_at,a.ig_user_id,
                           c.enabled,c.follow_after_engagement,c.follow_enabled_at,
                           c.follow_reel_message_enabled,c.follow_reel_message_enabled_at,
                           t.username,t.enabled AS target_enabled
                    FROM ig_accounts a
                    JOIN agent_configs c ON c.user_id=a.user_id AND c.ig_account_id=a.id
                    JOIN targets t ON t.id=%s AND t.user_id=a.user_id AND t.ig_account_id=a.id
                    WHERE a.id=%s AND a.user_id=%s AND t.ig_user_id=%s
                    FOR UPDATE OF a,c,t
                    """,
                    (target_id, account_id, user_id, owner_id),
                ).fetchone()
                if row is None or row["ig_user_id"] is None:
                    return None
                if (
                    row["health_status"] not in ("active", b"active")
                    or row["paused_at"] is not None
                    or row["enabled"] is not True
                    or row["follow_after_engagement"] is not True
                    or row["follow_enabled_at"] is None
                    or row["follow_enabled_at"] > now
                    or row["target_enabled"] is not True
                    or _text(row["ig_user_id"]).strip() == owner_id
                ):
                    return None
                exact = cur.execute(
                    """
                    SELECT 1
                    FROM story_engagements e
                    JOIN hermes_tool_calls tc ON tc.target_id=e.target_id
                      AND tc.story_fingerprint=e.story_fingerprint
                      AND tc.tool_name=e.action
                    JOIN hermes_execution_runs r ON r.id=tc.run_id
                      AND r.user_id=e.user_id AND r.ig_account_id=e.ig_account_id
                    JOIN jobs j ON j.id=r.job_id AND j.user_id=r.user_id
                      AND j.ig_account_id=r.ig_account_id
                    WHERE e.id=%s AND e.user_id=%s AND e.ig_account_id=%s
                      AND e.target_id=%s AND e.action='reply'
                      AND e.status='skipped' AND e.executed_at IS NULL
                      AND tc.status='started'
                      AND tc.safe_result->>'phase'='execution_started'
                      AND r.status='running' AND j.id=%s
                      AND j.claimed_by=%s AND j.status='running'
                      AND j.lease_expires_at>clock_timestamp()
                    """,
                    (engagement_id, user_id, account_id, target_id, job_id, worker_id),
                ).fetchone()
                if exact is None:
                    return None
                existing = cur.execute(
                    """SELECT 1 FROM relationship_follows
                       WHERE user_id=%s AND ig_account_id=%s AND owner_id=%s
                       FOR UPDATE""",
                    (user_id, account_id, owner_id),
                ).fetchone()
                if existing is not None:
                    return None
                inserted = cur.execute(
                    """
                    INSERT INTO relationship_follows
                      (user_id,ig_account_id,target_id,owner_id,prerequisite_engagement_id,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (ig_account_id,owner_id) DO NOTHING
                    RETURNING id
                    """,
                    (user_id, account_id, target_id, owner_id, engagement_id, now, now),
                ).fetchone()
                if inserted is None:
                    return None
                token = uuid4()
                cur.execute(
                    """
                    UPDATE relationship_follows
                    SET status='follow_running',claimed_by=%s,lease_token=%s,
                        lease_expires_at=%s,follow_reserved_at=%s,updated_at=%s
                    WHERE id=%s AND user_id=%s AND ig_account_id=%s
                    """,
                    (worker_id, token, now + timedelta(seconds=self._lease_seconds), now, now,
                     inserted["id"], user_id, account_id),
                )
                claim = FollowClaim(
                    inserted["id"], user_id, account_id, target_id, owner_id,
                    _text(row["username"]), "follow", token, None, True,
                    engagement_id, job_id, worker_id,
                )
                policy_row = {"id": inserted["id"], "user_id": user_id,
                              "ig_account_id": account_id}
                if not self._policy(cur, policy_row, "follow", now, reply_claim=claim):
                    raise _ReplyReservationRejected
                if (
                    row["follow_reel_message_enabled"]
                    and row["follow_reel_message_enabled_at"] is not None
                    and row["follow_reel_message_enabled_at"] <= now
                ):
                    cur.execute(
                        """
                        INSERT INTO follow_reel_messages
                          (relationship_follow_id,user_id,ig_account_id,target_id,recipient_id,reserved_at,created_at,updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (inserted["id"],user_id,account_id,target_id,owner_id,now,now,now),
                    )
                return claim
        except _ReplyReservationRejected:
            return None

    async def claim_next(self, worker_id: str, now: datetime) -> FollowClaim | None:
        if not worker_id or len(worker_id) > 200:
            raise ValueError("invalid follow worker id")
        return await asyncio.to_thread(self._claim, worker_id, _time(now))

    async def mark_attempting(self, claim: FollowClaim, now: datetime) -> bool:
        return await asyncio.to_thread(self._attempt, claim, _time(now))

    async def reply_ready(self, claim: FollowClaim, now: datetime) -> bool:
        """Check the follow receipt and Story lease immediately before send."""
        if not claim.story_reply_first:
            return False
        return await asyncio.to_thread(self._reply_ready, claim, _time(now))

    def _reply_ready(self, claim, now):
        if (
            claim.story_reply_engagement_id is None
            or claim.story_reply_job_id is None
            or claim.story_reply_worker_id is None
        ):
            return False
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM relationship_follows f
                JOIN ig_accounts a ON a.id=f.ig_account_id AND a.user_id=f.user_id
                JOIN agent_configs c ON c.user_id=f.user_id AND c.ig_account_id=f.ig_account_id
                JOIN story_engagements e ON e.id=%s AND e.user_id=f.user_id
                 AND e.ig_account_id=f.ig_account_id AND e.target_id=f.target_id
                JOIN hermes_tool_calls tc ON tc.target_id=e.target_id
                 AND tc.story_fingerprint=e.story_fingerprint AND tc.tool_name=e.action
                JOIN hermes_execution_runs r ON r.id=tc.run_id AND r.user_id=e.user_id
                 AND r.ig_account_id=e.ig_account_id
                JOIN jobs j ON j.id=r.job_id AND j.user_id=r.user_id
                 AND j.ig_account_id=r.ig_account_id
                WHERE f.id=%s AND f.user_id=%s AND f.ig_account_id=%s
                  AND f.target_id=%s AND f.owner_id=%s
                  AND f.status='following' AND f.confirmed_follow_at IS NOT NULL
                  AND a.health_status='active' AND a.paused_at IS NULL
                  AND c.enabled AND c.follow_after_engagement
                  AND e.action='reply' AND e.status='skipped' AND e.executed_at IS NULL
                  AND tc.status='started'
                  AND tc.safe_result->>'phase'='execution_started'
                  AND r.status='running'
                  AND j.id=%s AND j.claimed_by=%s AND j.status='running'
                  AND j.lease_expires_at>%s
                  AND NOT EXISTS (
                    SELECT 1 FROM instagram_execution_controls x
                    WHERE x.blocked AND (
                      x.scope='system' OR (x.scope='action' AND x.action='reply')
                      OR (x.scope='tenant' AND x.user_id=f.user_id)
                      OR (x.scope='account' AND x.user_id=f.user_id
                          AND x.ig_account_id=f.ig_account_id)
                    )
                  )
                """,
                (
                    claim.story_reply_engagement_id,
                    claim.id,
                    claim.user_id,
                    claim.ig_account_id,
                    claim.target_id,
                    claim.owner_id,
                    claim.story_reply_job_id,
                    claim.story_reply_worker_id,
                    now,
                ),
            ).fetchone()
        return row is not None

    async def finish(
        self, claim: FollowClaim, outcome: str, now: datetime, evidence: dict
    ) -> bool:
        if outcome not in {
            "confirmed",
            "already_following",
            "already_absent",
            "uncertain",
            "deferred",
        }:
            raise ValueError("invalid relationship outcome")
        if not isinstance(evidence, dict):
            raise TypeError("relationship evidence must be an object")
        if (outcome == "already_following" and claim.action != "follow") or (
            outcome == "already_absent" and claim.action != "unfollow"
        ):
            raise ValueError("relationship outcome does not match action")
        return await asyncio.to_thread(
            self._finish, claim, outcome, _time(now), evidence
        )

    def _enqueue(self, now):
        if type(self._scope) in {UnfollowStoreScope, PendingLikeFollowScope}:
            return 0
        restriction, scope_params = self._scope_filter("e", "id")
        if self._scope is not None:
            restriction += " AND e.action='reply'"
        with self._pool.connection() as conn:
            result = conn.execute(
                """
                INSERT INTO relationship_follows
                  (user_id,ig_account_id,target_id,owner_id,prerequisite_engagement_id,created_at,updated_at)
                SELECT DISTINCT ON (e.ig_account_id,t.ig_user_id)
                  e.user_id,e.ig_account_id,e.target_id,t.ig_user_id,e.id,%s,%s
                FROM story_engagements e
                JOIN targets t ON t.id=e.target_id AND t.user_id=e.user_id AND t.ig_account_id=e.ig_account_id
                JOIN ig_accounts a ON a.id=e.ig_account_id AND a.user_id=e.user_id
                JOIN agent_configs c ON c.ig_account_id=a.id AND c.user_id=a.user_id
                JOIN hermes_tool_calls tc ON tc.target_id=e.target_id
                  AND tc.story_fingerprint=e.story_fingerprint AND tc.tool_name=e.action
                JOIN hermes_execution_runs r ON r.id=tc.run_id AND r.user_id=e.user_id AND r.ig_account_id=e.ig_account_id
                JOIN jobs j ON j.id=r.job_id AND j.user_id=r.user_id AND j.ig_account_id=r.ig_account_id
                WHERE c.enabled AND c.follow_after_engagement AND c.follow_enabled_at IS NOT NULL
                  AND a.health_status='active' AND a.paused_at IS NULL AND t.enabled
                  AND t.ig_user_id ~ '^[0-9]{1,32}$'
                  AND e.status='sent' AND e.action IN ('like','reply') AND tc.status='confirmed'
                  AND r.status='completed' AND j.status='completed'
                  AND e.executed_at >= c.follow_enabled_at AND e.executed_at <= %s
                """ + restriction + """
                ORDER BY e.ig_account_id,t.ig_user_id,e.executed_at,e.id
                ON CONFLICT (ig_account_id,owner_id) DO NOTHING
            """,
                (now, now, now) + scope_params,
            )
            return result.rowcount

    @staticmethod
    def _params(claim, now):
        if claim.action not in {"follow", "unfollow"}:
            raise ValueError("invalid relationship action")
        return {
            "id": claim.id,
            "user": claim.user_id,
            "account": claim.ig_account_id,
            "target": claim.target_id,
            "owner": claim.owner_id,
            "token": claim.lease_token,
            "status": claim.action + "_running",
            "action": claim.action,
            "now": now,
        }

    def _locked(self, cursor, params):
        restriction = ""
        if self._scope is not None:
            unfollow = type(self._scope) is UnfollowStoreScope
            restriction = """ AND f.user_id=%(scope_user)s
              AND f.ig_account_id=%(scope_account)s
              """ + ("AND f.id=%(scope_selected)s AND %(action)s='unfollow'" if unfollow else
                        "AND f.prerequisite_engagement_id=%(scope_selected)s AND %(action)s='follow'")
            params = {**params, "scope_user": self._scope.user_id,
                      "scope_account": self._scope.account_id,
                      "scope_selected": self._scope.relationship_follow_id if unfollow
                      else self._scope.prerequisite_engagement_id}
            if type(self._scope) is PendingLikeFollowScope:
                restriction += " AND f.id=%(scope_relationship)s"
                params["scope_relationship"] = self._scope.relationship_follow_id
        cursor.execute(
            """
            SELECT f.* FROM relationship_follows f
            JOIN ig_accounts a ON a.id=f.ig_account_id AND a.user_id=f.user_id
            WHERE f.id=%(id)s AND f.user_id=%(user)s AND f.ig_account_id=%(account)s
              AND f.target_id=%(target)s AND f.owner_id=%(owner)s
              AND f.lease_token=%(token)s AND f.status=%(status)s AND f.lease_expires_at>%(now)s
            """ + restriction + """
            FOR UPDATE OF a,f
        """,
            params,
        )
        return cursor.fetchone()

    @staticmethod
    def _policy(cursor, row, action, now, *, reply_claim=None):
        params = {
            "id": row["id"],
            "now": now,
            "action": action,
            "events": [WARMUP_RESET_EVENT],
        }
        prerequisite = _REPLY_PREREQUISITE if reply_claim is not None else _PREREQUISITE
        running_jobs = (
            """AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.user_id=a.user_id
                AND j.ig_account_id=a.id AND j.id<>%(reply_job_id)s
                AND j.status='running' AND j.lease_expires_at>%(now)s)"""
            if reply_claim is not None
            else
            """AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.user_id=a.user_id
                AND j.ig_account_id=a.id AND j.status='running'
                AND j.lease_expires_at>%(now)s)"""
        )
        if reply_claim is not None:
            params.update(
                reply_engagement_id=reply_claim.story_reply_engagement_id,
                reply_job_id=reply_claim.story_reply_job_id,
                reply_worker_id=reply_claim.story_reply_worker_id,
            )
        cursor.execute(
            """
            SELECT (SELECT u.plan FROM users u WHERE u.id=a.user_id) AS plan,c.daily_follow_limit,c.daily_reply_limit,c.daily_dm_limit,c.daily_reel_message_limit,c.warmup_exempt,c.timezone,c.follow_reel_message_enabled,a.warmup_started_at,
              (SELECT max(h.created_at) FROM account_health_events h
                WHERE h.user_id=a.user_id AND h.ig_account_id=a.id
                  AND h.event_type=ANY(%(events)s)) AS restricted_at
            FROM relationship_follows f
            JOIN ig_accounts a ON a.id=f.ig_account_id AND a.user_id=f.user_id
            JOIN agent_configs c ON c.ig_account_id=a.id AND c.user_id=a.user_id
            JOIN targets t ON t.id=f.target_id AND t.user_id=f.user_id AND t.ig_account_id=f.ig_account_id
            WHERE f.id=%(id)s AND a.health_status='active' AND a.paused_at IS NULL AND c.enabled
              AND t.ig_user_id=f.owner_id
              AND (%(action)s<>'unfollow' OR (c.auto_unfollow_enabled
                AND f.confirmed_follow_at IS NOT NULL AND f.unfollow_due_at<=%(now)s))
              AND (%(action)s='unfollow' OR (t.enabled AND c.follow_after_engagement
                AND c.follow_enabled_at IS NOT NULL AND """
            + prerequisite
            + """))
              AND EXISTS (SELECT 1 FROM proxy_bindings b JOIN proxies p ON p.id=b.proxy_id
                WHERE b.user_id=a.user_id AND b.ig_account_id=a.id
                  AND b.released_at IS NULL AND p.health_status='active')
              """
            + running_jobs
            + """
              AND NOT EXISTS (SELECT 1 FROM relationship_follows other WHERE other.ig_account_id=a.id
                AND other.user_id=a.user_id AND other.id<>f.id AND other.status IN ('follow_running','unfollow_running')
                AND other.lease_expires_at>%(now)s)
              AND NOT EXISTS (SELECT 1 FROM follow_reel_messages message WHERE message.ig_account_id=a.id
                AND message.user_id=a.user_id AND message.status='running' AND message.lease_expires_at>%(now)s)
              AND NOT EXISTS (SELECT 1 FROM relationship_reconciliation_attempts observation
                WHERE observation.ig_account_id=a.id AND observation.user_id=a.user_id
                  AND observation.outcome='pending' AND observation.lease_expires_at>%(now)s)
              AND NOT EXISTS (SELECT 1 FROM instagram_execution_controls x WHERE x.blocked AND
                (x.scope='system' OR (x.scope='action' AND x.action=%(action)s)
                OR (x.scope='tenant' AND x.user_id=a.user_id)
                OR (x.scope='account' AND x.user_id=a.user_id AND x.ig_account_id=a.id)))
            FOR SHARE OF c,t
        """,
            params,
        )
        policy = cursor.fetchone()
        if not policy:
            return False
        cap = 20
        if action == "follow":
            start = policy["warmup_started_at"]
            if start is None or start > now:
                return False
            start = max(start, policy["restricted_at"] or start)
            days = (now - start).total_seconds() / 86400
            if days < 0:
                return False
            warmup = next(
                (
                    limit
                    for age, limit in ((3, 3), (7, 5), (14, 8), (30, 12))
                    if days < age
                ),
                20,
            )
            cap = min(policy["daily_follow_limit"], 20 if policy["warmup_exempt"] else warmup)
        # Field names are selected from the closed action enum, never user SQL.
        field = action + "_reserved_at"
        attempted = action + "_attempted_at"
        cursor.execute(
            f"""
            SELECT count(*) AS used FROM relationship_follows
            WHERE user_id=%s AND ig_account_id=%s AND id<>%s
              AND ( {field} >= %s OR {attempted} >= %s )
        """,
            (
                row["user_id"],
                row["ig_account_id"],
                row["id"],
                daily_window_start(now, _text(policy["timezone"])),
                daily_window_start(now, _text(policy["timezone"])),
            ),
        )
        used = cursor.fetchone()["used"]
        if cap <= 0 or used >= cap:
            return False
        if action != "follow" or not policy["follow_reel_message_enabled"]:
            return True
        return reel_capacity(cursor, policy, row["user_id"], row["ig_account_id"], now,
                             relationship_id=row["id"])

    def _claim(self, worker_id, now):
        restriction, scope_params = self._scope_filter("f", "prerequisite_engagement_id")
        expired_restriction = restriction
        candidate_restriction = restriction
        if type(self._scope) is UnfollowStoreScope:
            expired_restriction += " AND f.status='unfollow_running'"
            # attempted_at retains the original follow until this claim starts.
            # Only the action-specific unfollow timestamp blocks its first try.
            candidate_restriction += """ AND f.status='following'
              AND f.confirmed_follow_at IS NOT NULL AND f.unfollowed_at IS NULL
              AND f.unfollow_attempted_at IS NULL"""
        elif type(self._scope) is PendingLikeFollowScope:
            # The shared selector rechecks eligibility after normal policy locks.
            # Never recover expired claims or enqueue another relationship.
            expired_restriction += " AND false"
            candidate_restriction += """ AND f.status='follow_pending'
              AND f.follow_attempted_at IS NULL AND f.attempted_at IS NULL"""
        elif self._scope is not None:
            expired_restriction += " AND f.status='follow_running'"
            candidate_restriction += """ AND f.status='follow_pending'
              AND f.follow_attempted_at IS NULL AND f.attempted_at IS NULL
              AND EXISTS (SELECT 1 FROM story_engagements e
                WHERE e.id=f.prerequisite_engagement_id AND e.user_id=f.user_id
                  AND e.ig_account_id=f.ig_account_id AND e.action='reply')
              AND NOT EXISTS (SELECT 1 FROM follow_reel_messages m
                WHERE m.relationship_follow_id=f.id
                  AND (m.status<>'pending' OR m.attempted_at IS NOT NULL))"""
        with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                """
                WITH expired AS (
                  SELECT f.id,f.user_id,f.ig_account_id FROM relationship_follows f
                  JOIN ig_accounts a ON a.id=f.ig_account_id AND a.user_id=f.user_id
                  WHERE f.status IN ('follow_running','unfollow_running') AND f.lease_expires_at<=%s
                  """ + expired_restriction + """
                  ORDER BY a.id,f.id FOR UPDATE OF a,f SKIP LOCKED
                )
                UPDATE relationship_follows f SET status='uncertain',last_error='lease_expired',
                  claimed_by=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=%s
                FROM expired e WHERE f.id=e.id AND f.user_id=e.user_id AND f.ig_account_id=e.ig_account_id
            """,
                (now,) + scope_params + (now,),
            )
            cursor.execute(
                """
                SELECT f.*,t.username AS handle FROM relationship_follows f
                JOIN ig_accounts a ON a.id=f.ig_account_id AND a.user_id=f.user_id
                JOIN targets t ON t.id=f.target_id AND t.user_id=f.user_id AND t.ig_account_id=f.ig_account_id
                WHERE (f.status='follow_pending' OR (f.status='following' AND f.unfollow_due_at<=%s))
                  AND (f.next_attempt_at IS NULL OR f.next_attempt_at<=%s)
                """ + candidate_restriction + """
                ORDER BY (f.status='following') DESC,f.created_at,f.id
                FOR UPDATE OF a,f SKIP LOCKED
            """,
                (now, now) + scope_params,
            )
            rows = cursor.fetchall()
            for row in rows:
                action = (
                    "follow" if _text(row["status"]) == "follow_pending" else "unfollow"
                )
                if not self._policy(cursor, row, action, now):
                    continue
                if not self._pending_like_eligible(cursor, row, now):
                    continue
                token = uuid4()
                cursor.execute(
                    f"""
                    UPDATE relationship_follows SET status=%s,claimed_by=%s,lease_token=%s,
                      lease_expires_at=%s,attempted_at=NULL,{action}_reserved_at=%s,
                      next_attempt_at=NULL,updated_at=%s WHERE id=%s AND user_id=%s AND ig_account_id=%s
                """,
                    (
                        action + "_running",
                        worker_id,
                        token,
                        now + timedelta(seconds=self._lease_seconds),
                        now,
                        now,
                        row["id"],
                        row["user_id"],
                        row["ig_account_id"],
                    ),
                )
                if action == "follow":
                    # Normalize legacy SQL_ASCII text decoding before writing
                    # the ledger; the subsequent browser checks use this same
                    # canonical numeric recipient id.
                    recipient_id = _text(row["owner_id"])
                    cursor.execute(
                        """INSERT INTO follow_reel_messages
                          (relationship_follow_id,user_id,ig_account_id,target_id,recipient_id,reserved_at,created_at,updated_at)
                          SELECT %s,%s,%s,%s,%s,%s,%s,%s
                          WHERE EXISTS (SELECT 1 FROM agent_configs c WHERE c.user_id=%s AND c.ig_account_id=%s
                            AND c.follow_reel_message_enabled AND c.follow_reel_message_enabled_at<=%s)
                          ON CONFLICT (relationship_follow_id) DO UPDATE
                          SET status='pending',reserved_at=EXCLUDED.reserved_at,
                              updated_at=EXCLUDED.updated_at
                          WHERE follow_reel_messages.status='cancelled'
                            AND follow_reel_messages.attempted_at IS NULL""",
                        (row["id"],row["user_id"],row["ig_account_id"],row["target_id"],
                         recipient_id,now,now,now,row["user_id"],row["ig_account_id"],now),
                    )
                return FollowClaim(
                    row["id"],
                    row["user_id"],
                    row["ig_account_id"],
                    row["target_id"],
                    _text(row["owner_id"]),
                    _text(row["handle"]),
                    action,
                    token,
                    row["confirmed_follow_at"],
                )
        return None

    def _pending_like_eligible(self, cursor, row, now):
        return type(self._scope) is not PendingLikeFollowScope or pending_like_message(
            cursor,self._scope,now,status=_text(row['status']),
        ) is not None

    def _attempt(self, claim, now):
        params = self._params(claim, now)
        with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cursor,
        ):
            row = self._locked(cursor, params)
            if not row or row["attempted_at"] is not None:
                return False
            # _locked may wait for another transaction. Validate current time
            # only after its account and relationship locks have been acquired.
            cursor.execute("SELECT clock_timestamp() AS current_time")
            current_time = max(now, cursor.fetchone()["current_time"])
            if row["lease_expires_at"] <= current_time:
                return False
            permitted = self._policy(
                cursor,
                row,
                claim.action,
                current_time,
                reply_claim=claim if claim.story_reply_first else None,
            )
            if not permitted:
                return False
            cursor.execute("SELECT clock_timestamp() AS current_time")
            params["now"] = max(now, cursor.fetchone()["current_time"])
            if not self._pending_like_eligible(cursor, row, params["now"]):
                return False
            cursor.execute(
                f"""
                UPDATE relationship_follows SET attempted_at=%(now)s,{claim.action}_attempted_at=%(now)s,
                  updated_at=%(now)s WHERE id=%(id)s AND user_id=%(user)s AND ig_account_id=%(account)s
                    AND lease_token=%(token)s AND status=%(status)s AND attempted_at IS NULL
                    AND lease_expires_at>clock_timestamp()
            """,
                params,
            )
            return cursor.rowcount == 1

    def _finish(self, claim, outcome, now, evidence):
        params = self._params(claim, now)
        with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cursor,
        ):
            row = self._locked(cursor, params)
            if not row or (outcome == "confirmed" and row["attempted_at"] is None):
                return False
            status = "uncertain"
            followed, due, unfollowed = (
                row["confirmed_follow_at"],
                row["unfollow_due_at"],
                row["unfollowed_at"],
            )
            if outcome == "deferred":
                # Once a mutation began, a retry cannot be justified by a later error.
                if row["attempted_at"] is not None:
                    outcome = "uncertain"
                else:
                    status = (
                        "follow_pending" if claim.action == "follow" else "following"
                    )
            elif outcome == "already_following":
                status = "skipped"
            elif outcome in {"confirmed", "already_absent"}:
                if claim.action == "follow":
                    status, followed, due = "following", now, now + timedelta(hours=120)
                else:
                    status, unfollowed = "unfollowed", now
            cursor.execute(
                """
                UPDATE relationship_follows SET status=%(final_status)s,confirmed_follow_at=%(followed)s,
                  unfollow_due_at=%(due)s,unfollowed_at=%(unfollowed)s,claimed_by=NULL,lease_token=NULL,
                  lease_expires_at=NULL,next_attempt_at=%(next)s,last_error=%(error)s,
                  evidence=evidence || %(evidence)s,updated_at=%(now)s
                WHERE id=%(id)s AND user_id=%(user)s AND ig_account_id=%(account)s
            """,
                {
                    **params,
                    "final_status": status,
                    "followed": followed,
                    "due": due,
                    "unfollowed": unfollowed,
                    "next": now + timedelta(minutes=5)
                    if outcome == "deferred"
                    else None,
                    "error": "uncertain" if status == "uncertain" else None,
                    "evidence": Jsonb({claim.action: {**evidence, "outcome": outcome, "observed_at": now.isoformat()}}),
                },
            )
            if (claim.action == "unfollow" and evidence.get("identity_matches") is True
                    and evidence.get("followed_by") is True
                    and not evidence.get("health_signal")):
                cursor.execute("""INSERT INTO relationship_followback_observations
                    (relationship_id,observed_at)
                    SELECT id,%(now)s FROM relationship_follows
                    WHERE id=%(id)s AND user_id=%(user)s AND ig_account_id=%(account)s
                    AND confirmed_follow_at < %(now)s
                    AND evidence #>> '{follow,outcome}'='confirmed'
                    AND evidence #>> '{follow,followed_by}'='false'
                    ON CONFLICT DO NOTHING""", params)
            if claim.action == "follow":
                cursor.execute("""SELECT m.id FROM follow_reel_messages m JOIN agent_configs c
                    ON c.user_id=m.user_id AND c.ig_account_id=m.ig_account_id
                    WHERE m.relationship_follow_id=%s AND m.status='pending'
                    AND c.follow_reel_message_enabled AND c.follow_reel_message_enabled_at<=m.reserved_at""", (claim.id,))
                reserved = cursor.fetchone() is not None
                if status == "following" and reserved:
                    cursor.execute(
                        """INSERT INTO follow_reel_rotation (user_id,ig_account_id,next_slot)
                           VALUES (%s,%s,1)
                           ON CONFLICT (user_id,ig_account_id) DO UPDATE
                           SET next_slot=follow_reel_rotation.next_slot+1
                           RETURNING next_slot-1 AS slot""",
                        (claim.user_id,claim.ig_account_id),
                    )
                    slot = cursor.fetchone()["slot"]
                    cursor.execute(
                        """UPDATE follow_reel_messages
                           SET rotation_slot=%s,updated_at=%s
                           WHERE relationship_follow_id=%s AND status='pending'""",
                        (slot,now,claim.id),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("confirmed follow has no message reservation")
                else:
                    cursor.execute(
                        """UPDATE follow_reel_messages SET status=%s,
                           reserved_at=CASE WHEN %s='cancelled' THEN NULL ELSE reserved_at END,
                           updated_at=%s
                           WHERE relationship_follow_id=%s AND status='pending'""",
                        ("uncertain" if status == "uncertain" else "cancelled",
                         "uncertain" if status == "uncertain" else "cancelled",now,claim.id),
                    )
            return True
