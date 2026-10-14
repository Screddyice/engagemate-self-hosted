"""Keep the public worker import on the validated reader and transport."""
from engagemate.worker.follow_reel_browser import FollowReelBrowser, ReelBrowserError, verified_thread_item
from engagemate.worker.follow_reel_reader import FollowReelBrowser as Reader
from engagemate.worker import follow_reel_transport as transport


def test_worker_adapter_uses_validated_runtime():
    assert FollowReelBrowser is Reader
    assert ReelBrowserError is transport.ReelBrowserError
    assert verified_thread_item is transport.verified_thread_item
