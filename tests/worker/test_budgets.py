from __future__ import annotations

import pathlib
import re
import traceback
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

from engagemate.domain.budgets import (
    AccountBudgetState,
    ActionBudget,
    AgentConfigLimits,
    BudgetExhausted,
    BudgetInputError,
    BudgetPolicy,
    DailyActionUsage,
)
from engagemate.domain.models import Action


STARTED = datetime(2026, 1, 1, tzinfo=UTC)
TZ_SECRET = "proxy-password-should-not-leak"


class BrokenTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        raise TypeError(TZ_SECRET)

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


class InterruptingTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        raise KeyboardInterrupt(TZ_SECRET)

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (1, (3, 0, 0)),
        (3, (3, 0, 0)),
        (4, (5, 1, 0)),
        (7, (5, 1, 0)),
        (8, (8, 2, 0)),
        (14, (8, 2, 0)),
        (15, (12, 3, 0)),
        (30, (12, 3, 0)),
        (31, (20, 10, 0)),
        (90, (30, 20, 0)),
    ],
)
def test_remaining_applies_warmup_brackets_at_exact_day_boundaries(
    day: int, expected: tuple[int, int, int]
) -> None:
    budget = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        AgentConfigLimits(
            plan="scale",
            daily_like_limit=30,
            daily_reply_limit=30,
            daily_dm_limit=30,
        ),
        DailyActionUsage(likes=0, replies=0, dms=0),
        STARTED + timedelta(days=day - 1),
    )

    assert (budget.likes, budget.replies, budget.dms) == expected
    assert budget.plan_total == 27


@pytest.mark.parametrize(
    ("plan", "expected_total"),
    [("starter", 11), ("pro", 20), ("scale", 27), ("agency", 220), ("custom", 220)],
)
def test_remaining_applies_each_plan_daily_total(
    plan: str, expected_total: int
) -> None:
    budget = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        AgentConfigLimits(
            plan=plan,
            daily_like_limit=20,
            daily_reply_limit=5,
            daily_dm_limit=2,
        ),
        DailyActionUsage(likes=1, replies=1, dms=1),
        STARTED + timedelta(days=40),
    )

    assert (budget.likes, budget.replies, budget.dms) == (19, 4, 0)
    assert budget.plan_total == expected_total - 3


def test_remaining_enforces_user_caps_and_recorded_usage_without_negatives() -> None:
    budget = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        AgentConfigLimits(
            plan="pro",
            daily_like_limit=7,
            daily_reply_limit=2,
            daily_dm_limit=0,
        ),
        DailyActionUsage(likes=5, replies=4, dms=9),
        STARTED + timedelta(days=40),
    )

    assert (budget.likes, budget.replies, budget.dms) == (2, 0, 0)
    assert budget.plan_total == 2


def test_plan_total_counts_usage_above_per_action_caps() -> None:
    budget = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        AgentConfigLimits(
            plan="starter",
            daily_like_limit=3,
            daily_reply_limit=0,
            daily_dm_limit=0,
        ),
        # 3 + 8 = starter's 11-action total, spent past the per-action caps.
        DailyActionUsage(likes=2, replies=9, dms=0),
        STARTED + timedelta(days=40),
    )

    assert (budget.likes, budget.replies, budget.dms) == (1, 0, 0)
    assert budget.plan_total == 0
    with pytest.raises(BudgetExhausted):
        budget.consume(Action.LIKE)


def test_consume_decrements_selected_action_and_shared_total_once() -> None:
    budget = ActionBudget(likes=3, replies=2, dms=1, plan_total=4)

    after_like = budget.consume(Action.LIKE)
    after_reply = after_like.consume(Action.REPLY)

    assert after_like == ActionBudget(likes=2, replies=2, dms=1, plan_total=3)
    assert after_reply == ActionBudget(likes=2, replies=1, dms=1, plan_total=2)
    assert budget == ActionBudget(likes=3, replies=2, dms=1, plan_total=4)


def test_consume_never_exceeds_shared_plan_total() -> None:
    budget = ActionBudget(likes=4, replies=4, dms=4, plan_total=2)

    budget = budget.consume(Action.LIKE).consume(Action.REPLY)

    assert budget.plan_total == 0
    with pytest.raises(BudgetExhausted):
        budget.consume(Action.DM)


@pytest.mark.parametrize(
    ("budget", "action"),
    [
        (ActionBudget(likes=0, replies=1, dms=1, plan_total=3), Action.LIKE),
        (ActionBudget(likes=1, replies=0, dms=1, plan_total=3), Action.REPLY),
        (ActionBudget(likes=1, replies=1, dms=0, plan_total=3), Action.DM),
        (ActionBudget(likes=1, replies=1, dms=1, plan_total=0), Action.LIKE),
    ],
)
def test_consume_rejects_exhausted_capacity(
    budget: ActionBudget, action: Action
) -> None:
    with pytest.raises(BudgetExhausted):
        budget.consume(action)


@pytest.mark.parametrize("action", ["like", None, 1, True, object()])
def test_consume_rejects_unsupported_actions(action: object) -> None:
    with pytest.raises(BudgetInputError):
        ActionBudget(likes=1, replies=1, dms=1, plan_total=3).consume(action)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("account", "config", "usage", "now"),
    [
        (
            AccountBudgetState(warmup_started_at=None),
            AgentConfigLimits("pro", 1, 1, 1),
            DailyActionUsage(0, 0, 0),
            STARTED,
        ),
        (
            AccountBudgetState(warmup_started_at=STARTED + timedelta(seconds=1)),
            AgentConfigLimits("pro", 1, 1, 1),
            DailyActionUsage(0, 0, 0),
            STARTED,
        ),
        (
            AccountBudgetState(warmup_started_at=STARTED.replace(tzinfo=None)),
            AgentConfigLimits("pro", 1, 1, 1),
            DailyActionUsage(0, 0, 0),
            STARTED,
        ),
        (
            AccountBudgetState(warmup_started_at=STARTED),
            AgentConfigLimits("pro", 1, 1, 1),
            DailyActionUsage(0, 0, 0),
            STARTED.replace(tzinfo=None),
        ),
        (
            AccountBudgetState(warmup_started_at=STARTED),
            AgentConfigLimits("enterprise", 1, 1, 1),
            DailyActionUsage(0, 0, 0),
            STARTED,
        ),
        (
            AccountBudgetState(warmup_started_at=STARTED),
            AgentConfigLimits("", 1, 1, 1),
            DailyActionUsage(0, 0, 0),
            STARTED,
        ),
        (
            AccountBudgetState(warmup_started_at=STARTED),
            AgentConfigLimits("pro", True, 1, 1),  # type: ignore[arg-type]
            DailyActionUsage(0, 0, 0),
            STARTED,
        ),
        (
            AccountBudgetState(warmup_started_at=STARTED),
            AgentConfigLimits("pro", -1, 1, 1),
            DailyActionUsage(0, 0, 0),
            STARTED,
        ),
        (
            AccountBudgetState(warmup_started_at=STARTED),
            AgentConfigLimits("pro", 1, 1, 1),
            DailyActionUsage(0, False, 0),  # type: ignore[arg-type]
            STARTED,
        ),
        (
            AccountBudgetState(warmup_started_at=STARTED),
            AgentConfigLimits("pro", 1, 1, 1),
            DailyActionUsage(0, -1, 0),
            STARTED,
        ),
    ],
)
def test_remaining_fails_closed_for_invalid_inputs(
    account: AccountBudgetState,
    config: AgentConfigLimits,
    usage: DailyActionUsage,
    now: datetime,
) -> None:
    with pytest.raises(BudgetInputError):
        BudgetPolicy.remaining(account, config, usage, now)


def test_remaining_normalizes_aware_datetime_offsets_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    now = (STARTED + timedelta(days=3)).astimezone(eastern)

    budget = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        AgentConfigLimits("scale", 30, 30, 30),
        DailyActionUsage(0, 0, 0),
        now,
    )

    assert (budget.likes, budget.replies, budget.dms) == (5, 1, 0)


@pytest.mark.parametrize("broken_field", ["warmup_started_at", "now"])
def test_remaining_sanitizes_broken_timezone_errors(broken_field: str) -> None:
    broken_datetime = datetime(2026, 1, 1, tzinfo=BrokenTimezone())
    account = AccountBudgetState(
        warmup_started_at=(
            broken_datetime if broken_field == "warmup_started_at" else STARTED
        )
    )
    now = broken_datetime if broken_field == "now" else STARTED

    with pytest.raises(BudgetInputError, match="^invalid warm-up time$") as raised:
        BudgetPolicy.remaining(
            account,
            AgentConfigLimits("pro", 1, 1, 1),
            DailyActionUsage(0, 0, 0),
            now,
        )

    rendered = "".join(
        traceback.format_exception(
            type(raised.value),
            raised.value,
            raised.value.__traceback__,
        )
    )
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert TZ_SECRET not in str(raised.value)
    assert TZ_SECRET not in rendered


@pytest.mark.parametrize("broken_field", ["warmup_started_at", "now"])
def test_remaining_does_not_swallow_base_exceptions(broken_field: str) -> None:
    broken_datetime = datetime(2026, 1, 1, tzinfo=InterruptingTimezone())
    account = AccountBudgetState(
        warmup_started_at=(
            broken_datetime if broken_field == "warmup_started_at" else STARTED
        )
    )
    now = broken_datetime if broken_field == "now" else STARTED

    with pytest.raises(KeyboardInterrupt, match=TZ_SECRET):
        BudgetPolicy.remaining(
            account,
            AgentConfigLimits("pro", 1, 1, 1),
            DailyActionUsage(0, 0, 0),
            now,
        )


def test_budget_values_are_frozen() -> None:
    budget = ActionBudget(likes=1, replies=1, dms=1, plan_total=3)

    with pytest.raises(FrozenInstanceError):
        budget.likes = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"likes": -1, "replies": 0, "dms": 0, "plan_total": 0},
        {"likes": 0, "replies": True, "dms": 0, "plan_total": 0},
        {"likes": 0, "replies": 0, "dms": 0, "plan_total": -1},
    ],
)
def test_action_budget_rejects_invalid_capacity(kwargs: dict[str, object]) -> None:
    with pytest.raises(BudgetInputError):
        ActionBudget(**kwargs)  # type: ignore[arg-type]


def test_owner_workspace_ceilings_match_the_web_configuration():
    source = _plans_ts_source()
    total = int(re.search(r"dailyActions: (\d+)", source).group(1))
    views = int(re.search(r"dailyViews: (\d+)", source).group(1))
    assert BudgetPolicy._PLAN_TOTALS["self_hosted"] == total
    assert BudgetPolicy._PLAN_VIEW_TOTALS["self_hosted"] == views


def _view_config(limit: int) -> AgentConfigLimits:
    return AgentConfigLimits(
        plan="scale",
        daily_like_limit=30,
        daily_reply_limit=30,
        daily_dm_limit=30,
        daily_view_limit=limit,
    )


def test_views_do_not_draw_on_the_plan_engagement_total() -> None:
    budget = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        _view_config(200),
        DailyActionUsage(likes=0, replies=0, dms=0, views=40),
        STARTED + timedelta(days=60),
    )

    assert budget.plan_total == 27
    after = budget.consume(Action.VIEW)
    assert after.views == budget.views - 1
    assert after.plan_total == budget.plan_total


def test_views_ramp_with_warm_up_then_the_plan_governs() -> None:
    new_account = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        _view_config(500),
        DailyActionUsage(0, 0, 0, 0),
        STARTED,
    )
    assert new_account.views == 30

    mature = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        _view_config(5_000),
        DailyActionUsage(0, 0, 0, 0),
        STARTED + timedelta(days=60),
    )
    # The last bracket sits above every plan total, so the plan is the ceiling.
    assert mature.views == 500


def test_an_account_with_no_engagement_budget_can_still_view() -> None:
    budget = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        AgentConfigLimits("scale", 0, 0, 0, 100),
        DailyActionUsage(0, 0, 0, 0),
        STARTED + timedelta(days=60),
    )

    assert budget.engagements == 0
    assert budget.views == 100
    for action in (Action.LIKE, Action.REPLY, Action.DM):
        with pytest.raises(BudgetExhausted):
            budget.consume(action)
    assert budget.consume(Action.VIEW).views == 99


def test_exhausted_views_raise_even_with_engagement_budget_left() -> None:
    with pytest.raises(BudgetExhausted):
        ActionBudget(5, 5, 5, plan_total=15, views=0).consume(Action.VIEW)
def _plans_ts_source() -> str:
    return (
        pathlib.Path(__file__).resolve().parents[2]
        / "app"
        / "src"
        / "lib"
        / "plans.ts"
    ).read_text()


def _message_config(reply: int, dm: int) -> AgentConfigLimits:
    return AgentConfigLimits(
        plan="scale",
        daily_like_limit=30,
        daily_reply_limit=reply,
        daily_dm_limit=dm,
        daily_view_limit=500,
    )


@pytest.mark.parametrize(
    ("day", "expected"),
    [(1, 0), (4, 1), (8, 2), (15, 3), (31, 10)],
)
def test_replies_and_dms_share_one_daily_private_message_ceiling(
    day: int, expected: int
) -> None:
    """A Story reply and a DM both land in the same stranger's inbox.

    Instagram counts them together, so warm-up has to as well. Summing two
    generous per-action caps would let a young account send more private
    messages than either cap ever advertised.
    """

    budget = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        _message_config(30, 30),
        DailyActionUsage(likes=0, replies=0, dms=0),
        STARTED + timedelta(days=day - 1),
    )

    assert budget.private_messages == expected


def test_private_message_ceiling_never_exceeds_the_configured_pair() -> None:
    budget = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        _message_config(1, 0),
        DailyActionUsage(likes=0, replies=0, dms=0),
        STARTED + timedelta(days=60),
    )

    assert budget.private_messages == 1


def test_private_message_ceiling_counts_replies_and_dms_already_sent() -> None:
    budget = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        _message_config(30, 30),
        DailyActionUsage(likes=0, replies=2, dms=1),
        STARTED + timedelta(days=60),
    )

    assert budget.private_messages == 12
    assert (budget.replies, budget.dms) == (13, 0)


def test_dms_stop_once_replies_have_spent_the_shared_ceiling() -> None:
    budget = ActionBudget(
        likes=5, replies=5, dms=5, plan_total=20, views=0, private_messages=2
    )

    spent = budget.consume(Action.REPLY).consume(Action.REPLY)

    assert spent.private_messages == 0
    # Each action keeps its own untouched capacity; the shared pool is
    # what stops the next message.
    assert (spent.replies, spent.dms) == (3, 5)
    assert spent.remaining_for(Action.DM) == 0
    with pytest.raises(BudgetExhausted):
        spent.consume(Action.DM)
    with pytest.raises(BudgetExhausted):
        spent.consume(Action.REPLY)
    # Likes and views are public, so the message ceiling must not bind them.
    assert spent.consume(Action.LIKE).likes == 4


def test_likes_and_views_never_draw_on_the_private_message_ceiling() -> None:
    budget = ActionBudget(
        likes=2, replies=1, dms=1, plan_total=9, views=3, private_messages=2
    )

    after = budget.consume(Action.LIKE).consume(Action.VIEW)

    assert after.private_messages == 2


def test_remaining_for_folds_in_every_shared_ceiling() -> None:
    budget = ActionBudget(
        likes=9, replies=9, dms=9, plan_total=4, views=7, private_messages=1
    )

    assert budget.remaining_for(Action.VIEW) == 7
    assert budget.remaining_for(Action.LIKE) == 4
    assert budget.remaining_for(Action.REPLY) == 1
    assert budget.remaining_for(Action.DM) == 1


@pytest.mark.parametrize("action", ["reply", None, 1, True, object()])
def test_remaining_for_rejects_unsupported_actions(action: object) -> None:
    with pytest.raises(BudgetInputError):
        ActionBudget(1, 1, 1, plan_total=3).remaining_for(action)  # type: ignore[arg-type]


def test_engagements_reports_what_the_message_ceiling_actually_allows() -> None:
    budget = ActionBudget(
        likes=1, replies=8, dms=8, plan_total=20, views=0, private_messages=2
    )

    # 1 like plus the 2 private messages the ceiling permits, not 1 + 8 + 8.
    assert budget.engagements == 3


def test_an_omitted_message_ceiling_binds_at_the_pair_it_was_given() -> None:
    budget = ActionBudget(likes=1, replies=2, dms=1, plan_total=9)

    assert budget.private_messages == 3


def test_a_message_ceiling_above_the_pair_is_clamped_to_it() -> None:
    budget = ActionBudget(
        likes=1, replies=1, dms=1, plan_total=9, private_messages=40
    )

    assert budget.private_messages == 2


def test_remaining_always_states_the_message_ceiling_explicitly() -> None:
    """A budget the policy built must never fall back to the loose default."""

    budget = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        _message_config(30, 30),
        DailyActionUsage(likes=0, replies=0, dms=0),
        STARTED + timedelta(days=60),
    )

    assert budget.private_messages == 15
    assert budget.private_messages == budget.replies
    assert budget.dms == 0


def test_private_message_ceiling_rejects_invalid_capacity() -> None:
    with pytest.raises(BudgetInputError):
        ActionBudget(1, 1, 1, plan_total=3, private_messages=-1)
    with pytest.raises(BudgetInputError):
        ActionBudget(1, 1, 1, plan_total=3, private_messages=True)  # type: ignore[arg-type]


def _mature_config() -> AgentConfigLimits:
    return AgentConfigLimits(
        plan="scale",
        daily_like_limit=30,
        daily_reply_limit=30,
        daily_dm_limit=30,
        daily_view_limit=500,
    )


def test_a_qualifying_reconnect_restarts_the_warm_up_ramp() -> None:
    """A recorded reconnect after a long pause starts the ramp again."""

    now = STARTED + timedelta(days=60)
    mature = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED),
        _mature_config(),
        DailyActionUsage(0, 0, 0),
        now,
    )
    setback = BudgetPolicy.remaining(
        AccountBudgetState(
            warmup_started_at=STARTED, last_restricted_at=now - timedelta(hours=6)
        ),
        _mature_config(),
        DailyActionUsage(0, 0, 0),
        now,
    )

    assert (mature.likes, mature.replies, mature.dms) == (30, 15, 0)
    assert (setback.likes, setback.replies, setback.dms) == (3, 0, 0)
    assert setback.private_messages == 0


def test_the_ramp_climbs_again_from_a_qualifying_reconnect() -> None:
    restricted_at = STARTED + timedelta(days=60)
    brackets = {
        days: BudgetPolicy.remaining(
            AccountBudgetState(
                warmup_started_at=STARTED, last_restricted_at=restricted_at
            ),
            _mature_config(),
            DailyActionUsage(0, 0, 0),
            restricted_at + timedelta(days=days),
        )
        for days in (0, 3, 7, 14, 30)
    }

    assert [
        (budget.likes, budget.replies, budget.dms)
        for budget in brackets.values()
    ] == [(3, 0, 0), (5, 1, 0), (8, 2, 0), (12, 3, 0), (20, 10, 0)]


def test_a_reconnect_before_warm_up_started_cannot_extend_the_ramp() -> None:
    now = STARTED + timedelta(days=60)
    budget = BudgetPolicy.remaining(
        AccountBudgetState(
            warmup_started_at=STARTED, last_restricted_at=STARTED - timedelta(days=5)
        ),
        _mature_config(),
        DailyActionUsage(0, 0, 0),
        now,
    )

    assert (budget.likes, budget.replies, budget.dms) == (30, 15, 0)


def test_no_recorded_reconnect_leaves_the_ramp_on_elapsed_time() -> None:
    budget = BudgetPolicy.remaining(
        AccountBudgetState(warmup_started_at=STARTED, last_restricted_at=None),
        _mature_config(),
        DailyActionUsage(0, 0, 0),
        STARTED + timedelta(days=60),
    )

    assert (budget.likes, budget.replies, budget.dms) == (30, 15, 0)


@pytest.mark.parametrize(
    "last_restricted_at",
    [
        STARTED.replace(tzinfo=None),
        STARTED + timedelta(days=61),
        "2026-01-01T00:00:00Z",
        0,
    ],
)
def test_an_unreadable_restart_time_fails_closed(
    last_restricted_at: object,
) -> None:
    with pytest.raises(BudgetInputError):
        BudgetPolicy.remaining(
            AccountBudgetState(
                warmup_started_at=STARTED,
                last_restricted_at=last_restricted_at,  # type: ignore[arg-type]
            ),
            _mature_config(),
            DailyActionUsage(0, 0, 0),
            STARTED + timedelta(days=60),
        )


def test_restart_time_offsets_normalize_to_utc() -> None:
    now = STARTED + timedelta(days=60)
    eastern = timezone(timedelta(hours=-5))
    budget = BudgetPolicy.remaining(
        AccountBudgetState(
            warmup_started_at=STARTED,
            last_restricted_at=(now - timedelta(hours=6)).astimezone(eastern),
        ),
        _mature_config(),
        DailyActionUsage(0, 0, 0),
        now,
    )

    assert (budget.likes, budget.replies, budget.dms) == (3, 0, 0)


def test_a_broken_restart_timezone_leaks_nothing() -> None:
    with pytest.raises(BudgetInputError, match="^invalid warm-up time$") as raised:
        BudgetPolicy.remaining(
            AccountBudgetState(
                warmup_started_at=STARTED,
                last_restricted_at=datetime(2026, 1, 1, tzinfo=BrokenTimezone()),
            ),
            AgentConfigLimits("pro", 1, 1, 1),
            DailyActionUsage(0, 0, 0),
            STARTED,
        )

    rendered = "".join(
        traceback.format_exception(
            type(raised.value), raised.value, raised.value.__traceback__
        )
    )
    assert raised.value.__cause__ is None
    assert TZ_SECRET not in str(raised.value)
    assert TZ_SECRET not in rendered


@pytest.mark.parametrize("day,likes,replies,views", [
    (30, 12, 3, 250), (31, 20, 10, 300), (44, 20, 10, 300),
    (45, 40, 12, 400), (58, 40, 12, 400), (59, 80, 15, 500),
    (72, 80, 15, 500), (73, 120, 18, 600), (86, 120, 18, 600),
    (87, 160, 20, 600), (100, 160, 20, 600), (101, 200, 20, 600),
])
@pytest.mark.parametrize("plan", ["agency", "custom"])
def test_post_warmup_capacity_boundaries(day, likes, replies, views, plan):
    budget = BudgetPolicy.remaining(
        AccountBudgetState(STARTED), AgentConfigLimits(plan, 500, 100, 100, 1000),
        DailyActionUsage(0, 0, 0), STARTED + timedelta(days=day - 1),
    )
    assert (budget.likes, budget.replies, budget.views) == (likes, replies, views)
    assert budget.dms == 0
    assert budget.private_messages == replies
    assert budget.plan_total == 220


def test_mature_capacity_still_honors_saved_opt_outs_and_restrictions():
    now = STARTED + timedelta(days=200)
    config = AgentConfigLimits("agency", 200, 0, 50, 600)
    budget = BudgetPolicy.remaining(AccountBudgetState(STARTED), config,
                                   DailyActionUsage(199, 0, 0, 599), now)
    assert (budget.likes, budget.replies, budget.dms, budget.views) == (1, 0, 0, 1)
    reset = BudgetPolicy.remaining(AccountBudgetState(STARTED, now), config,
                                   DailyActionUsage(0, 0, 0), now)
    assert (reset.likes, reset.replies, reset.views) == (3, 0, 30)
