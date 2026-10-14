"""Terminal discovery failures remain reportable and do not restart traversal."""

from engagemate.worker.follower_discovery import FollowerDiscovery

from tests.worker.test_follower_discovery import Browser, Store, Stories, no_sleep, run


async def test_storage_failure_stops_with_a_report_and_no_retry():
    class UnavailableStore(Store):
        calls = 0

        async def next_candidate(self, *_):
            self.calls += 1
            raise RuntimeError("database unavailable")

    store = UnavailableStore()
    store.preloaded = True  # Reach the queue failure after source initialization.
    browser = Browser({}, {})
    loop = FollowerDiscovery(
        browser=browser, store=store, stories=Stories(), sleep=no_sleep
    )
    active = run("source")
    assert await loop.find(active, 1) == []
    assert loop.report() == {
        "stop_reason": "discovery_unavailable",
        "counters": {},
        "can_continue": False,
    }
    assert await loop.find(active, 1) == []
    assert store.calls == 1
    assert browser.calls == []


async def test_invalid_source_snapshot_stops_before_reading_queue_or_browser():
    browser = Browser({}, {})
    loop = FollowerDiscovery(
        browser=browser, store=Store(), stories=Stories(), sleep=no_sleep
    )
    active = run("source")
    active.config_snapshot["follower_sources"][0]["id"] = "invalid"
    assert await loop.find(active, 1) == []
    assert loop.report()["stop_reason"] == "source_approval_required"
    assert browser.calls == []
