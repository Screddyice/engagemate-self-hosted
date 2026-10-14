"""Durable, non-billable reservations for bounded relationship observations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from engagemate.db.text import text


@dataclass(frozen=True, slots=True)
class ReconciliationClaim:
    id: UUID
    user_id: UUID
    ig_account_id: UUID
    relationship_id: UUID
    owner_id: str
    handle: str
    followed_at: datetime
    cached_observation: dict | None = None


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("reconciliation clock must be timezone aware")
    return value.astimezone(UTC)


def observation(value, owner, followed_at, now):
    """Accept exact-identity, typed observations strictly after our follow."""
    if not isinstance(value, dict):
        return None
    try:
        observed = _time(
            datetime.fromisoformat(value.get("observed_at", "").replace("Z", "+00:00"))
        )
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if (
        value.get("owner_id") != owner
        or value.get("identity_matches") is not True
        or type(value.get("followed_by")) is not bool
        or not followed_at < observed <= now
    ):
        return None
    return {
        "owner_id": owner,
        "identity_matches": True,
        "followed_by": value["followed_by"],
        "observed_at": observed.isoformat(),
        "source": "profile_reconciliation",
    }


_POLICY = """
a.health_status='active' AND a.paused_at IS NULL AND c.enabled
AND EXISTS (SELECT 1 FROM proxy_bindings b JOIN proxies p ON p.id=b.proxy_id
 WHERE b.user_id=a.user_id AND b.ig_account_id=a.id AND b.released_at IS NULL AND p.health_status='active')
AND NOT EXISTS (SELECT 1 FROM follow_reel_messages m WHERE m.user_id=a.user_id AND m.ig_account_id=a.id
  AND m.status='running' AND m.lease_expires_at>%(now)s)
AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.user_id=a.user_id AND j.ig_account_id=a.id
 AND j.status='running' AND j.lease_expires_at>%(now)s)
AND NOT EXISTS (SELECT 1 FROM relationship_follows busy WHERE busy.user_id=a.user_id AND busy.ig_account_id=a.id
 AND busy.status IN ('follow_running','unfollow_running') AND busy.lease_expires_at>%(now)s)
AND NOT EXISTS (SELECT 1 FROM instagram_execution_controls x WHERE x.blocked AND
 (x.scope='system' OR x.scope='action' OR (x.scope='tenant' AND x.user_id=a.user_id)
 OR (x.scope='account' AND x.user_id=a.user_id AND x.ig_account_id=a.id)))
"""
_ELIGIBLE = """
f.confirmed_follow_at BETWEEN %(since)s AND %(mature)s
AND f.evidence #>> '{follow,outcome}'='confirmed'
AND f.evidence #> '{follow,followed_by}'='false'::jsonb
AND t.ig_user_id=f.owner_id
"""


class PostgresReconciliationStore:
    def __init__(self, pool):
        self.pool = pool

    async def claim_next(self, worker_id, now):
        if not worker_id or len(worker_id) > 200:
            raise ValueError("invalid worker id")
        return await asyncio.to_thread(self._claim, worker_id, _time(now))

    def _claim(self, worker_id, now):
        params = {
            "now": now,
            "since": now - timedelta(days=30),
            "mature": now - timedelta(hours=72),
            "midnight": now.replace(hour=0, minute=0, second=0, microsecond=0),
        }
        with self.pool.connection() as conn:
            row = conn.execute(
                """
SELECT f.*,t.username FROM relationship_follows f
JOIN ig_accounts a ON a.id=f.ig_account_id AND a.user_id=f.user_id
JOIN agent_configs c ON c.ig_account_id=a.id AND c.user_id=a.user_id
JOIN targets t ON t.id=f.target_id AND t.user_id=f.user_id AND t.ig_account_id=f.ig_account_id
WHERE """
                + _POLICY
                + " AND "
                + _ELIGIBLE
                + """
AND NOT EXISTS (SELECT 1 FROM relationship_reconciliation_attempts r WHERE r.relationship_id=f.id
 AND (r.outcome='follow_back' OR COALESCE(r.next_check_at,r.reserved_at+interval '18 hours')>%(now)s))
AND NOT EXISTS (SELECT 1 FROM relationship_reconciliation_attempts r WHERE r.user_id=a.user_id AND r.ig_account_id=a.id
 AND r.outcome='pending' AND r.lease_expires_at>%(now)s)
AND (SELECT count(*) FROM relationship_reconciliation_attempts r WHERE r.user_id=a.user_id AND r.ig_account_id=a.id
 AND r.reserved_at>=%(midnight)s AND r.reserved_at<=%(now)s)<30
ORDER BY (SELECT max(r.reserved_at) FROM relationship_reconciliation_attempts r WHERE r.relationship_id=f.id) NULLS FIRST,
 f.confirmed_follow_at,f.id
FOR UPDATE OF a SKIP LOCKED LIMIT 1
""",
                params,
            ).fetchone()
            if not row:
                return None
            # Recheck after the account lock: a concurrent claim can have committed
            # after this SELECT's snapshot began. READ COMMITTED gives this a fresh snapshot.
            if not conn.execute(
                """SELECT 1 FROM relationship_follows f
 JOIN ig_accounts a ON a.id=f.ig_account_id AND a.user_id=f.user_id
 JOIN agent_configs c ON c.ig_account_id=a.id AND c.user_id=a.user_id
 JOIN targets t ON t.id=f.target_id AND t.user_id=f.user_id AND t.ig_account_id=f.ig_account_id
 WHERE f.id=%(relationship_id)s AND """
                + _POLICY
                + " AND "
                + _ELIGIBLE,
                {**params, "relationship_id": row["id"]},
            ).fetchone():
                return None
            if (
                conn.execute(
                    """SELECT count(*) AS n FROM relationship_reconciliation_attempts
 WHERE user_id=%s AND ig_account_id=%s AND
 ((reserved_at>=%s AND reserved_at<=%s) OR (outcome='pending' AND lease_expires_at>%s))""",
                    (
                        row["user_id"],
                        row["ig_account_id"],
                        params["midnight"],
                        now,
                        now,
                    ),
                ).fetchone()["n"]
                >= 30
            ):
                return None
            if conn.execute(
                """SELECT 1 FROM relationship_reconciliation_attempts WHERE user_id=%s AND ig_account_id=%s
 AND ((outcome='pending' AND lease_expires_at>%s) OR (relationship_id=%s AND (outcome='follow_back' OR COALESCE(next_check_at,reserved_at+interval '18 hours')>%s))) LIMIT 1""",
                (row["user_id"], row["ig_account_id"], now, row["id"], now),
            ).fetchone():
                return None
            claim_id = uuid4()
            conn.execute(
                """INSERT INTO relationship_reconciliation_attempts
 (id,user_id,ig_account_id,relationship_id,worker_id,reserved_at,lease_expires_at)
 VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    claim_id,
                    row["user_id"],
                    row["ig_account_id"],
                    row["id"],
                    worker_id,
                    now,
                    now + timedelta(seconds=120),
                ),
            )
            owner = text(row["owner_id"])
            evidence = row["evidence"] or {}
            cached = None
            for key in ("first_follow_back", "reconciliation", "unfollow"):
                candidate = evidence.get(key)
                if (
                    key == "unfollow"
                    and isinstance(candidate, dict)
                    and row["unfollowed_at"]
                ):
                    candidate = {
                        **candidate,
                        "observed_at": row["unfollowed_at"].isoformat(),
                    }
                valid = observation(candidate, owner, row["confirmed_follow_at"], now)
                if valid and (
                    valid["followed_by"]
                    or (
                        key == "reconciliation"
                        and datetime.fromisoformat(valid["observed_at"])
                        > now - timedelta(hours=18)
                    )
                ):
                    if cached is None or valid["followed_by"]:
                        cached = valid
                    if valid["followed_by"]:
                        break
            return ReconciliationClaim(
                claim_id,
                row["user_id"],
                row["ig_account_id"],
                row["id"],
                owner,
                text(row["username"]),
                row["confirmed_follow_at"],
                cached,
            )

    async def permitted(self, claim, now):
        return await asyncio.to_thread(self._permitted, claim, _time(now))

    def _permitted(self, claim, now):
        with self.pool.connection() as conn:
            return (
                conn.execute(
                    """SELECT 1 FROM relationship_reconciliation_attempts r
 JOIN relationship_follows f ON f.id=r.relationship_id AND f.user_id=r.user_id AND f.ig_account_id=r.ig_account_id
 JOIN ig_accounts a ON a.id=r.ig_account_id AND a.user_id=r.user_id
 JOIN agent_configs c ON c.ig_account_id=a.id AND c.user_id=a.user_id
 JOIN targets t ON t.id=f.target_id AND t.user_id=f.user_id AND t.ig_account_id=f.ig_account_id
 WHERE r.id=%(id)s AND r.user_id=%(user)s AND r.ig_account_id=%(account)s AND f.id=%(relationship)s
 AND f.owner_id=%(owner)s AND r.outcome='pending' AND r.lease_expires_at>%(now)s AND """
                    + _POLICY
                    + " AND "
                    + _ELIGIBLE,
                    {
                        "id": claim.id,
                        "user": claim.user_id,
                        "account": claim.ig_account_id,
                        "relationship": claim.relationship_id,
                        "owner": claim.owner_id,
                        "now": now,
                        "since": now - timedelta(days=30),
                        "mature": now - timedelta(hours=72),
                    },
                ).fetchone()
                is not None
            )

    async def finish(self, claim, now, value=None, reason="unknown"):
        return await asyncio.to_thread(self._finish, claim, _time(now), value, reason)

    def _finish(self, claim, now, value, reason):
        valid = observation(value, claim.owner_id, claim.followed_at, now)
        with self.pool.connection() as conn:
            row = conn.execute(
                """SELECT f.evidence FROM relationship_reconciliation_attempts r
 JOIN relationship_follows f ON f.id=r.relationship_id AND f.user_id=r.user_id AND f.ig_account_id=r.ig_account_id
 WHERE r.id=%s AND r.user_id=%s AND r.ig_account_id=%s AND r.relationship_id=%s AND f.owner_id=%s
 AND f.confirmed_follow_at=%s AND r.outcome='pending' AND r.lease_expires_at>%s AND r.reserved_at<=%s
 FOR UPDATE OF r,f""",
                (
                    claim.id,
                    claim.user_id,
                    claim.ig_account_id,
                    claim.relationship_id,
                    claim.owner_id,
                    claim.followed_at,
                    now,
                    now,
                ),
            ).fetchone()
            if row is None:
                return False
            outcome = "unknown"
            if valid:
                evidence = dict(row["evidence"] or {})
                previous = observation(
                    evidence.get("reconciliation"),
                    claim.owner_id,
                    claim.followed_at,
                    now,
                )
                if not previous or valid["observed_at"] >= previous["observed_at"]:
                    evidence["reconciliation"] = valid
                first = observation(
                    evidence.get("first_follow_back"),
                    claim.owner_id,
                    claim.followed_at,
                    now,
                )
                if valid["followed_by"] and not (first and first["followed_by"]):
                    evidence["first_follow_back"] = valid
                conn.execute(
                    "UPDATE relationship_follows SET evidence=%s,updated_at=%s WHERE id=%s AND user_id=%s AND ig_account_id=%s",
                    (
                        Jsonb(evidence),
                        now,
                        claim.relationship_id,
                        claim.user_id,
                        claim.ig_account_id,
                    ),
                )
                outcome = (
                    "follow_back" if valid["followed_by"] else "not_following_back"
                )
            safe_reason = (
                reason
                if reason
                in {
                    "unknown",
                    "worker_cancelled",
                    "timeout",
                    "policy_changed",
                    "account_browser_busy",
                    "rate_limited",
                    "authentication_required",
                    "checkpoint_required",
                    "identity_mismatch",
                    "session_unavailable",
                    "profile_unreadable",
                }
                else "unknown"
            )
            conn.execute(
                "UPDATE relationship_reconciliation_attempts SET outcome=%s,finished_at=%s,failure_code=%s,next_check_at=%s WHERE id=%s",
                (
                    outcome,
                    now,
                    None if valid else safe_reason,
                    datetime.fromisoformat(valid["observed_at"]) + timedelta(hours=18)
                    if valid
                    else None,
                    claim.id,
                ),
            )
            return True
