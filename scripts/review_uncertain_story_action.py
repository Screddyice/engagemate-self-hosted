#!/usr/bin/env python3
"""Record an operator's evidence review without replaying a Story action."""

from __future__ import annotations

import argparse
import json
import os
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

REVIEW_TOOL = "operator_uncertain_story_review"
REVIEW_STATES = ("no_write_verified", "reviewed_no_replay")


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def record_review(connection, *, call_id: UUID, account: str, state: str, evidence: str) -> str:
    if state not in REVIEW_STATES:
        raise ValueError("invalid review state")
    if not evidence.strip() or len(evidence) > 500:
        raise ValueError("a bounded evidence reference is required")

    row = connection.execute(
        """
        select h.id, h.tool_name, h.status, run.user_id, run.ig_account_id
        from hermes_tool_calls h
        join hermes_execution_runs run on run.id = h.run_id
        join ig_accounts account on account.id = run.ig_account_id
                                and account.user_id = run.user_id
        where h.id = %(call_id)s and account.username = %(account)s
          and h.tool_name in ('view', 'like', 'reply', 'dm')
        for update of h
        """,
        {"call_id": call_id, "account": account},
    ).fetchone()
    if row is None or _text(row["status"]) != "uncertain":
        raise ValueError("the selected account has no uncertain Story tool call with this ID")

    existing = connection.execute(
        """
        select after_state from mcp_audit_events
        where tool_name = %(tool)s and outcome = 'updated'
          and user_id = %(user_id)s and ig_account_id = %(account_id)s
          and after_state->>'tool_call_id' = %(call_id)s
        order by created_at desc, id desc limit 1
        """,
        {"tool": REVIEW_TOOL, "user_id": row["user_id"],
         "account_id": row["ig_account_id"], "call_id": str(call_id)},
    ).fetchone()
    after = {"tool_call_id": str(call_id), "review_state": state,
             "evidence_ref": evidence.strip()}
    if existing:
        if existing["after_state"] != after:
            raise ValueError("this call already has a different review; preserve its audit trail")
        return "already_recorded"

    connection.execute(
        """
        insert into mcp_audit_events
            (user_id, ig_account_id, client_id, tool_name, outcome,
             before_state, after_state, safe_metadata)
        values (%(user_id)s, %(account_id)s, 'operator', %(tool)s, 'updated',
                %(before)s::jsonb, %(after)s::jsonb, '{}'::jsonb)
        """,
        {"user_id": row["user_id"], "account_id": row["ig_account_id"],
         "tool": REVIEW_TOOL,
         "before": json.dumps({"tool_call_id": str(call_id),
                               "action": _text(row["tool_name"]), "status": "uncertain"}),
         "after": json.dumps(after)},
    )
    return "recorded"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--call-id", type=UUID, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--state", choices=REVIEW_STATES, required=True)
    parser.add_argument("--evidence", required=True,
                        help="Document path or observation reference; no credentials or message text")
    args = parser.parse_args()
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as connection:
        outcome = record_review(connection, call_id=args.call_id, account=args.account,
                                state=args.state, evidence=args.evidence)
    print(outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
