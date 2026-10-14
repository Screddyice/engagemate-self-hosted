import asyncio
from datetime import datetime, timezone
from uuid import UUID

import pytest
from engagemate.db.executions import UsageLimitExceeded
from engagemate.intelligence.schemas import EngagementDecision
from engagemate.worker.circuit_breaker import HealthSignal
from engagemate.worker.executor import (
    ExecutionContext,
    ExecutionResultStatus,
    Executor,
)
from engagemate.worker.instagram_adapter import ActionOutcome
from engagemate.worker.story_types import StoryContent, StoryMediaType, StoryOpportunity

USER = UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT = UUID("00000000-0000-0000-0000-000000000002")
TARGET = UUID("00000000-0000-0000-0000-000000000003")
ENGAGEMENT = UUID("00000000-0000-0000-0000-000000000004")
NOW = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
FINGERPRINT = "ab" * 32


def opportunity():
    return StoryOpportunity(TARGET, "owner", "target.one", "vip", "media", NOW, FINGERPRINT, StoryContent(StoryMediaType.IMAGE, None, None, None, ()))


def decision(action="like"):
    return EngagementDecision(
        engage=True, action=action,
        generated_text=None if action == "like" else "hello",
        confidence=0.9, rationale="relevant", model_used="claude-sonnet-4-6",
    )


class Claim:
    def __init__(self, duplicate=False):
        self.duplicate, self.engagement_id = duplicate, ENGAGEMENT


class Repo:
    def __init__(self, duplicate=False, claim_error=None):
        self.duplicate, self.claim_error, self.calls = duplicate, claim_error, []

    async def claim(self, metadata):
        self.calls.append("claim")
        if self.claim_error is not None:
            raise self.claim_error
        return Claim(self.duplicate)

    async def mark_sent(self, *args):
        self.calls.append("sent")
        return True

    async def mark_failed(self, *args):
        self.calls.append("failed")
        return True

    async def mark_skipped(self, *args):
        self.calls.append(("skipped", args[-1]))
        return True

    async def persist_breaker(self, *args):
        self.calls.append("breaker")


class Adapter:
    def __init__(self, outcome=ActionOutcome.CONFIRMED, signal=HealthSignal.NONE):
        self.outcome, self.signal, self.calls = outcome, signal, []

    async def like_story(self, *args):
        self.calls.append(("like", args)); return self.outcome

    async def reply_to_story(self, *args):
        self.calls.append(("reply", args)); return self.outcome

    async def dm_about_story(self, *args):
        self.calls.append(("dm", args)); return self.outcome

    async def detect_health_signal(self):
        self.calls.append(("health", ())); return self.signal


class Close:
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    async def close(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Bearer close-secret")


@pytest.mark.asyncio
async def test_duplicate_skips_without_adapter_use():
    repo, adapter, close = Repo(True), Adapter(), Close()
    result = await Executor(repo, adapter).execute(opportunity(), decision(), ExecutionContext(USER, ACCOUNT, NOW, close))
    assert result.status is ExecutionResultStatus.SKIPPED
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_usage_limit_skips_without_adapter_use():
    repo = Repo(claim_error=UsageLimitExceeded("private budget detail"))
    adapter = Adapter()
    result = await Executor(repo, adapter).execute(
        opportunity(), decision(), ExecutionContext(USER, ACCOUNT, NOW, Close())
    )
    assert result.status is ExecutionResultStatus.SKIPPED
    assert result.engagement_id is None
    assert result.failure_code == "usage_limit_reached"
    assert adapter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["like", "reply", "dm"])
async def test_executes_exactly_one_action_then_marks_sent(action):
    repo, adapter, close = Repo(), Adapter(), Close()
    result = await Executor(repo, adapter).execute(opportunity(), decision(action), ExecutionContext(USER, ACCOUNT, NOW, close))
    assert result.status is ExecutionResultStatus.SENT
    assert [call[0] for call in adapter.calls] == ["health", action, "health"]
    assert repo.calls == ["claim", "sent"]


@pytest.mark.asyncio
async def test_reply_requires_confirmed_follow_before_story_action():
    repo, adapter, close = Repo(), Adapter(), Close()
    events = []

    async def follow_first(engagement_id, context):
        events.append("follow_confirmed")
        assert engagement_id == ENGAGEMENT
        return True

    async def reply(*args):
        events.append("reply")
        return ActionOutcome.CONFIRMED

    adapter.reply_to_story = reply
    result = await Executor(repo, adapter).execute(
        opportunity(), decision("reply"), ExecutionContext(USER, ACCOUNT, NOW, close),
        before_action=follow_first,
    )

    assert result.status is ExecutionResultStatus.SENT
    assert events == ["follow_confirmed", "reply"]
    assert repo.calls == ["claim", "sent"]


@pytest.mark.asyncio
async def test_reply_is_skipped_when_follow_receipt_is_not_confirmed():
    repo, adapter, close = Repo(), Adapter(), Close()

    async def follow_first(*args):
        return False

    result = await Executor(repo, adapter).execute(
        opportunity(), decision("reply"), ExecutionContext(USER, ACCOUNT, NOW, close),
        before_action=follow_first,
    )

    assert result.status is ExecutionResultStatus.SKIPPED
    assert result.failure_code == "reply_follow_unconfirmed"
    assert [call[0] for call in adapter.calls] == ["health"]
    assert repo.calls == ["claim", "failed"]


@pytest.mark.asyncio
async def test_breaker_persists_marks_failed_and_closes_in_order():
    repo, adapter, close = Repo(), Adapter(signal=HealthSignal.CHALLENGE), Close()
    result = await Executor(repo, adapter).execute(opportunity(), decision(), ExecutionContext(USER, ACCOUNT, NOW, close))
    assert result.status is ExecutionResultStatus.BREAKER_TRIPPED
    assert repo.calls == ["claim", "breaker", "failed"]
    assert close.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", [HealthSignal.CHALLENGE, HealthSignal.RATE_LIMIT, HealthSignal.LOGIN_REQUIRED])
async def test_preexisting_restriction_blocks_action_before_click(signal):
    repo, adapter, close = Repo(), Adapter(signal=signal), Close()
    result = await Executor(repo, adapter).execute(
        opportunity(), decision(), ExecutionContext(USER, ACCOUNT, NOW, close)
    )
    assert result.status is ExecutionResultStatus.BREAKER_TRIPPED
    assert [call[0] for call in adapter.calls] == ["health"]
    assert repo.calls == ["claim", "breaker", "failed"]
    assert close.calls == 1


@pytest.mark.asyncio
async def test_new_restriction_after_action_still_pauses_account():
    repo, adapter, close = Repo(), Adapter(), Close()
    signals = iter((HealthSignal.NONE, HealthSignal.CHALLENGE))

    async def health():
        adapter.calls.append(("health", ()))
        return next(signals)

    adapter.detect_health_signal = health
    result = await Executor(repo, adapter).execute(
        opportunity(), decision(), ExecutionContext(USER, ACCOUNT, NOW, close)
    )
    assert result.status is ExecutionResultStatus.SENT
    assert [call[0] for call in adapter.calls] == ["health", "like", "health"]
    assert repo.calls == ["claim", "sent", "breaker"]
    assert close.calls == 1


@pytest.mark.asyncio
async def test_action_and_close_failures_stay_bounded():
    repo, adapter, close = Repo(), Adapter(ActionOutcome.FAILED), Close(True)
    result = await Executor(repo, adapter).execute(opportunity(), decision(), ExecutionContext(USER, ACCOUNT, NOW, close))
    assert result.status is ExecutionResultStatus.FAILED
    assert result.failure_code == "action_failed"
    assert "secret" not in repr(result).lower()
    assert repo.calls == ["claim", "failed"]


@pytest.mark.parametrize("reason", ["story_unavailable", "story_expired"])
async def test_unavailable_story_skips_without_account_health_failure(reason):
    repo, adapter, close = Repo(), Adapter(ActionOutcome.SKIPPED), Close()
    adapter.skip_reason = reason
    result = await Executor(repo, adapter).execute(
        opportunity(), decision("reply"), ExecutionContext(USER, ACCOUNT, NOW, close)
    )
    assert result.status is ExecutionResultStatus.SKIPPED
    assert result.failure_code == reason
    assert repo.calls == ["claim", ("skipped", reason)]
    assert [call[0] for call in adapter.calls] == ["health", "reply"]
    assert close.calls == 0


async def test_unknown_skip_reason_cannot_release_a_claim():
    repo, adapter = Repo(), Adapter(ActionOutcome.SKIPPED)
    adapter.skip_reason = "unknown"
    result = await Executor(repo, adapter).execute(
        opportunity(), decision("reply"), ExecutionContext(USER, ACCOUNT, NOW, Close())
    )
    assert result.status is ExecutionResultStatus.FAILED
    assert repo.calls == ["claim", "failed"]


async def test_failed_skip_persistence_does_not_report_safe_skip():
    repo, adapter = Repo(), Adapter(ActionOutcome.SKIPPED)
    adapter.skip_reason = "story_unavailable"
    async def fail(*args):
        return False
    repo.mark_skipped = fail
    result = await Executor(repo, adapter).execute(
        opportunity(), decision("reply"), ExecutionContext(USER, ACCOUNT, NOW, Close())
    )
    assert result.status is ExecutionResultStatus.FAILED
    assert result.failure_code == "repository_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", [HealthSignal.NETWORK_ERROR, HealthSignal.PROXY_ERROR])
async def test_transport_signal_fails_current_action_without_pausing(signal):
    repo, adapter, close = Repo(), Adapter(signal=signal), Close()
    result = await Executor(repo, adapter).execute(
        opportunity(), decision(), ExecutionContext(USER, ACCOUNT, NOW, close)
    )
    assert result.status is ExecutionResultStatus.FAILED
    assert result.failure_code == signal.value
    assert "breaker" not in repo.calls
    assert repo.calls == ["claim", "failed"]


@pytest.mark.asyncio
async def test_cancellation_finalizes_claim_closes_and_reraises():
    repo, adapter, close = Repo(), Adapter(), Close()

    async def cancel(*args):
        raise asyncio.CancelledError

    adapter.like_story = cancel
    with pytest.raises(asyncio.CancelledError):
        await Executor(repo, adapter).execute(opportunity(), decision(), ExecutionContext(USER, ACCOUNT, NOW, close))
    assert repo.calls == ["claim", "failed"]
    assert close.calls == 1


@pytest.mark.asyncio
async def test_rejects_wrong_target_and_inconsistent_values_before_claim():
    repo = Repo()
    bad = opportunity()
    object.__setattr__(bad, "target_handle", " wrong ")
    with pytest.raises(ValueError, match="execution input"):
        await Executor(repo, Adapter()).execute(bad, decision(), ExecutionContext(USER, ACCOUNT, NOW, Close()))
    assert repo.calls == []


@pytest.mark.asyncio
async def test_rejects_mutated_fingerprint_before_claim():
    repo = Repo()
    bad = opportunity()
    object.__setattr__(bad, "fingerprint", "not-a-fingerprint")
    with pytest.raises(ValueError, match="execution input"):
        await Executor(repo, Adapter()).execute(
            bad, decision(), ExecutionContext(USER, ACCOUNT, NOW, Close())
        )
    assert repo.calls == []


@pytest.mark.parametrize('pause_fails', [False, True])
async def test_confirmed_write_precedes_post_action_health_and_survives_pause_error(pause_fails):
    repo, adapter, close = Repo(), Adapter(), Close()
    async def action(*args):
        adapter.signal = HealthSignal.LOGIN_REQUIRED
        return ActionOutcome.CONFIRMED
    adapter.reply_to_story = action
    if pause_fails:
        async def broken_pause(*args):
            repo.calls.append('breaker')
            raise RuntimeError('private database detail')
        repo.persist_breaker = broken_pause
    result = await Executor(repo, adapter).execute(
        opportunity(), decision('reply'), ExecutionContext(USER, ACCOUNT, NOW, close))
    assert result.status is ExecutionResultStatus.SENT
    assert repo.calls == ['claim', 'sent', 'breaker']
    assert result.action_evidence == ('post_receipt_health_failed' if pause_fails else None)


@pytest.mark.parametrize("phase", ["claim", "preflight", "action"])
@pytest.mark.parametrize("carrier", ["malformed", ENGAGEMENT, TARGET])
async def test_executor_rejects_incoming_cancellation_receipts(phase, carrier):
    from engagemate.worker.executor import ExecutionResult
    if isinstance(carrier, UUID):
        carrier = ExecutionResult(ExecutionResultStatus.SENT, carrier)
    repo, adapter = Repo(), Adapter()
    error = asyncio.CancelledError()
    error._engagemate_story_result = carrier
    async def cancel(*args):
        raise error
    if phase == "claim":
        repo.claim = cancel
    elif phase == "preflight":
        adapter.detect_health_signal = cancel
    else:
        adapter.like_story = cancel
    with pytest.raises(asyncio.CancelledError) as caught:
        await Executor(repo, adapter).execute(
            opportunity(), decision(), ExecutionContext(USER, ACCOUNT, NOW, Close()))
    assert caught.value is error
    assert not hasattr(error, "_engagemate_story_result")
    assert "sent" not in repo.calls


@pytest.mark.parametrize("committed", [True, False])
async def test_cancel_during_commit_observes_one_retained_sent_task(committed):
    repo, adapter = Repo(), Adapter()
    started, release = asyncio.Event(), asyncio.Event()
    async def mark_sent(*args):
        repo.calls.append("sent_attempt")
        started.set()
        await release.wait()
        return committed
    repo.mark_sent = mark_sent
    task = asyncio.create_task(Executor(repo, adapter).execute(
        opportunity(), decision(), ExecutionContext(USER, ACCOUNT, NOW, Close())))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, 1)
    assert repo.calls == ["claim", "sent_attempt"]
    carrier = getattr(caught.value, "_engagemate_story_result", None)
    if committed:
        assert carrier.status is ExecutionResultStatus.SENT and carrier.engagement_id == ENGAGEMENT
    else:
        assert carrier is None
