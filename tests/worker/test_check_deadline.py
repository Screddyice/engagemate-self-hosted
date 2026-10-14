"""The operator clock reserves ordinary pacing and never resets per tool."""

from types import SimpleNamespace as NS

import pytest

from engagemate.domain.models import Action
from engagemate.domain.pacing import PacingPolicy
from engagemate.worker.active_run import ActiveRunRegistry
from engagemate.worker.check_deadline import CheckDeadline, DeadlineRunner
from engagemate.worker.execution_mcp_server import build_execution_mcp
from tests.worker.test_execution_mcp_tools import KEY, _active_run
from tests.worker.test_follower_discovery import Browser, FollowerDiscovery, Store, Stories, page, profile, run


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


@pytest.mark.parametrize("action,window", [(Action.REPLY,30), (Action.LIKE,135), (Action.VIEW,135)])
def test_reserves_full_pacing_and_all_stages_in_same_300_seconds(action, window):
    clock = Clock()
    deadline = CheckDeadline(action,300,clock=clock)
    assert deadline.remaining() == 0
    deadline.start()
    assert deadline.discovery_remaining() == window
    clock.now += 25
    assert deadline.discovery_remaining() == max(0,window-25)
    with pytest.raises(RuntimeError):
        deadline.start()
    clock.now += 300
    assert deadline.remaining() == deadline.discovery_remaining() == 0


async def test_starts_before_any_claim_context_or_browser_work():
    deadline = CheckDeadline(Action.REPLY,300)

    async def once(worker):
        assert worker == "scope"
        assert 299 <= deadline.remaining() <= 300
        return "completed"

    runner = DeadlineRunner(NS(run_once=once),deadline)
    assert await runner.run_once("scope") == "completed"
    with pytest.raises(RuntimeError):
        await runner.run_once("scope")


async def test_bounded_discovery_advances_checked_queue_and_next_run_continues():
    clock, store = Clock(), Store()
    browser = Browser({("source",None):page(*range(10))},
                      {f"user{i}":profile(i,has_story=i==9) for i in range(10)})
    active = run("source")
    active.check_deadline = CheckDeadline(Action.REPLY,300,clock=clock)
    active.check_deadline.start()
    loop = FollowerDiscovery(browser=browser,store=store,stories=Stories(),sleep=clock.sleep,monotonic=clock)
    assert await loop.find(active,1) == []
    assert loop.report()["stop_reason"] == "time_limit"
    assert 0 < len(store.reasons) < 10
    assert not store.admitted
    # A new check shares the queue/cursor, not the expired clock.
    active.check_deadline = None
    resumed = FollowerDiscovery(browser=browser,store=store,stories=Stories(),sleep=clock.sleep,monotonic=clock)
    assert [o.fingerprint for o in await resumed.find(active,1)] == ["9"]
    assert browser.calls == [("source",None)]


async def test_discovery_network_wait_is_bounded_and_normal_run_unchanged():
    import asyncio

    class Blocked(Browser):
        async def followers(self,*args):
            await asyncio.Event().wait()

    active = run("source")
    # Shift a300-second clock to leave only a tiny discovery slice.
    clock = Clock()
    active.check_deadline = CheckDeadline(Action.REPLY,300,clock=clock)
    active.check_deadline.start()
    clock.now += 29.99
    loop = FollowerDiscovery(browser=Blocked({},{}),store=Store(),stories=Stories(),delay_seconds=0)
    assert await asyncio.wait_for(loop.find(active,1),0.5) == []
    assert loop.report()["stop_reason"] == "time_limit"
    assert loop.max_seconds == 300


@pytest.mark.parametrize("late", [False,True])
async def test_reply_admission_uses_actual_unchanged_pacing_and_has_no_late_claim(late):
    registry, active, clock = ActiveRunRegistry(), _active_run(), Clock()
    active.context.pacing_seed = "fixture-seed"
    active.check_deadline = CheckDeadline(Action.REPLY,300,clock=clock)
    active.check_deadline.start()
    server = build_execution_mcp(registry,KEY)
    claims, pacing = [], []
    original_begin = active.tool_repository.begin_mutation

    async def begin(**values):
        claims.append(values)
        return await original_begin(**values)

    async def pace(action,opportunity):
        seconds = PacingPolicy.delay(action,f"fixture-seed:{opportunity.fingerprint}").total_seconds()
        pacing.append(seconds)
        await clock.sleep(seconds)

    active.tool_repository.begin_mutation, active.pace = begin, pace
    with registry.activate(active):
        discovered = await server.call_tool("instagram.discover",{})
        opaque = discovered.structured_content["opportunities"][0]["opportunity_id"]
        if late:
            clock.now += 253.157
        result = await server.call_tool("instagram.reply",{
            "opportunity_id":opaque,"idempotency_key":"deadline_reply_0001",
            "generated_text":"Your caption looks interesting! What inspired the caption?",
            "story_detail":"caption",
        })
    if late:
        assert result.structured_content == {"status":"denied","reason":"production_check_time_limit"}
        assert claims == pacing == active.executor.calls == active.tool_repository.finished == []
        assert active.budget.replies == 1
    else:
        assert result.structured_content["status"] == "confirmed"
        assert len(claims) == len(pacing) == len(active.executor.calls) == 1
        assert 60 <= pacing[0] <= 180
        assert active.check_deadline.remaining() >= 60


@pytest.mark.parametrize("stage", ["claim", "post_pace", "transition"])
async def test_waits_cannot_spend_the_execution_reserve(stage):
    registry,active,clock = ActiveRunRegistry(),_active_run(),Clock()
    active.context.pacing_seed = "fixture-seed"
    active.check_deadline = CheckDeadline(Action.LIKE,300,clock=clock)
    active.check_deadline.start()
    server = build_execution_mcp(registry,KEY)
    original = active.tool_repository.begin_mutation

    async def begin(**values):
        if stage == "claim":
            clock.now += 250
        return await original(**values)

    async def pace(*args):
        if stage == "post_pace":
            clock.now += 250

    async def transition(**values):
        if stage == "transition":
            clock.now += 250
        return True

    active.tool_repository.begin_mutation,active.pace = begin,pace
    active.tool_repository.mark_execution_started = transition
    with registry.activate(active):
        discovered = await server.call_tool("instagram.discover",{})
        result = await server.call_tool("instagram.like",{
            "opportunity_id":discovered.structured_content["opportunities"][0]["opportunity_id"],
            "idempotency_key":"late_wait_fixture_0001",
        })
    assert result.structured_content == {"status":"denied","reason":"production_check_time_limit"}
    assert active.executor.calls == []
    assert active.tool_repository.finished[0]["status"] == "denied"
    # The transition stage has already marked the mutation executable, so its
    # allocation stays reserved. Earlier denials remain available.
    assert active.budget.likes == (0 if stage == "transition" else 1)
