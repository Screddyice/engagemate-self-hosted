"""Unlinked action receipts reserve capacity without creating billing events."""

from engagemate.domain.budgets import daily_window_start

UNMATCHED_STORY_CALLS = """
SELECT tc.tool_name AS action
FROM hermes_tool_calls tc JOIN hermes_execution_runs r ON r.id=tc.run_id
WHERE r.user_id=%(user_id)s AND r.ig_account_id=%(account_id)s
  AND tc.status IN ('started','uncertain','confirmed')
  AND tc.tool_name IN ('view','like','reply','dm')
  AND tc.started_at >= %(utc_midnight)s AND tc.started_at <= %(now)s
  AND NOT EXISTS (
      SELECT 1 FROM story_engagements e
      WHERE e.user_id=r.user_id AND e.ig_account_id=r.ig_account_id
        AND e.target_id=tc.target_id AND e.story_fingerprint=tc.story_fingerprint
        AND e.action=tc.tool_name AND e.status='sent' AND e.created_at>=tc.started_at
        AND e.executed_at >= %(utc_midnight)s AND e.executed_at <= %(now)s)
"""


def unmatched_story_usage(cursor, user_id, account_id, now, timezone_name="UTC"):
    cursor.execute(
        "SELECT count(*) FILTER (WHERE action IN ('reply','dm')) AS messages, "
        "count(*) FILTER (WHERE action='like') AS likes FROM ("
        + UNMATCHED_STORY_CALLS + ") calls",
        {"user_id":user_id,"account_id":account_id,"now":now,
         "utc_midnight":daily_window_start(now, timezone_name)},
    )
    return cursor.fetchone()
