#!/usr/bin/env python3
"""Prove one approved canary produced exactly one action, and nothing else.

A green tool call is not evidence. This checks the whole chain for a single
idempotency key and refuses anything ambiguous: one confirmed tool call, one
sent engagement, one finalized billable usage event for a billed action, one
run summary, unchanged account health, and independent confirmation that the
action persists on Instagram. Duplicates, reserved or released usage, deteriorated
health, and any tenant or account mismatch fail.

    verify_execution_evidence.py --run <uuid> --key <idempotency-key> \
        --instagram-confirmed
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

# Watching a Story records a view on Instagram but is not billed, so it must
# produce no usage event. Requiring one would fail every view canary; allowing
# one silently would hide a billing bug.
_BILLABLE = frozenset({"like", "reply", "dm"})
_SUMMARY_COUNTS = {
    "like": "likes_sent",
    "reply": "replies_sent",
    "dm": "dms_sent",
}


class EvidenceError(Exception):
    """Evidence that does not prove exactly one clean action."""


def _text(value: object) -> str | None:
    """Decode text from UTF-8 and SQL_ASCII PostgreSQL clusters alike.

    A SQL_ASCII cluster hands psycopg bytes for every text column. Comparing
    those to a string silently never matches, which for a safety gate means
    refusing everything or, worse, writing bytes back into an approval key.
    """

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return value if type(value) is str else None


def _required_text(value: object, what: str) -> str:
    decoded = _text(value)
    if decoded is None or not decoded.strip():
        raise EvidenceError(f"{what} is unreadable")
    return decoded



@dataclass(frozen=True, slots=True)
class Evidence:
    run_id: UUID
    idempotency_key: str
    user_id: UUID
    ig_account_id: UUID
    target_id: UUID
    action: str
    engagement_id: UUID
    usage_status: str | None
    run_summary_id: UUID
    health_status: str


_CALL_SQL = """
SELECT call.id, call.tool_name AS action, call.status, call.target_id,
       call.story_fingerprint, call.started_at,
       run.user_id, run.ig_account_id, run.job_id
FROM hermes_tool_calls call
JOIN hermes_execution_runs run ON run.id = call.run_id
WHERE call.run_id = %(run_id)s AND call.idempotency_key = %(key)s
"""

_ENGAGEMENT_SQL = """
SELECT id, status
FROM story_engagements
WHERE user_id = %(user_id)s
  AND ig_account_id = %(ig_account_id)s
  AND target_id = %(target_id)s
  AND story_fingerprint = %(story_fingerprint)s
  AND action = %(action)s
"""

_USAGE_SQL = """
SELECT id, status, finalized_at, user_id, ig_account_id
FROM engagement_usage_events
WHERE engagement_id = %(engagement_id)s
"""

_SUMMARY_SQL = """
SELECT id, user_id, ig_account_id, likes_sent, replies_sent, dms_sent
FROM run_summaries
WHERE job_id = %(job_id)s
"""

_RUN_CALLS_SQL = """
SELECT idempotency_key FROM hermes_tool_calls WHERE run_id = %(run_id)s
"""

_HEALTH_SQL = """
SELECT account.health_status,
       account.paused_at,
       (SELECT count(*)
          FROM account_health_events event
         WHERE event.user_id = account.user_id
           AND event.ig_account_id = account.id
           AND event.event_type = ANY(%(restriction_events)s)
           AND event.created_at >= %(since)s
       ) AS restrictions
FROM ig_accounts account
WHERE account.id = %(ig_account_id)s AND account.user_id = %(user_id)s
"""


def _one(rows: list[Any], what: str) -> Any:
    if not rows:
        raise EvidenceError(f"no {what}")
    if len(rows) > 1:
        raise EvidenceError(f"{len(rows)} {what} where exactly one is required")
    return rows[0]


def verify(
    pool: Any,
    *,
    run_id: UUID,
    idempotency_key: str,
    instagram_confirmed: bool,
    now: datetime,
) -> Evidence:
    """Return the evidence chain, or raise on the first thing that fails."""

    from engagemate.worker.circuit_breaker import RESTRICTION_SIGNALS

    if type(now) is not datetime or now.tzinfo is None:
        raise EvidenceError("an exact timezone-aware clock is required")
    if instagram_confirmed is not True:
        raise EvidenceError(
            "an operator must confirm the action is visible on Instagram"
        )

    with pool.connection() as connection:
        call = _one(
            connection.execute(
                _CALL_SQL, {"run_id": run_id, "key": idempotency_key}
            ).fetchall(),
            "tool calls for this idempotency key",
        )
        status = _required_text(call["status"], "tool call status")
        if status != "confirmed":
            raise EvidenceError(f"tool call status is {status}, not confirmed")
        if call["target_id"] is None or call["story_fingerprint"] is None:
            raise EvidenceError("tool call did not record what it attempted")
        action = _required_text(call["action"], "action")
        if action not in {*_SUMMARY_COUNTS, "view"}:
            raise EvidenceError("unknown action")
        run_call = _one(
            connection.execute(_RUN_CALLS_SQL, {"run_id": run_id}).fetchall(),
            "action attempts in this single-action canary",
        )
        if _text(run_call["idempotency_key"]) != idempotency_key:
            raise EvidenceError("run action does not match the approved attempt")

        scope = {
            "user_id": call["user_id"],
            "ig_account_id": call["ig_account_id"],
            "target_id": call["target_id"],
            "story_fingerprint": _required_text(
                call["story_fingerprint"], "story fingerprint"
            ),
            "action": action,
        }
        engagement = _one(
            connection.execute(_ENGAGEMENT_SQL, scope).fetchall(),
            "story engagements for this action",
        )
        sent = _required_text(engagement["status"], "engagement status")
        if sent != "sent":
            raise EvidenceError(f"engagement status is {sent}, not sent")

        usage_rows = connection.execute(
            _USAGE_SQL, {"engagement_id": engagement["id"]}
        ).fetchall()
        usage_status: str | None = None
        if action in _BILLABLE:
            usage = _one(usage_rows, "usage events for this engagement")
            usage_status = _required_text(usage["status"], "usage status")
            if usage_status == "reserved" or usage["finalized_at"] is None:
                raise EvidenceError("usage event is still reserved, not finalized")
            if usage_status != "billable":
                raise EvidenceError("sent engagement usage is not billable")
            if (
                usage["user_id"] != call["user_id"]
                or usage["ig_account_id"] != call["ig_account_id"]
            ):
                raise EvidenceError("usage event belongs to another account")
        elif usage_rows:
            raise EvidenceError(f"a {action} produced a usage event and must not")

        summary = _one(
            connection.execute(_SUMMARY_SQL, {"job_id": call["job_id"]}).fetchall(),
            "run summaries for this job",
        )
        if (
            summary["user_id"] != call["user_id"]
            or summary["ig_account_id"] != call["ig_account_id"]
        ):
            raise EvidenceError("run summary belongs to another account")
        for reported_action, column in _SUMMARY_COUNTS.items():
            if summary[column] != int(reported_action == action):
                raise EvidenceError("run summary action counts do not match the canary")

        health = connection.execute(
            _HEALTH_SQL,
            {
                "user_id": call["user_id"],
                "ig_account_id": call["ig_account_id"],
                "since": call["started_at"],
                "restriction_events": sorted(s.value for s in RESTRICTION_SIGNALS),
            },
        ).fetchone()
        if health is None:
            raise EvidenceError("the account is unreadable")
        current = _required_text(health["health_status"], "account health")
        if current != "active" or health["paused_at"] is not None:
            raise EvidenceError(f"account health is {current}")
        if int(health["restrictions"]) > 0:
            raise EvidenceError("Instagram restricted the account during this action")

    return Evidence(
        run_id=run_id,
        idempotency_key=idempotency_key,
        user_id=call["user_id"],
        ig_account_id=call["ig_account_id"],
        target_id=call["target_id"],
        action=action,
        engagement_id=engagement["id"],
        usage_status=usage_status,
        run_summary_id=summary["id"],
        health_status=current,
    )


def render(evidence: Evidence) -> str:
    return "\n".join(
        [
            f"run              {evidence.run_id}",
            f"idempotency_key  {evidence.idempotency_key}",
            f"action           {evidence.action}",
            f"engagement       {evidence.engagement_id}",
            f"usage            {evidence.usage_status or 'not billable'}",
            f"run_summary      {evidence.run_summary_id}",
            f"account_health   {evidence.health_status}",
            "verdict          one confirmed action, no duplicates",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--run", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument(
        "--instagram-confirmed",
        action="store_true",
        help="the operator verified the action on Instagram through native UI or independent readback",
    )
    args = parser.parse_args(argv)

    from engagemate.db.pool import create_pool

    url = args.database_url or os.getenv("DATABASE_URL")
    if not url:
        print("refused: DATABASE_URL is unset", file=sys.stderr)
        return 2
    try:
        run_id = UUID(args.run)
    except (TypeError, ValueError):
        print("refused: run id is invalid", file=sys.stderr)
        return 2

    pool = create_pool(url, min_size=1, max_size=2)
    try:
        print(
            render(
                verify(
                    pool,
                    run_id=run_id,
                    idempotency_key=args.key,
                    instagram_confirmed=args.instagram_confirmed,
                    now=datetime.now(UTC),
                )
            )
        )
    except EvidenceError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
