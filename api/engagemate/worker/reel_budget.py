"""One policy for reserving and sending follow-up reel messages."""

from engagemate.domain.budgets import (
    AccountBudgetState, AgentConfigLimits, BudgetPolicy, DailyActionUsage,
    daily_window_start,
)
from engagemate.db.text import text
from engagemate.worker.story_admission_usage import unmatched_story_usage


def reel_capacity(cursor, policy, user_id, account_id, now, *,
                  message_id=None, relationship_id=None):
    """Recheck both message lanes, including durable holds, under policy locks."""
    midnight = daily_window_start(now, text(policy["timezone"]))
    cursor.execute("""SELECT
        (SELECT count(*) FROM story_engagements
         WHERE user_id=%s AND ig_account_id=%s AND status='sent'
           AND action IN ('reply','dm') AND executed_at BETWEEN %s AND %s) AS stories,
        (SELECT count(*) FROM story_engagements
         WHERE user_id=%s AND ig_account_id=%s AND status='sent'
           AND action='like' AND executed_at BETWEEN %s AND %s) AS likes,
        (SELECT count(*) FROM follow_reel_messages
         WHERE user_id=%s AND ig_account_id=%s
           AND (%s::uuid IS NULL OR id<>%s)
           AND (%s::uuid IS NULL OR relationship_follow_id<>%s)
           AND status IN ('pending','running','sent','uncertain')
           AND (reserved_at BETWEEN %s AND %s OR attempted_at BETWEEN %s AND %s)) AS reels""",
        (user_id, account_id, midnight, now, user_id, account_id, midnight, now,
         user_id, account_id, message_id, message_id, relationship_id, relationship_id,
         midnight, now, midnight, now))
    used = cursor.fetchone()
    holds = unmatched_story_usage(cursor, user_id, account_id, now, text(policy["timezone"]))
    stories, reels = used["stories"] + holds["messages"], used["reels"]
    likes = used["likes"] + holds["likes"]
    account = AccountBudgetState(policy["warmup_started_at"], policy["restricted_at"])
    config = AgentConfigLimits(text(policy["plan"]), 0, policy["daily_reply_limit"],
                               policy["daily_dm_limit"],
                               daily_reel_message_limit=policy["daily_reel_message_limit"],
                               warmup_exempt=policy["warmup_exempt"])
    usage = DailyActionUsage(likes, stories, reels, reel_messages=reels)
    budget = BudgetPolicy.remaining(account, config, usage, now)
    if config.daily_reel_message_limit is not None:
        return budget.reel_messages > 0
    return budget.private_messages > 0 and budget.plan_total > 0
