"""Warm-up, subscription-plan, and account action budgets."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from engagemate.domain.models import Action


class BudgetError(Exception):
    """Base class for bounded budget failures."""


class BudgetInputError(BudgetError):
    """Raised when budget inputs cannot be trusted."""


class BudgetExhausted(BudgetError):
    """Raised when an action has no remaining daily capacity."""

    def __init__(self, action: Action) -> None:
        self.action = action
        super().__init__("action budget exhausted")


RECONNECT_WARMUP_AFTER = timedelta(days=30)
WARMUP_RESET_EVENT = "warmup_restart"


@dataclass(frozen=True, slots=True)
class AccountBudgetState:
    """Account fields needed to determine its warm-up bracket."""

    warmup_started_at: datetime | None
    #: The latest qualifying reconnect after at least 30 days paused.
    #: Short disconnects and Instagram safety events do not reset the ramp.
    last_restricted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AgentConfigLimits:
    """Plan and per-account limits from the agent configuration."""

    plan: str
    daily_like_limit: int
    daily_reply_limit: int
    daily_dm_limit: int
    daily_view_limit: int = 0
    daily_reel_message_limit: int | None = None
    warmup_exempt: bool = False


@dataclass(frozen=True, slots=True)
class DailyActionUsage:
    """Today's recorded action quantities."""

    likes: int
    replies: int
    dms: int
    views: int = 0
    # Included in dms for aggregate accounting, identified for lane quotas.
    reel_messages: int = 0

    @property
    def total(self) -> int:
        """Engagements counted against the plan. Views are not engagements."""

        return self.likes + self.replies + self.dms


#: Actions that put a message in a stranger's private inbox. Instagram reads
#: a Story reply and a DM as the same act, so they draw on one shared ceiling
#: rather than two independent ones that could sum past either.
PRIVATE_MESSAGE_ACTIONS = frozenset({Action.REPLY, Action.DM})

_ACTION_FIELDS = {
    Action.LIKE: "likes",
    Action.REPLY: "replies",
    Action.DM: "dms",
    Action.VIEW: "views",
}


@dataclass(frozen=True, slots=True)
class ActionBudget:
    """Immutable per-action capacity plus the shared plan capacity."""

    likes: int
    replies: int
    dms: int
    plan_total: int
    views: int = 0
    #: Replies and DMs a stranger may receive today, across both actions.
    #: Omitting it means "bounded only by the pair", which is what a caller
    #: with no warm-up context can honestly claim. BudgetPolicy.remaining
    #: always states it, so a real run is never governed by that fallback.
    private_messages: int | None = None
    reel_messages: int = 0

    def __post_init__(self) -> None:
        _validate_counts(
            self.likes,
            self.replies,
            self.dms,
            self.plan_total,
            self.views,
            self.reel_messages,
        )
        pair = self.replies + self.dms
        if self.private_messages is None:
            object.__setattr__(self, "private_messages", pair)
            return
        _validate_counts(self.private_messages)
        object.__setattr__(self, "private_messages", min(self.private_messages, pair))

    @property
    def engagements(self) -> int:
        """Engagement capacity, which the plan total also bounds."""

        messages = min(self.replies + self.dms, self.private_messages)
        return min(self.plan_total, self.likes + messages)

    def remaining_for(self, action: Action) -> int:
        """Capacity for one action once every shared ceiling is applied."""

        if not isinstance(action, Action):
            raise BudgetInputError("invalid action")
        own = getattr(self, _ACTION_FIELDS[action])
        if action is Action.VIEW:
            return own
        if action in PRIVATE_MESSAGE_ACTIONS:
            return min(own, self.private_messages, self.plan_total)
        return min(own, self.plan_total)

    def consume(self, action: Action) -> ActionBudget:
        """Consume one action, and a unit of the plan total unless it is a view."""

        if not isinstance(action, Action):
            raise BudgetInputError("invalid action")

        field = _ACTION_FIELDS[action]
        if getattr(self, field) == 0:
            raise BudgetExhausted(action)
        if action is Action.VIEW:
            return replace(self, views=self.views - 1)
        if self.plan_total == 0:
            raise BudgetExhausted(action)
        spent = {field: getattr(self, field) - 1, "plan_total": self.plan_total - 1}
        if action in PRIVATE_MESSAGE_ACTIONS:
            if self.private_messages == 0:
                raise BudgetExhausted(action)
            spent["private_messages"] = self.private_messages - 1
        return replace(self, **spent)


class BudgetPolicy:
    """Calculate remaining capacity across all daily limit dimensions."""

    # Must equal dailyActions in app/src/lib/plans.ts, which is what the
    # pricing page sells and the README publishes. A plan missing here raises
    # rather than defaulting, so omitting one fails every run on that plan.
    # tests/worker/test_budgets.py reads plans.ts and pins both sides.
    _PLAN_TOTALS = {"starter": 11, "pro": 20, "scale": 27, "agency": 220, "custom": 220, "self_hosted": 220}
    # Watching a Story sends nothing, so a plan allows far more views than
    # engagements. Views are counted and capped all the same: a new account
    # that watches hundreds of Stories on its first day still looks automated.
    # The final warm-up bracket sits above every plan total on purpose, so a
    # matured account is governed by what its plan sells rather than warm-up.
    _PLAN_VIEW_TOTALS = {
        "starter": 100,
        "pro": 300,
        "scale": 600,
        "agency": 600,
        "custom": 600,
        "self_hosted": 600,
    }
    # (like, reply, legacy_dm, view, private_message). Standalone DMs are
    # retired. Historical DM usage still consumes today's shared allowance.
    # The first 30 days stay unchanged. Later stages are product ceilings,
    # not verified safe limits; recorded restrictions restart this ladder.
    _WARMUP_BRACKETS = (
        (3, (3, 0, 0, 30, 0)),
        (7, (5, 1, 0, 60, 1)),
        (14, (8, 2, 0, 120, 2)),
        (30, (12, 3, 0, 250, 3)),
        (44, (20, 10, 0, 300, 10)),
        (58, (40, 12, 0, 400, 12)),
        (72, (80, 15, 0, 500, 15)),
        (86, (120, 18, 0, 600, 18)),
        (100, (160, 20, 0, 600, 20)),
        (None, (200, 20, 0, 600, 20)),
    )

    @classmethod
    def remaining(
        cls,
        account: AccountBudgetState,
        config: AgentConfigLimits,
        usage: DailyActionUsage,
        now: datetime,
    ) -> ActionBudget:
        """Return today's remaining account budget, clamped at zero."""

        if not isinstance(account, AccountBudgetState):
            raise BudgetInputError("invalid account budget state")
        if not isinstance(config, AgentConfigLimits):
            raise BudgetInputError("invalid agent config limits")
        if not isinstance(usage, DailyActionUsage):
            raise BudgetInputError("invalid daily action usage")

        started_at = _as_utc(account.warmup_started_at)
        current_at = _as_utc(now)
        if started_at is None or current_at is None or started_at > current_at:
            raise BudgetInputError("invalid warm-up time")

        if account.last_restricted_at is not None:
            reset_at = _as_utc(account.last_restricted_at)
            if reset_at is None or reset_at > current_at:
                raise BudgetInputError("invalid warm-up time")
            # A reconnect older than launch cannot extend the ramp.
            started_at = max(started_at, reset_at)

        if type(config.plan) is not str or config.plan not in cls._PLAN_TOTALS:
            raise BudgetInputError("invalid plan")
        _validate_counts(
            config.daily_like_limit,
            config.daily_reply_limit,
            config.daily_dm_limit,
            config.daily_view_limit,
        )
        _validate_counts(usage.likes, usage.replies, usage.dms, usage.views)

        warmup_day = (current_at - started_at).days + 1
        *warmup_limits, warmup_messages = cls._warmup_limits(warmup_day)
        if type(config.warmup_exempt) is not bool:
            raise BudgetInputError("invalid warm-up exemption")
        if config.warmup_exempt:
            *warmup_limits, warmup_messages = cls._WARMUP_BRACKETS[-1][1]
        configured_limits = (
            config.daily_like_limit,
            config.daily_reply_limit,
            config.daily_dm_limit,
            config.daily_view_limit,
        )
        used = (usage.likes, usage.replies, usage.dms, usage.views)
        likes, replies, dms, views = (
            max(min(warmup, configured) - consumed, 0)
            for warmup, configured, consumed in zip(
                warmup_limits, configured_limits, used, strict=True
            )
        )
        plan_total = max(cls._PLAN_TOTALS[config.plan] - usage.total, 0)
        views = min(views, max(cls._PLAN_VIEW_TOTALS[config.plan] - usage.views, 0))
        configured_messages = config.daily_reply_limit + config.daily_dm_limit
        separate_reels = config.daily_reel_message_limit is not None
        if separate_reels:
            _validate_counts(config.daily_reel_message_limit, usage.reel_messages)
            if usage.reel_messages > usage.dms:
                raise BudgetInputError("invalid reel usage")
            configured_messages += config.daily_reel_message_limit
        private_messages = max(
            min(warmup_messages, configured_messages) - usage.replies - usage.dms,
            0,
        )
        reel_messages = 0
        if separate_reels:
            reel_remaining = max(config.daily_reel_message_limit - usage.reel_messages, 0)
            # Reserve each lane's unspent messages before allowing likes.
            # Neither Story replies nor reels can consume the other's target.
            story_remaining = min(replies + dms, private_messages, plan_total)
            reel_messages = min(reel_remaining, max(private_messages - story_remaining, 0),
                                max(plan_total - story_remaining, 0))
            private_messages = min(story_remaining, max(private_messages - reel_messages, 0))
            likes = min(likes, max(plan_total - private_messages - reel_messages, 0))
        return ActionBudget(
            likes,
            replies,
            dms,
            plan_total=plan_total,
            views=views,
            private_messages=private_messages,
            reel_messages=reel_messages,
        )

    @classmethod
    def _warmup_limits(cls, day: int) -> tuple[int, int, int, int, int]:
        for final_day, limits in cls._WARMUP_BRACKETS:
            if final_day is None or day <= final_day:
                return limits
        raise AssertionError("unreachable")



def _validate_counts(*values: int) -> None:
    if any(type(value) is not int or value < 0 for value in values):
        raise BudgetInputError("invalid nonnegative count")


def daily_window_start(now: datetime, timezone_name: str = "UTC") -> datetime:
    """The account's local midnight, expressed in UTC, including DST changes."""
    current = _as_utc(now)
    if current is None:
        raise BudgetInputError("invalid daily budget clock")
    zone = ZoneInfo(timezone_name)
    return current.astimezone(zone).replace(hour=0, minute=0, second=0,
                                          microsecond=0).astimezone(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except Exception:
        return None
