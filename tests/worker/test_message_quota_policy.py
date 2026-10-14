"""Daily message targets, plan reservations, and Pacific calendar windows."""
from datetime import UTC, datetime, timedelta
from dataclasses import replace
import pytest
from engagemate.domain.models import Action
from engagemate.domain.budgets import (
    AccountBudgetState, AgentConfigLimits, BudgetPolicy, DailyActionUsage, daily_window_start,
)

CLOCK = datetime(2026, 10, 6, 18, tzinfo=UTC)
CONFIG = AgentConfigLimits('starter', 8, 3, 0, 100,
                          daily_reel_message_limit=3, warmup_exempt=True)
ACCOUNT = AccountBudgetState(CLOCK - timedelta(days=1))

@pytest.mark.parametrize('replies,reels,likes,expected', [
    (0, 0, 0, (3, 3, 5)),
    (3, 0, 0, (0, 3, 5)),
    (0, 3, 0, (3, 0, 5)),
    (1, 2, 2, (2, 1, 3)),
    (3, 3, 5, (0, 0, 0)),
    (0, 0, 8, (3, 0, 0)),
])
def test_independent_message_targets_preserve_plan_capacity(replies, reels, likes, expected):
    budget = BudgetPolicy.remaining(ACCOUNT, CONFIG,
        DailyActionUsage(likes, replies, reels, reel_messages=reels), CLOCK)
    assert (budget.remaining_for(Action.REPLY),
            budget.reel_messages, budget.likes) == expected
    assert budget.private_messages + budget.reel_messages + budget.likes <= budget.plan_total


def test_exemption_is_opt_in_and_does_not_change_other_accounts():
    budget = BudgetPolicy.remaining(ACCOUNT, replace(CONFIG, warmup_exempt=False),
                                   DailyActionUsage(0,0,0), CLOCK)
    assert budget.replies == budget.reel_messages == budget.private_messages == 0
    assert budget.likes == 3

@pytest.mark.parametrize('at,expected', [
    ('2026-10-06T06:59:59+00:00','2026-10-05T07:00:00+00:00'),
    ('2026-10-06T07:00:00+00:00','2026-10-06T07:00:00+00:00'),
    ('2026-11-01T12:00:00+00:00','2026-11-01T07:00:00+00:00'),
    ('2026-11-02T12:00:00+00:00','2026-11-02T08:00:00+00:00'),
    ('2026-03-08T12:00:00+00:00','2026-03-08T08:00:00+00:00'),
])
def test_pacific_midnight_handles_dst(at, expected):
    assert daily_window_start(datetime.fromisoformat(at), 'America/Los_Angeles') == datetime.fromisoformat(expected)


@pytest.mark.parametrize('field,value', [('warmup_exempt',1),('daily_reel_message_limit',-1),('daily_reel_message_limit',True)])
def test_invalid_quota_configuration_fails_closed(field,value):
    from engagemate.domain.budgets import BudgetInputError
    with pytest.raises(BudgetInputError):
        BudgetPolicy.remaining(ACCOUNT, replace(CONFIG,**{field:value}), DailyActionUsage(0,0,0), CLOCK)
