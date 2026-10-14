from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from engagemate.domain.budgets import ActionBudget
from engagemate.worker.follower_discovery import FollowerDiscovery, prospect_reason


async def no_sleep(_):
    pass


class Store:
    def __init__(self):
        self.queue = deque()
        self.states = {}
        self.admitted = []
        self.reasons = []
        self.preloaded = False

    async def source_state(self, _, source):
        return self.states.get(
            source,
            {
                "cursor": None,
                "exhausted": False,
                # A preloaded queue models a source that was already scanned
                # before this discovery object was created.
                "initialized": self.preloaded,
            },
        )

    async def save_page(self, _, source, expected, cursor, exhausted, candidates):
        self.states[source] = {
            "cursor": cursor,
            "exhausted": exhausted,
            "initialized": True,
        }
        self.queue.extend(c for c in candidates if c["owner_id"] not in self.admitted)
        return True

    async def next_candidate(self, *_):
        return self.queue.popleft() if self.queue else None

    async def mark_checked(self, _, owner, reason, now):
        self.reasons.append((owner, reason))

    async def admit_target(self, _, sources, owner, handle, now):
        self.admitted.append(owner)
        return NS(owner_id=owner)


class Browser:
    def __init__(self, pages, profiles):
        self.pages, self.profiles = pages, profiles
        self.calls = []

    async def followers(self, handle, cursor):
        self.calls.append((handle, cursor))
        return self.pages[(handle, cursor)]

    async def inspect(self, handle):
        return self.profiles[handle]


class Stories:
    async def find(self, context, targets, budget):
        return [NS(fingerprint=targets[0].owner_id)]


def page(*owners, cursor=None, exhausted=True):
    return {
        "candidates": [{"owner_id": str(i), "handle": f"user{i}"} for i in owners],
        "next_cursor": cursor,
        "exhausted": exhausted,
    }


def profile(owner, **changes):
    return {
        "owner_id": str(owner),
        "handle": f"user{owner}",
        "following": False,
        "followed_by": False,
        "private": False,
        "has_story": True,
    } | changes


def run(*handles):
    async def live():
        return True

    async def policy(_):
        return None

    return NS(
        config_snapshot={
            "follower_sources": [{"id": str(uuid4()), "username": h} for h in handles]
        },
        context=NS(username="sender"),
        discovery_context=NS(now=datetime.now(UTC)),
        budget=ActionBudget(2, 0, 0, 2),
        lease_is_live=live,
        execution_mode="autonomous",
        tool_repository=NS(discovery_stop_reason=policy),
    )


@pytest.mark.asyncio
async def test_keeps_scanning_past_empty_and_existing_network_until_budget():
    store = Store()
    profiles = {f"user{i}": profile(i, has_story=i >= 9) for i in range(12)}
    profiles["user9"]["following"] = True
    browser = Browser({("inspiration", None): page(*range(12))}, profiles)
    loop = FollowerDiscovery(
        browser=browser, store=store, stories=Stories(), sleep=no_sleep
    )
    result = await loop.find(run("inspiration"), 25)
    assert [o.fingerprint for o in result] == ["10", "11"]
    assert loop.counters["checked"] == 12
    assert store.admitted == ["10", "11"]
    assert loop.counters["no_story"] == 9
    assert loop.counters["excluded"] == 1


@pytest.mark.asyncio
async def test_repeat_discover_resumes_and_round_robins_sources():
    browser = Browser(
        {("first", None): page(1), ("second", None): page(2)},
        {"user1": profile(1), "user2": profile(2)},
    )
    loop = FollowerDiscovery(
        browser=browser, store=Store(), stories=Stories(), sleep=no_sleep
    )
    active = run("first", "second")
    assert [o.fingerprint for o in await loop.find(active, 1)] == ["1"]
    assert [o.fingerprint for o in await loop.find(active, 1)] == ["2"]
    assert await loop.find(active, 1) == []
    assert loop.stop_reason == "source_exhausted"
    assert browser.calls == [("first", None), ("second", None)]


@pytest.mark.asyncio
async def test_first_discovery_primes_every_approved_source_before_selecting():
    browser = Browser(
        {("first", None): page(1), ("second", None): page(2)},
        {"user1": profile(1), "user2": profile(2)},
    )
    loop = FollowerDiscovery(
        browser=browser, store=Store(), stories=Stories(), sleep=no_sleep
    )
    active = run("first", "second")

    result = await loop.find(active, 1)

    assert [opportunity.fingerprint for opportunity in result] == ["1"]
    assert browser.calls == [("first", None), ("second", None)]


@pytest.mark.asyncio
async def test_priming_empty_source_still_loads_next_source():
    browser = Browser(
        {("first", None): page(), ("second", None): page(2)},
        {"user2": profile(2)},
    )
    loop = FollowerDiscovery(
        browser=browser, store=Store(), stories=Stories(), sleep=no_sleep
    )
    active = run("first", "second")

    result = await loop.find(active, 1)

    assert [opportunity.fingerprint for opportunity in result] == ["2"]
    assert browser.calls == [("first", None), ("second", None)]


@pytest.mark.asyncio
async def test_empty_nonterminal_page_advances_cursor():
    browser = Browser(
        {
            ("source", None): page(cursor="next", exhausted=False),
            ("source", "next"): page(1),
        },
        {"user1": profile(1)},
    )
    loop = FollowerDiscovery(
        browser=browser, store=Store(), stories=Stories(), sleep=no_sleep
    )
    assert len(await loop.find(run("source"), 1)) == 1
    assert browser.calls == [("source", None), ("source", "next")]


@pytest.mark.asyncio
async def test_check_limit_stops_without_claiming_budget_filled():
    browser = Browser(
        {("source", None): page(1, 2)}, {"user1": profile(1, has_story=False)}
    )
    loop = FollowerDiscovery(
        browser=browser, store=Store(), stories=Stories(), sleep=no_sleep, max_checks=1
    )
    assert await loop.find(run("source"), 2) == []
    assert loop.report()["stop_reason"] == "scan_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,budget",
    [("disabled", ActionBudget(1, 0, 0, 1)), ("autonomous", ActionBudget(0, 0, 0, 0))],
)
async def test_no_browser_calls_with_disabled_execution_or_empty_budget(mode, budget):
    browser = Browser({}, {})
    loop = FollowerDiscovery(
        browser=browser, store=Store(), stories=Stories(), sleep=no_sleep
    )
    active = run("source")
    active.execution_mode, active.budget = mode, budget
    assert await loop.find(active, 2) == []
    assert browser.calls == []


@pytest.mark.asyncio
async def test_revoked_source_approval_during_pacing_stops_before_browser():
    active = run("source")

    async def revoked(_):
        return "source_changed"

    async def revoke(_):
        active.tool_repository.discovery_stop_reason = revoked

    browser = Browser({}, {})
    loop = FollowerDiscovery(
        browser=browser, store=Store(), stories=Stories(), sleep=revoke
    )
    assert await loop.find(active, 1) == []
    assert loop.stop_reason == "source_changed"
    assert browser.calls == []


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"following": True}, "excluded"),
        ({"followed_by": True}, "excluded"),
        ({"followed_by": None}, "unknown"),
        ({"following": 0}, "unknown"),
        ({"owner_id": "other"}, "identity_mismatch"),
        ({"private": True}, "excluded"),
        ({"has_story": None}, "unknown"),
        ({"handle": "SENDER"}, "excluded"),
    ],
)
def test_fail_closed_prospect_admission(change, reason):
    assert prospect_reason(profile(1, **change), "1", {"sender"}) == reason


@pytest.mark.parametrize("privacy", [True, None, 0, "false"])
async def test_private_and_unknown_profiles_are_skipped_before_story_preview(privacy):
    store = Store()
    browser = Browser(
        {("source", None): page(1, 2)},
        {"user1": profile(1, private=privacy), "user2": profile(2)},
    )
    previews = []

    class CheckedStories:
        async def find(self, context, targets, budget):
            previews.extend(target.owner_id for target in targets)
            assert targets[0].owner_id == "2"
            return [NS(fingerprint=targets[0].owner_id)]

    loop = FollowerDiscovery(
        browser=browser, store=store, stories=CheckedStories(), sleep=no_sleep
    )
    result = await loop.find(run("source"), 1)
    assert [opportunity.fingerprint for opportunity in result] == ["2"]
    assert previews == ["2"]
    assert store.admitted == ["2"]
    assert store.reasons == [
        ("1", "excluded" if privacy is True else "unknown"), ("2", "available"),
    ]


@pytest.mark.parametrize("reason", ["profile_unreadable", "rate_limited", "authentication_required", "checkpoint_required"])
async def test_unreadable_recipient_advances_but_restrictions_stop(reason):
    from engagemate.worker.follower_browser import FollowerBrowserError
    store = Store()
    store.queue.extend([{"owner_id": "1", "handle": "user1"}, {"owner_id": "2", "handle": "user2"}])
    store.preloaded = True
    class SelectiveBrowser(Browser):
        async def inspect(self, handle):
            if handle == "user1":
                raise FollowerBrowserError(reason)
            return profile(2)
    discovery = FollowerDiscovery(browser=SelectiveBrowser({}, {}), store=store, stories=Stories(), sleep=no_sleep)
    results = await discovery.find(run("source"), 1)
    if reason == "profile_unreadable":
        assert [r.fingerprint for r in results] == ["2"]
        assert store.reasons == [("1", "unknown"), ("2", "available")]
        assert store.admitted == ["2"]
    else:
        assert results == []
        assert store.admitted == []
        assert discovery.stop_reason == reason


class FlakyStories:
    """Stories whose reads fail for the listed owners with the given reason."""

    def __init__(self, failing, reason="story_unreadable"):
        self.failing, self.reason = set(failing), reason

    async def find(self, context, targets, budget):
        from engagemate.worker.discovery import DiscoveryError
        owner = targets[0].owner_id
        if owner in self.failing:
            raise DiscoveryError("story discovery failed", reason=self.reason)
        return [NS(fingerprint=owner)]


def _discovery_for(owners, stories):
    store = Store()
    store.queue.extend({"owner_id": str(i), "handle": f"user{i}"} for i in owners)
    store.preloaded = True
    profiles = {f"user{i}": profile(i) for i in owners}
    discovery = FollowerDiscovery(
        browser=Browser({}, profiles), store=store, stories=stories, sleep=no_sleep
    )
    return discovery, store


@pytest.mark.asyncio
async def test_one_unreadable_story_skips_that_prospect():
    """2026-09-17: one unreadable Story stopped a run holding seven opportunities."""
    discovery, store = _discovery_for([1, 2, 3], FlakyStories({"1"}))
    results = await discovery.find(run("source"), 2)
    assert [r.fingerprint for r in results] == ["2", "3"]
    assert discovery.stop_reason is None
    assert discovery.counters["story_unreadable"] == 1
    assert ("1", "available") in store.reasons


@pytest.mark.asyncio
async def test_consecutive_unreadable_stories_still_stop_discovery():
    discovery, _ = _discovery_for([1, 2, 3, 4], FlakyStories({"1", "2", "3"}))
    results = await discovery.find(run("source"), 2)
    assert results == []
    assert discovery.stop_reason == "story_unreadable"
    assert discovery.report()["can_continue"] is False


@pytest.mark.asyncio
async def test_three_unavailable_stories_continue_to_healthy_opportunity():
    discovery, store = _discovery_for(
        [1, 2, 3, 4], FlakyStories({"1", "2", "3"}, "story_unavailable")
    )
    results = await discovery.find(run("source"), 1)
    assert [r.fingerprint for r in results] == ["4"]
    assert discovery.stop_reason is None and discovery.story_failures == 0
    assert discovery.counters["story_unavailable"] == 3
    assert discovery.counters["story_unreadable"] == 0
    assert [owner for owner, _ in store.reasons] == ["1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_explicit_unavailable_read_resets_unknown_failure_streak():
    from engagemate.worker.discovery import DiscoveryError

    class MixedStories:
        async def find(self, context, targets, budget):
            owner = targets[0].owner_id
            if owner != "6":
                reason = "story_unavailable" if owner == "3" else "story_unreadable"
                raise DiscoveryError("story discovery failed", reason=reason)
            return [NS(fingerprint=owner)]

    discovery, _ = _discovery_for(range(1, 7), MixedStories())
    results = await discovery.find(run("source"), 1)
    assert [r.fingerprint for r in results] == ["6"]
    assert discovery.stop_reason is None
    assert discovery.counters["story_unreadable"] == 4
    assert discovery.counters["story_unavailable"] == 1


@pytest.mark.asyncio
async def test_a_readable_story_resets_the_failure_run():
    discovery, _ = _discovery_for([1, 2, 3, 4, 5], FlakyStories({"1", "2", "4", "5"}))
    results = await discovery.find(run("source"), 3)
    assert [r.fingerprint for r in results] == ["3"]
    # The fake browser has no follower pages, so the drained queue ends in
    # followers_unreadable. Two failures, a success, then two more never stop.
    assert discovery.stop_reason != "story_unreadable"
    assert discovery.counters["story_unreadable"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["rate_limited", "authentication_required", "checkpoint_required"])
async def test_a_restricted_story_read_stops_at_once(reason):
    discovery, _ = _discovery_for([1, 2], FlakyStories({"1"}, reason))
    results = await discovery.find(run("source"), 2)
    assert results == []
    assert discovery.stop_reason == reason
