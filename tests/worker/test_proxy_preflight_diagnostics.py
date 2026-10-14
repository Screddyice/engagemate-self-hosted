"""Proxy diagnostics keep fixed reasons without arming an external action."""

from contextlib import contextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from engagemate.onboarding.profile_capture import ProfileCaptureError
from engagemate.proxy.iproyal import IPRoyalProvider
from engagemate.proxy.preflight import PREFLIGHT_REASONS, preflight_failure_evidence
from engagemate.worker import follow_runtime, follow_reel_runtime


@pytest.mark.parametrize("module", [follow_runtime, follow_reel_runtime])
@pytest.mark.parametrize("reason", sorted(PREFLIGHT_REASONS))
async def test_known_preflight_reason_stops_before_browser_or_mutation(monkeypatch, module, reason):
    reel = module is follow_reel_runtime
    performer = (module.LiveFollowReelPerformer if reel else module.LiveFollowPerformer)(None, None, None)
    performer._sender = Mock(return_value=("123", "sender"))
    performer._pause = Mock()
    monkeypatch.setattr(module.ProfileCaptureService, "_load", Mock(side_effect=ProfileCaptureError(reason)))
    factory = Mock()
    monkeypatch.setattr(module, "LeasedBrowserFactory", factory)
    claim = SimpleNamespace(user_id="owner", ig_account_id="account", owner_id="456",
                            sender_id="123", sender_handle="sender")
    before, prepare = AsyncMock(), AsyncMock()
    outcome, evidence = await (performer(claim, prepare, before) if reel else performer(claim, before))
    assert outcome == "deferred"
    assert evidence == {"reason": "proxy_or_session_unavailable", "preflight_reason": reason,
                        "phase": "proxy_preflight", "mutation_attempted": False}
    factory.assert_not_called()
    before.assert_not_awaited()
    prepare.assert_not_awaited()
    performer._pause.assert_not_called()


@pytest.mark.parametrize("module", [follow_runtime, follow_reel_runtime])
@pytest.mark.parametrize("reason", ["private_proxy_password", "http://user:password@proxy.invalid/",
                                    "proxy_access_denied\nBearer private_token", ["proxy_access_denied"]])
async def test_unknown_and_secret_bearing_reason_keeps_generic_evidence(monkeypatch, module, reason, caplog):
    reel = module is follow_reel_runtime
    performer = (module.LiveFollowReelPerformer if reel else module.LiveFollowPerformer)(None, None, None)
    performer._sender = Mock(return_value=("123", "sender"))
    performer._pause = Mock()
    monkeypatch.setattr(module.ProfileCaptureService, "_load", Mock(side_effect=ProfileCaptureError(reason)))
    factory = Mock()
    monkeypatch.setattr(module, "LeasedBrowserFactory", factory)
    claim = SimpleNamespace(user_id="owner", ig_account_id="account", owner_id="456",
                            sender_id="123", sender_handle="sender")
    before, prepare = AsyncMock(), AsyncMock()
    result = await (performer(claim, prepare, before) if reel else performer(claim, before))
    assert result == ("deferred", {"reason": "proxy_or_session_unavailable", "preflight_reason": None,
                                  "phase": None, "mutation_attempted": False})
    assert "private" not in json.dumps(result) + caplog.text
    assert "password" not in json.dumps(result) + caplog.text
    factory.assert_not_called()
    before.assert_not_awaited()
    prepare.assert_not_awaited()
    performer._pause.assert_not_called()


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("private proxy timeout"),
                                  httpx.ReadTimeout("private proxy timeout")])
def test_actual_provider_timeout_maps_to_fixed_connection_failure(error):
    @contextmanager
    def client(**_):
        yield SimpleNamespace(get=Mock(side_effect=error))

    endpoint = SimpleNamespace(username="test-user", password="test-password", server="http://proxy.invalid:1234")
    provider = IPRoyalProvider((endpoint,), health_url="https://health.invalid/",
                              connect_timeout=1, read_timeout=1, client_factory=client)
    health = provider.check(endpoint)
    assert health.reason == "connection_failed"
    assert preflight_failure_evidence(health.reason)["preflight_reason"] == "connection_failed"
