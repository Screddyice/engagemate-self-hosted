import asyncio
from pathlib import Path

import pytest
from engagemate.worker.instagram_adapter import (
    ActionOutcome,
    DiagnosticArtifact,
    HealthSignal,
    InstagramAdapter,
)

FIXTURE = Path(__file__).parent / "fixtures" / "instagram_active_story.html"
FINGERPRINT = "ab" * 32


class FakeLocator:
    def __init__(self, page, nodes):
        self.page = page
        self.nodes = nodes

    async def count(self):
        return len(self.nodes)

    async def is_visible(self):
        return len(self.nodes) == 1 and not self.nodes[0].get("hidden", False)

    async def get_attribute(self, name):
        return self.nodes[0].get(name)

    async def click(self):
        node = self.nodes[0]
        self.page.clicks.append(node["data-testid"])
        if node["data-testid"] == "story-like" and not self.page.prevent_confirmation:
            node["aria-pressed"] = "true"
        if node["data-testid"] in {"story-reply-send", "story-dm-send"}:
            self.page.status.update(
                {"data-status": "sent", "data-action": self.page.ack_action}
            )

    async def fill(self, value):
        if self.page.cancel_fill:
            raise asyncio.CancelledError
        self.page.fills.append(value)

    def locator(self, selector):
        return self.page.locator(selector)


class FakePage:
    def __init__(self, *, owner="target.one", fingerprint=FINGERPRINT):
        self.root = {
            "data-story-owner": owner,
            "data-story-fingerprint": fingerprint,
        }
        self.nodes = {
            "story-like": [{"data-testid": "story-like", "aria-pressed": "false"}],
            "story-reply": [{"data-testid": "story-reply"}],
            "story-reply-send": [{"data-testid": "story-reply-send", "data-recipient": owner}],
            "story-dm": [{"data-testid": "story-dm", "data-recipient": owner}],
            "story-dm-input": [{"data-testid": "story-dm-input"}],
            "story-dm-send": [{"data-testid": "story-dm-send", "data-recipient": owner}],
        }
        self.status = {
            "data-testid": "story-action-status",
            "data-status": "idle",
            "data-recipient": owner,
            "data-story-fingerprint": fingerprint,
        }
        self.clicks = []
        self.fills = []
        self.prevent_confirmation = False
        self.cancel_fill = False
        self.ack_action = None
        self.mask_selectors = []
        self.url = "https://www.instagram.com/stories/target.one/"
        self.secret = "Bearer diagnostic-secret generated-secret caption-secret"

    def locator(self, selector):
        self.mask_selectors.append(selector)
        if selector == '[data-engagemate-active-story="true"]':
            return FakeLocator(self, [self.root])
        if not selector.startswith('[data-testid="'):
            return FakeLocator(self, [])
        testid = selector.split('"')[1]
        if testid == "story-action-status":
            return FakeLocator(self, [self.status])
        if testid == "account-health":
            return FakeLocator(self, self.nodes.get("health", []))
        return FakeLocator(self, self.nodes.get(testid, []))

    async def screenshot(self, *, mask):
        assert mask
        return b"masked-pixels"

    async def safe_dom_excerpt(self, allowed_attributes, max_bytes):
        assert "value" not in allowed_attributes
        return '<section role="dialog" data-status="failed">'


class Sink:
    def __init__(self):
        self.items = []

    async def write(self, artifact):
        self.items.append(artifact)


@pytest.mark.asyncio
async def test_fixture_and_fake_drive_exact_one_send_actions():
    assert "data-engagemate-active-story" in FIXTURE.read_text()
    for method, text, expected in [
        ("like_story", None, ["story-like"]),
        ("reply_to_story", "safe reply", ["story-reply-send"]),
        ("dm_about_story", "safe dm", ["story-dm", "story-dm-send"]),
    ]:
        page = FakePage()
        page.ack_action = (
            "reply"
            if method == "reply_to_story"
            else "dm"
            if method == "dm_about_story"
            else None
        )
        result = await getattr(InstagramAdapter(page, Sink()), method)(
            "target.one", FINGERPRINT, *(() if text is None else (text,))
        )
        assert result is ActionOutcome.CONFIRMED
        assert page.clicks == expected
        assert page.fills == ([] if text is None else [text])


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,fingerprint", [("wrong", FINGERPRINT), ("target.one", "wrong")])
async def test_action_rejects_wrong_owner_or_story_before_click(owner, fingerprint):
    page = FakePage(owner=owner, fingerprint=fingerprint)
    result = await InstagramAdapter(page, Sink()).like_story("target.one", FINGERPRINT)
    assert result is ActionOutcome.FAILED
    assert page.clicks == []


@pytest.mark.asyncio
async def test_action_rejects_missing_duplicate_hidden_and_confirmation_failure():
    for mutation in ("missing", "duplicate", "hidden", "no-confirm"):
        page = FakePage()
        if mutation == "missing":
            page.nodes["story-like"] = []
        elif mutation == "duplicate":
            page.nodes["story-like"] *= 2
        elif mutation == "hidden":
            page.nodes["story-like"][0]["hidden"] = True
        else:
            page.prevent_confirmation = True
        result = await InstagramAdapter(page, Sink()).like_story("target.one", FINGERPRINT)
        assert result is ActionOutcome.FAILED


@pytest.mark.asyncio
async def test_reply_and_dm_reject_wrong_recipient_before_fill_or_send():
    for method, control in [
        ("reply_to_story", "story-reply-send"),
        ("dm_about_story", "story-dm-send"),
    ]:
        page = FakePage()
        page.nodes[control][0]["data-recipient"] = "someone.else"
        result = await getattr(InstagramAdapter(page, Sink()), method)(
            "target.one", FINGERPRINT, "generated secret text"
        )
        assert result is ActionOutcome.FAILED
        assert page.clicks == ([] if method == "reply_to_story" else ["story-dm"])
        assert page.fills == []


@pytest.mark.asyncio
async def test_failure_diagnostic_is_bounded_and_secret_safe():
    page, sink = FakePage(), Sink()
    page.nodes["story-like"][0]["click_error"] = object()

    async def fail():
        secret = "Bearer traceback-secret"
        raise RuntimeError(page.secret + secret)

    page.locator = lambda selector, original=page.locator: (
        type("Bad", (), {"count": lambda self: fail()})()
        if selector == '[data-engagemate-active-story="true"]'
        else original(selector)
    )
    result = await InstagramAdapter(page, sink).like_story("target.one", FINGERPRINT)
    assert result is ActionOutcome.FAILED
    assert len(sink.items) == 1
    artifact = sink.items[0]
    assert isinstance(artifact, DiagnosticArtifact)
    graph = repr(artifact) + artifact.dom_excerpt + repr(artifact.screenshot)
    assert "secret" not in graph.lower()
    assert len(artifact.screenshot) <= 1_000_000
    assert len(artifact.dom_excerpt.encode()) <= 16_384


@pytest.mark.asyncio
async def test_diagnostic_rebuilds_allowlisted_dom_and_strips_url_secrets():
    page, sink = FakePage(owner="wrong"), Sink()
    page.url = "https://user:password@www.instagram.com/stories/target.one/?token=secret#private"

    async def malicious_excerpt(allowed_attributes, max_bytes):
        return (
            '<section role="dialog" onclick="Bearer attr-secret">'
            'Bearer text-secret<input value="generated-secret">'
            '<div data-status="failed" aria-label="bounded"></div></section>'
        )

    page.safe_dom_excerpt = malicious_excerpt
    await InstagramAdapter(page, sink).like_story("target.one", FINGERPRINT)
    artifact = sink.items[0]
    exposed = artifact.dom_excerpt + artifact.current_url
    assert "secret" not in exposed.lower()
    assert "onclick" not in exposed
    assert "value" not in exposed
    assert artifact.current_url == "https://www.instagram.com/stories/target.one/"
    assert "body" in page.mask_selectors


@pytest.mark.asyncio
async def test_cancellation_propagates_without_diagnostic():
    page, sink = FakePage(), Sink()

    async def cancel():
        raise asyncio.CancelledError

    page.locator = lambda selector: type("Cancelled", (), {"count": lambda self: cancel()})()
    with pytest.raises(asyncio.CancelledError):
        await InstagramAdapter(page, sink).like_story("target.one", FINGERPRINT)
    assert sink.items == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["reply_to_story", "dm_about_story"])
async def test_text_action_cancellation_traceback_locals_redact_generated_text(method):
    page, sink = FakePage(), Sink()
    page.cancel_fill = True
    plaintext = "private generated reply"
    try:
        await getattr(InstagramAdapter(page, sink), method)(
            "target.one", FINGERPRINT, plaintext
        )
    except asyncio.CancelledError as error:
        frames = []
        traceback = error.__traceback__
        while traceback is not None:
            if "/api/engagemate/" in traceback.tb_frame.f_code.co_filename:
                frames.append(
                    {
                        name: repr(value)
                        for name, value in traceback.tb_frame.f_locals.items()
                    }
                )
            traceback = traceback.tb_next
        assert plaintext not in repr(frames)
    else:
        raise AssertionError("cancellation did not propagate")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["reply_to_story", "dm_about_story"])
async def test_text_action_rejects_stale_or_wrong_action_sent_ack(method):
    for initial_action in ("reply", "dm", "like"):
        page = FakePage()
        page.status.update({"data-status": "sent", "data-action": initial_action})
        result = await getattr(InstagramAdapter(page, Sink()), method)(
            "target.one", FINGERPRINT, "safe text"
        )
        assert result is ActionOutcome.FAILED
        assert page.clicks == []
        assert page.fills == []

    page = FakePage()
    expected = "reply" if method == "reply_to_story" else "dm"
    wrong = "dm" if expected == "reply" else "reply"
    page.ack_action = wrong
    result = await getattr(InstagramAdapter(page, Sink()), method)(
        "target.one", FINGERPRINT, "safe text"
    )
    assert result is ActionOutcome.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("challenge", HealthSignal.CHALLENGE),
        ("checkpoint", HealthSignal.CHECKPOINT),
        ("login_required", HealthSignal.LOGIN_REQUIRED),
        ("suspicious_activity", HealthSignal.SUSPICIOUS_ACTIVITY),
        ("rate_limit", HealthSignal.RATE_LIMIT),
        ("network_error", HealthSignal.NETWORK_ERROR),
        ("proxy_error", HealthSignal.PROXY_ERROR),
        ("none", HealthSignal.NONE),
    ],
)
async def test_health_detection_uses_only_strict_semantic_signal(raw, expected):
    page = FakePage()
    page.nodes["health"] = [] if raw is None else [{"data-testid": "account-health", "data-health-signal": raw}]
    assert await InstagramAdapter(page, Sink()).detect_health_signal() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("nodes", [[], [{"hidden": True}], [{}, {}], [{}], [{"data-health-signal": "unexpected"}]])
async def test_missing_or_unreadable_health_never_means_healthy(nodes):
    page = FakePage()
    page.nodes["health"] = nodes
    signal = await InstagramAdapter(page, Sink()).detect_health_signal()
    assert signal.value == "health_unavailable"


@pytest.mark.asyncio
async def test_health_probe_failure_is_bounded_and_not_healthy():
    page = FakePage()

    def failed_locator(selector):
        raise RuntimeError("private browser detail")

    page.locator = failed_locator
    signal = await InstagramAdapter(page, Sink()).detect_health_signal()
    assert signal.value == "health_unavailable"
