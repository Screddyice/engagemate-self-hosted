"""A bounded check commits checked candidates and resumes from the saved queue."""

from engagemate.domain.models import Action
from engagemate.worker.check_deadline import CheckDeadline
from engagemate.worker.follower_discovery import FollowerDiscovery
from engagemate.worker.follower_store import PostgresFollowerStore
from tests.worker.test_check_deadline import Clock
from tests.worker.test_follower_discovery import Browser, Stories, profile, run
from tests.worker.test_follower_store import database_url, world  # noqa: F401


async def test_expired_check_resumes_saved_postgres_queue_without_action_claim(world):  # noqa: F811
    pool,store,context,_,_,source,_,_ = world
    candidates = [{"owner_id":str(i),"handle":f"user{i}"} for i in range(100,112)]
    assert await store.save_page(context,source,None,None,True,candidates)
    active,clock = run("source"),Clock()
    active.config_snapshot["follower_sources"][0]["id"] = str(source)
    active.discovery_context = context
    active.check_deadline = CheckDeadline(Action.REPLY,300,clock=clock)
    active.check_deadline.start()
    browser = Browser({}, {f"user{i}":profile(i,has_story=i==111) for i in range(100,112)})
    loop = FollowerDiscovery(browser=browser,store=store,stories=Stories(),sleep=clock.sleep,monotonic=clock)
    assert await loop.find(active,1) == []
    assert loop.report()["stop_reason"] == "time_limit"
    with pool.connection() as connection:
        checked = connection.execute("SELECT count(*) AS n FROM follower_candidates WHERE user_id=%s AND last_checked_at IS NOT NULL",(context.user_id,)).fetchone()["n"]
        assert 0 < checked < 12
        assert connection.execute("SELECT count(*) AS n FROM targets WHERE user_id=%s",(context.user_id,)).fetchone()["n"] == 0
    first = await store.next_candidate(context,[source],context.now)
    assert first["owner_id"] != "100"
    active.check_deadline = None
    resumed = FollowerDiscovery(browser=browser,store=PostgresFollowerStore(pool),stories=Stories(),sleep=clock.sleep,monotonic=clock)
    assert [o.fingerprint for o in await resumed.find(active,1)] == ["111"]
    assert browser.calls == []


async def test_real_timeout_keeps_completed_candidate_cooldown_and_pending_candidate_due(world):  # noqa: F811
    import asyncio

    pool,store,context,_,_,source,_,_ = world
    assert await store.save_page(context,source,None,None,True,[
        {"owner_id":"700","handle":"user700"},{"owner_id":"701","handle":"user701"},
    ])
    entered = asyncio.Event()

    class Network(Browser):
        async def inspect(self,handle):
            if handle == "user701":
                entered.set()
                await asyncio.Event().wait()
            return profile(700,has_story=False)

    active,clock = run("source"),Clock()
    active.config_snapshot["follower_sources"][0]["id"] = str(source)
    active.discovery_context = context
    active.check_deadline = CheckDeadline(Action.REPLY,300,clock=clock)
    active.check_deadline.start()
    clock.now += 28  # Two real seconds remain in the discovery slice.
    loop = FollowerDiscovery(browser=Network({},{}),store=store,stories=Stories(),delay_seconds=0)
    assert await asyncio.wait_for(loop.find(active,1),5) == []
    assert entered.is_set() and loop.report()["stop_reason"] == "time_limit"
    first = await PostgresFollowerStore(pool).next_candidate(context,[source],context.now)
    assert first["owner_id"] == "701"
    with pool.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM targets WHERE user_id=%s",(context.user_id,)).fetchone()["n"] == 0
