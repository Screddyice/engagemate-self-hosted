#!/usr/bin/env python3
"""Preview and approve exactly one live Instagram action.

In canary mode the worker asks to mutate, `begin_mutation` finds no approval,
and the attempt is denied as `canary_approval_required` without the browser
clicking anything. That denied row is the record of what the worker wanted to
do. This script shows a human that row and, on an explicit approval, writes the
single-use `live_action_approvals` grant the next attempt will consume.

Two properties matter more than convenience here:

* The preview prints an allowlist of fields. It never selects cookies, proxy
  credentials, fingerprints, DOM, or media, so it cannot print them.
* An approval is one action, on one Story, for fifteen minutes, once. It is
  matched on the same key `begin_mutation` matches on, including the generated
  text hash, so approving one message never authorises a different one.

    prepare_instagram_canary.py preview --account <username>
    echo APPROVE | prepare_instagram_canary.py approve \
        --preview <id> --approved-by operator
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

# Approval is a named human act, not a role. A canary that anyone can approve
# is not an approval gate. Extend deliberately, never from an environment
# variable a compromised process could set.
APPROVERS = frozenset({"operator"})
APPROVAL_TTL = timedelta(minutes=15)
CONFIRMATION = "APPROVE"
_FINGERPRINT_PREFIX = 12
_MUTATIONS = ("view", "like", "reply", "dm")

class CanaryError(Exception):
    """A preview or approval that must not proceed."""


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
        raise CanaryError(f"{what} is unreadable")
    return decoded



@dataclass(frozen=True, slots=True)
class Preview:
    """The allowlist of fields a human needs to judge one action."""

    preview_id: UUID
    run_id: UUID
    account_username: str
    target_username: str
    fingerprint_prefix: str
    action: str
    proposed_text: str | None
    idempotency_key: str
    remaining_engagements: int
    remaining_private_messages: int
    remaining_views: int
    attempted_at: datetime


_PENDING_SQL = """
SELECT call.id AS preview_id,
       call.run_id,
       call.tool_name AS action,
       call.story_fingerprint,
       call.proposed_text,
       call.idempotency_key,
       call.started_at,
       call.target_id,
       run.user_id,
       run.ig_account_id,
       account.username AS account_username,
       account.warmup_started_at,
       account.health_status,
       target.username AS target_username,
       account_user.plan,
       config.daily_like_limit,
       config.daily_reply_limit,
       config.daily_dm_limit,
       config.daily_view_limit
FROM hermes_tool_calls call
JOIN hermes_execution_runs run ON run.id = call.run_id
JOIN ig_accounts account
      ON account.id = run.ig_account_id
     AND account.user_id = run.user_id
JOIN users account_user ON account_user.id = run.user_id
JOIN targets target
      ON target.id = call.target_id
     AND target.ig_account_id = run.ig_account_id
     AND target.user_id = run.user_id
LEFT JOIN agent_configs config
       ON config.ig_account_id = run.ig_account_id
      AND config.user_id = run.user_id
WHERE account.username = %(account_username)s
  AND call.status = 'denied'
  AND call.failure_class = 'canary_approval_required'
  AND call.tool_name = ANY(%(mutations)s)
  AND call.target_id IS NOT NULL
  AND call.story_fingerprint IS NOT NULL
ORDER BY call.started_at DESC, call.id DESC
LIMIT 1
"""

_USAGE_SQL = """
SELECT
    count(*) FILTER (WHERE action = 'like') AS likes,
    count(*) FILTER (WHERE action = 'reply') AS replies,
    count(*) FILTER (WHERE action = 'dm') AS dms,
    count(*) FILTER (WHERE action = 'view') AS views
FROM story_engagements
WHERE user_id = %(user_id)s
  AND ig_account_id = %(account_id)s
  AND status = 'sent'
  AND created_at >= date_trunc('day', %(now)s)
"""

_RESTRICTED_SQL = """
SELECT max(event.created_at) AS last_restricted_at
FROM account_health_events event
WHERE event.user_id = %(user_id)s
  AND event.ig_account_id = %(account_id)s
  AND event.event_type = ANY(%(restriction_events)s)
"""


def _budget(connection: Any, row: Any, now: datetime) -> Any:
    from engagemate.domain.budgets import (
        AccountBudgetState,
        AgentConfigLimits,
        BudgetPolicy,
        DailyActionUsage,
    )
    from engagemate.worker.circuit_breaker import RESTRICTION_SIGNALS

    scope = {"user_id": row["user_id"], "account_id": row["ig_account_id"]}
    usage = connection.execute(_USAGE_SQL, {**scope, "now": now}).fetchone()
    restricted = connection.execute(
        _RESTRICTED_SQL,
        {
            **scope,
            "restriction_events": sorted(s.value for s in RESTRICTION_SIGNALS),
        },
    ).fetchone()
    if row["daily_like_limit"] is None:
        raise CanaryError("the account has no agent configuration")
    try:
        return BudgetPolicy.remaining(
            AccountBudgetState(
                row["warmup_started_at"], restricted["last_restricted_at"]
            ),
            AgentConfigLimits(
                _required_text(row["plan"], "plan"),
                row["daily_like_limit"],
                row["daily_reply_limit"],
                row["daily_dm_limit"],
                row["daily_view_limit"],
            ),
            DailyActionUsage(
                int(usage["likes"]), int(usage["replies"]),
                int(usage["dms"]), int(usage["views"]),
            ),
            now,
        )
    except CanaryError:
        raise
    # Budget errors share no public base, and none of them mean "go ahead".
    except Exception:  # noqa: BLE001
        raise CanaryError("the account budget is unreadable") from None


def preview(pool: Any, *, account_username: str, now: datetime) -> Preview:
    """Return the newest mutation this account was denied for lack of approval."""

    _require_utc(now)
    with pool.connection() as connection:
        row = connection.execute(
            _PENDING_SQL,
            {"account_username": account_username, "mutations": list(_MUTATIONS)},
        ).fetchone()
        if row is None:
            raise CanaryError("no approval-pending action for this account")
        health = _required_text(row["health_status"], "account health")
        if health != "active":
            raise CanaryError(f"account health is {health}, not active")
        action = _required_text(row["action"], "action")
        fingerprint = _required_text(row["story_fingerprint"], "story fingerprint")
        text = _text(row["proposed_text"])
        if action in {"reply", "dm"} and not text:
            raise CanaryError("a message action recorded no proposed text")
        if action in {"view", "like"} and text:
            raise CanaryError("a public action recorded proposed text")
        budget = _budget(connection, row, now)
    return Preview(
        preview_id=row["preview_id"],
        run_id=row["run_id"],
        account_username=_required_text(row["account_username"], "account"),
        target_username=_required_text(row["target_username"], "target"),
        fingerprint_prefix=fingerprint[:_FINGERPRINT_PREFIX],
        action=action,
        proposed_text=text,
        idempotency_key=_required_text(row["idempotency_key"], "idempotency key"),
        remaining_engagements=budget.engagements,
        remaining_private_messages=budget.private_messages,
        remaining_views=budget.views,
        attempted_at=row["started_at"],
    )


def render_preview(value: Preview) -> str:
    """Render only the allowlisted fields, one per line."""

    lines = [
        f"preview_id       {value.preview_id}",
        f"account          {value.account_username}",
        f"target           {value.target_username}",
        f"action           {value.action}",
        f"story            {value.fingerprint_prefix}...",
        f"idempotency_key  {value.idempotency_key}",
        f"attempted_at     {value.attempted_at.isoformat()}",
        f"remaining        engagements={value.remaining_engagements} "
        f"private_messages={value.remaining_private_messages} "
        f"views={value.remaining_views}",
    ]
    if value.proposed_text is not None:
        lines.append(f"proposed_text    {value.proposed_text}")
    return "\n".join(lines)


_APPROVAL_TARGET_SQL = """
SELECT call.target_id,
       call.story_fingerprint,
       call.tool_name AS action,
       call.generated_text_hash,
       call.status,
       call.failure_class,
       run.user_id,
       run.ig_account_id
FROM hermes_tool_calls call
JOIN hermes_execution_runs run ON run.id = call.run_id
WHERE call.id = %(preview_id)s
"""

_REPLAY_SQL = """
SELECT id FROM live_action_approvals
WHERE user_id = %(user_id)s
  AND ig_account_id = %(ig_account_id)s
  AND target_id = %(target_id)s
  AND story_fingerprint = %(story_fingerprint)s
  AND action = %(action)s
  AND generated_text_hash IS NOT DISTINCT FROM %(generated_text_hash)s
  AND consumed_at IS NULL
  AND expires_at > %(now)s
LIMIT 1
"""


def approve(
    pool: Any,
    *,
    preview_id: UUID,
    approved_by: str,
    confirmation: str,
    now: datetime,
    ttl: timedelta = APPROVAL_TTL,
) -> UUID:
    """Write one single-use approval for the previewed action."""

    _require_utc(now)
    if approved_by not in APPROVERS:
        raise CanaryError("approver is not permitted to approve live actions")
    if confirmation.removesuffix("\n") != CONFIRMATION:
        raise CanaryError("confirmation must be exactly APPROVE")
    if not isinstance(preview_id, UUID):
        raise CanaryError("preview id is invalid")

    with pool.connection() as connection, connection.transaction():
        row = connection.execute(
            _APPROVAL_TARGET_SQL, {"preview_id": preview_id}
        ).fetchone()
        if row is None:
            raise CanaryError("preview id is unknown")
        if (
            _text(row["status"]) != "denied"
            or _text(row["failure_class"]) != "canary_approval_required"
        ):
            raise CanaryError("preview is not an approval-pending action")
        # Decoded before reuse: a bytes fingerprint written back into the
        # approval would never match what begin_mutation looks for.
        key = {
            "user_id": row["user_id"],
            "ig_account_id": row["ig_account_id"],
            "target_id": row["target_id"],
            "story_fingerprint": _required_text(
                row["story_fingerprint"], "story fingerprint"
            ),
            "action": _required_text(row["action"], "action"),
            "generated_text_hash": _text(row["generated_text_hash"]),
        }
        # The approval table's composite foreign key rejects a target that
        # belongs to another tenant or another account. Checking first turns
        # that into a readable refusal instead of a constraint traceback.
        owned = connection.execute(
            """
            SELECT id FROM targets
            WHERE id = %(target_id)s
              AND ig_account_id = %(ig_account_id)s
              AND user_id = %(user_id)s
            """,
            key,
        ).fetchone()
        if owned is None:
            raise CanaryError("target does not belong to this account")
        if connection.execute(_REPLAY_SQL, {**key, "now": now}).fetchone():
            raise CanaryError("an unconsumed approval already exists")
        created = connection.execute(
            """
            INSERT INTO live_action_approvals (
                user_id, ig_account_id, target_id, story_fingerprint, action,
                generated_text_hash, approved_by, approved_at, expires_at
            ) VALUES (
                %(user_id)s, %(ig_account_id)s, %(target_id)s,
                %(story_fingerprint)s, %(action)s, %(generated_text_hash)s,
                %(approved_by)s, %(now)s, %(expires_at)s
            )
            RETURNING id
            """,
            {
                **key,
                "approved_by": approved_by,
                "now": now,
                "expires_at": now + ttl,
            },
        ).fetchone()
    return created["id"]


def _require_utc(now: datetime) -> None:
    if type(now) is not datetime or now.tzinfo is None:
        raise CanaryError("an exact timezone-aware clock is required")


def _pool(database_url: str | None) -> Any:
    from engagemate.db.pool import create_pool

    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise CanaryError("DATABASE_URL is unset")
    return create_pool(url, min_size=1, max_size=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("preview")
    show.add_argument("--account", required=True)
    grant = sub.add_parser("approve")
    grant.add_argument("--preview", required=True)
    grant.add_argument("--approved-by", required=True)
    args = parser.parse_args(argv)

    now = datetime.now(UTC)
    try:
        pool = _pool(args.database_url)
        try:
            if args.command == "preview":
                print(render_preview(preview(pool, account_username=args.account, now=now)))
            else:
                try:
                    preview_id = UUID(args.preview)
                except (TypeError, ValueError):
                    raise CanaryError("preview id is invalid") from None
                approval = approve(
                    pool,
                    preview_id=preview_id,
                    approved_by=args.approved_by,
                    confirmation=sys.stdin.read(),
                    now=now,
                )
                print(f"approved {approval} until {(now + APPROVAL_TTL).isoformat()}")
        finally:
            pool.close()
    except CanaryError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
