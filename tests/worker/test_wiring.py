"""Adapters that join the sync repositories and live browser to the runner."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from engagemate.domain.models import JobStatus
from engagemate.worker.runner import RunSummary
from engagemate.worker.wiring import (
    AsyncAccountPauser,
    AsyncJobRepository,
    AsyncRunSummaryRepository,
    BrowserMediaFetcher,
    DiscardingArtifactSink,
    build_runner,
    resolve_page,
)

from tests.worker import test_canary_scripts as canary_fixtures

database_url = canary_fixtures.database_url
world = canary_fixtures.world

USER = UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT = UUID("00000000-0000-0000-0000-000000000002")
JOB = UUID("00000000-0000-0000-0000-000000000005")


class FakeJobRepository:
    def __init__(self, claimed=None, alive=True, finished=True):
        self.claimed = claimed
        self.alive = alive
        self.finished = finished
        self.calls = []

    def claim_next(self, worker_id):
        self.calls.append(("claim_next", worker_id))
        return self.claimed

    def heartbeat(self, job_id, worker_id):
        self.calls.append(("heartbeat", job_id, worker_id))
        return self.alive

    def finish(self, job_id, worker_id, status, error=None):
        self.calls.append(("finish", job_id, worker_id, status, error))
        return self.finished


class FakeAccountRepository:
    def __init__(self):
        self.calls = []

    def set_paused(self, user_id, account_id, *, paused, reason=None):
        self.calls.append((user_id, account_id, paused, reason))
        return object()


async def test_async_job_repository_forwards_a_claim():
    repository = FakeJobRepository(claimed="job")
    jobs = AsyncJobRepository(repository)

    assert await jobs.claim_next("worker-1") == "job"
    assert repository.calls == [("claim_next", "worker-1")]


async def test_async_job_repository_forwards_a_heartbeat():
    repository = FakeJobRepository(alive=False)
    jobs = AsyncJobRepository(repository)

    assert await jobs.heartbeat(JOB, "worker-1") is False
    assert repository.calls == [("heartbeat", JOB, "worker-1")]


async def test_async_job_repository_forwards_a_finish():
    repository = FakeJobRepository()
    jobs = AsyncJobRepository(repository)

    assert await jobs.finish(JOB, "worker-1", JobStatus.COMPLETED, "done") is True
    assert repository.calls == [
        ("finish", JOB, "worker-1", JobStatus.COMPLETED, "done")
    ]


async def test_account_pauser_pauses_with_a_reason():
    repository = FakeAccountRepository()

    await AsyncAccountPauser(repository).pause(USER, ACCOUNT, "breaker_tripped")

    assert repository.calls == [(USER, ACCOUNT, True, "breaker_tripped")]


class SummaryConnection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), dict(params)))


class SummaryPool:
    def __init__(self):
        self.value = SummaryConnection()

    def connection(self):
        connection = self.value

        class Context:
            def __enter__(self):
                return connection

            def __exit__(self, exc_type, exc, traceback):
                return False

        return Context()


async def test_run_summary_repository_persists_only_bounded_aggregates():
    pool = SummaryPool()
    summary = RunSummary(
        job_id=JOB,
        user_id=USER,
        ig_account_id=ACCOUNT,
        status=JobStatus.COMPLETED,
        considered=2,
        engaged=1,
        skipped=0,
        gated=1,
        failed=1,
        stop_reason=None,
        input_tokens=360,
        output_tokens=24,
        images_analyzed=2,
        video_frames_analyzed=0,
        retries=1,
        runtime_ms=9_000,
    )
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)

    await AsyncRunSummaryRepository(pool).persist(summary, now, now)

    sql, params = pool.value.calls[0]
    assert "ON CONFLICT (job_id) DO NOTHING" in sql
    assert "opportunities_found, skipped, gated, failed" in sql
    assert params["skipped"] == 0
    assert params["gated"] == 1
    assert params["input_tokens"] == 360
    assert params["images_analyzed"] == 2
    assert "secret" not in repr(params).lower()


def _summary_for(world, **overrides):
    return RunSummary(
        **{
            "job_id": world["job_id"],
            "user_id": world["user_id"],
            "ig_account_id": world["account_id"],
            "status": JobStatus.COMPLETED,
            "considered": 1,
            "engaged": 999,  # Never use this undifferentiated total for action counts.
            "skipped": 0,
            "gated": 0,
            "failed": 0,
            "stop_reason": None,
            **overrides,
        }
    )


def _persisted_action(world, action="like", status="sent", *, foreign=False):
    prefix = "other_" if foreign else ""
    engagement = uuid4()
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE jobs SET status='completed' WHERE id=%s", (world["job_id"],)
        )
        connection.execute(
            "INSERT INTO story_engagements "
            "(id,user_id,ig_account_id,target_id,story_fingerprint,action,status,executed_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,now())",
            (
                engagement,
                world[prefix + "user_id"],
                world[prefix + "account_id"],
                world[prefix + "target_id"],
                "a" * 64,
                action,
                status,
            ),
        )
        # Duplicate durable calls must not double-count one sent engagement.
        for _ in range(2 if status == "sent" else 1):
            connection.execute(
                "INSERT INTO hermes_tool_calls "
                "(run_id,tool_name,target_id,story_fingerprint,idempotency_key,status) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    world["run_id"],
                    action,
                    world[prefix + "target_id"],
                    "a" * 64,
                    str(uuid4()),
                    "confirmed" if status == "sent" else "uncertain",
                ),
            )
    return engagement


@pytest.mark.parametrize("action", ["like", "reply", "dm", "view"])
@pytest.mark.parametrize("status", ["sent", "failed"])
async def test_summary_counts_persisted_actions_and_failures(world, action, status):
    _persisted_action(world, action, status)
    # Match the database clock used by the persisted action timestamps.
    with world["pool"].connection() as connection:
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    repository = AsyncRunSummaryRepository(world["pool"])
    await repository.persist(_summary_for(world), now - timedelta(minutes=1), now)
    # The final snapshot is idempotent, including if a caller later inflates totals.
    await repository.persist(
        _summary_for(world, failed=999), now - timedelta(minutes=1), now
    )
    with world["pool"].connection() as connection:
        row = connection.execute(
            "SELECT likes_sent,replies_sent,dms_sent,failed FROM run_summaries "
            "WHERE job_id=%s",
            (world["job_id"],),
        ).fetchone()
    assert dict(row) == {
        "likes_sent": int(action == "like" and status == "sent"),
        "replies_sent": int(action == "reply" and status == "sent"),
        "dms_sent": int(action == "dm" and status == "sent"),
        "failed": int(status == "failed"),
    }


@pytest.mark.parametrize(
    "contamination", ["foreign", "previous_run", "unconfirmed", "wrong_job"]
)
async def test_summary_excludes_actions_outside_its_authoritative_scope(
    world, contamination
):
    engagement = _persisted_action(world, foreign=contamination == "foreign")
    with world["pool"].connection() as connection:
        if contamination == "previous_run":
            connection.execute(
                "UPDATE story_engagements SET created_at=now()-interval '1 day', "
                "executed_at=now()-interval '1 day' WHERE id=%s",
                (engagement,),
            )
        elif contamination == "unconfirmed":
            connection.execute(
                "UPDATE hermes_tool_calls SET status='denied' WHERE run_id=%s",
                (world["run_id"],),
            )
        elif contamination == "wrong_job":
            # A separate job on the same account cannot inherit the first job's Like.
            world["job_id"] = uuid4()
            connection.execute(
                "INSERT INTO jobs (id,user_id,ig_account_id,schedule_key,scheduled_for,status) "
                "VALUES (%s,%s,%s,%s,now(),'completed')",
                (world["job_id"], world["user_id"], world["account_id"], str(uuid4())),
            )
    # Match the database clock used by the persisted action timestamps.
    with world["pool"].connection() as connection:
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    await AsyncRunSummaryRepository(world["pool"]).persist(
        _summary_for(world), now, now
    )
    with world["pool"].connection() as connection:
        row = connection.execute(
            "SELECT likes_sent FROM run_summaries WHERE job_id=%s", (world["job_id"],)
        ).fetchone()
    assert row["likes_sent"] == 0


@pytest.mark.parametrize("invalid_scope", ["foreign_tenant", "active_job"])
async def test_summary_requires_its_own_terminal_job(world, invalid_scope):
    summary = _summary_for(world)
    if invalid_scope == "foreign_tenant":
        summary = _summary_for(
            world,
            user_id=world["other_user_id"],
            ig_account_id=world["other_account_id"],
        )
        with world["pool"].connection() as connection:
            connection.execute(
                "UPDATE jobs SET status='completed' WHERE id=%s", (world["job_id"],)
            )
    # Match the database clock used by the persisted action timestamps.
    with world["pool"].connection() as connection:
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    await AsyncRunSummaryRepository(world["pool"]).persist(summary, now, now)
    with world["pool"].connection() as connection:
        assert (
            connection.execute("SELECT count(*) AS n FROM run_summaries").fetchone()[
                "n"
            ]
            == 0
        )


class FakeResponse:
    def __init__(self, ok=True, body=b"bytes", headers=None, status=200):
        self._ok = ok
        self._body = body
        self._headers = (
            headers if headers is not None else {"content-type": "image/jpeg"}
        )
        self.status = status

    @property
    def ok(self):
        return self._ok

    async def body(self):
        return self._body

    @property
    def headers(self):
        return self._headers


class FakeRequest:
    def __init__(self, response=None, error=None):
        self._response = response if response is not None else FakeResponse()
        self._error = error
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        if self._error is not None:
            raise self._error
        return self._response


class FakeContext:
    def __init__(self, request=None, pages=None):
        self.request = request if request is not None else FakeRequest()
        self.pages = pages if pages is not None else ["page-0"]
        self.created = 0

    async def new_page(self):
        self.created += 1
        return f"page-{len(self.pages)}"


class FakeSession:
    def __init__(self, context=None):
        self.context = context if context is not None else FakeContext()


async def test_media_fetcher_returns_mime_and_bytes():
    context = FakeContext()
    fetcher = BrowserMediaFetcher(context)

    mime, data = await fetcher.fetch("https://cdn.example.com/a.jpg")

    assert mime == "image/jpeg"
    assert data == b"bytes"
    assert context.request.calls == ["https://cdn.example.com/a.jpg"]


async def test_media_fetcher_strips_content_type_parameters():
    context = FakeContext(
        FakeRequest(
            FakeResponse(headers={"content-type": "image/jpeg; charset=binary"})
        )
    )

    mime, _ = await BrowserMediaFetcher(context).fetch("https://cdn.example.com/a.jpg")

    assert mime == "image/jpeg"


async def test_media_fetcher_raises_on_a_failed_response():
    context = FakeContext(FakeRequest(FakeResponse(ok=False, status=404)))

    with pytest.raises(RuntimeError):
        await BrowserMediaFetcher(context).fetch("https://cdn.example.com/a.jpg")


async def test_media_fetcher_raises_when_the_content_type_is_missing():
    context = FakeContext(FakeRequest(FakeResponse(headers={})))

    with pytest.raises(RuntimeError):
        await BrowserMediaFetcher(context).fetch("https://cdn.example.com/a.jpg")


async def test_media_fetcher_refuses_a_non_https_url():
    context = FakeContext()

    with pytest.raises(ValueError):
        await BrowserMediaFetcher(context).fetch("http://cdn.example.com/a.jpg")

    assert context.request.calls == []


async def test_resolve_page_reuses_the_contexts_open_page():
    session = FakeSession()

    assert await resolve_page(session) == "page-0"
    assert session.context.created == 0


async def test_resolve_page_opens_one_when_the_context_has_none():
    session = FakeSession(FakeContext(pages=[]))

    assert await resolve_page(session) == "page-0"
    assert session.context.created == 1


async def test_discarding_artifact_sink_accepts_and_drops_artifacts():
    sink = DiscardingArtifactSink()

    assert await sink.write(object()) is None


def test_production_wiring_requires_one_shared_hermes_registry_and_repository() -> None:
    parameters = inspect.signature(build_runner).parameters

    assert parameters["hermes"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["registry"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["tool_repository"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["execution_mode"].kind is inspect.Parameter.KEYWORD_ONLY
    source = inspect.getsource(build_runner)
    assert "IntelligenceClient" not in source
    assert "intelligence=" not in source


async def test_follower_preflight_reuses_only_fresh_matching_profile_evidence():
    import time
    from types import SimpleNamespace

    from engagemate.worker.circuit_breaker import HealthSignal
    from engagemate.worker.wiring import _SessionBoundDiscovery

    events = []

    class Browser:
        page = SimpleNamespace(url="https://www.instagram.com/prospect/")

        async def _check_page(self):
            events.append("check_existing_page")

        async def inspect(self, handle):
            pytest.fail("a completed follower recheck must not be repeated")

    discovery = _SessionBoundDiscovery(FakeSession(), None)
    discovery._follower_service = object()
    discovery._follower_browser = Browser()
    opportunity = SimpleNamespace(target_handle="prospect", owner_id="123")
    discovery._verified_profile = ("prospect", "123", time.monotonic())
    assert await discovery.preflight_health(opportunity) is HealthSignal.NONE
    assert events == ["check_existing_page"]
    discovery._verified_profile = ("prospect", "999", time.monotonic())
    assert (
        await discovery.preflight_health(opportunity) is HealthSignal.HEALTH_UNAVAILABLE
    )
    discovery._verified_profile = ("prospect", "123", time.monotonic() - 31)
    assert (
        await discovery.preflight_health(opportunity) is HealthSignal.HEALTH_UNAVAILABLE
    )
    discovery._verified_profile = ("prospect", "123", time.monotonic())
    discovery._follower_browser.page.url = "https://example.com/prospect/"
    assert (
        await discovery.preflight_health(opportunity) is HealthSignal.HEALTH_UNAVAILABLE
    )


async def test_follower_discovery_delegates_to_guarded_media_discovery():
    from engagemate.worker.wiring import _FollowerStoryDiscovery

    calls = []

    class Discovery:
        async def find(self, *args):
            calls.append(args)
            return ("measured-opportunity",)

    result = await _FollowerStoryDiscovery(Discovery()).find(
        "context", "targets", "budget"
    )
    assert result == ("measured-opportunity",)
    assert calls == [("context", "targets", "budget")]


@pytest.mark.parametrize("allow_replies", [False, True])
async def test_bound_executor_keeps_navigation_after_claim_and_deduplicates(
    monkeypatch, allow_replies,
):
    from datetime import UTC
    from types import SimpleNamespace

    from engagemate.intelligence.schemas import EngagementDecision
    from engagemate.worker import wiring
    from engagemate.worker.circuit_breaker import HealthSignal
    from engagemate.worker.executor import ExecutionContext, ExecutionResultStatus
    from engagemate.worker.instagram_adapter import ActionOutcome
    from engagemate.worker.story_fingerprint import fingerprint_story
    from engagemate.worker.story_types import (
        StoryContent,
        StoryMediaType,
        StoryOpportunity,
    )

    now = datetime.now(UTC)
    events = []

    class Repository:
        duplicate = False

        async def claim(self, metadata):
            events.append("claim")
            return SimpleNamespace(duplicate=self.duplicate, engagement_id=JOB)

        async def mark_sent(self, *args):
            events.append("sent")
            return True

    class Discovery:
        async def preflight_health(self, opportunity):
            events.append("fresh_profile_health")
            return HealthSignal.NONE

    class Adapter:
        action_evidence = None

        def __init__(self, page, sink, opportunity, *, preflight_health, allow_reply_measurement):
            assert allow_reply_measurement is allow_replies
            self.health = preflight_health
            events.append("bind_exact_opportunity")

        async def detect_health_signal(self):
            return await self.health()

        async def like_story(self, handle, fingerprint):
            events.append("navigate_and_like")
            return ActionOutcome.CONFIRMED

    monkeypatch.setattr(wiring, "LiveStoryAdapter", Adapter)
    repository = Repository()
    wrapper = wiring._SessionBoundExecutor(
        FakeSession(), repository, DiscardingArtifactSink(), Discovery(), allow_replies=allow_replies
    )
    opportunity = StoryOpportunity(
        JOB,
        "123",
        "prospect",
        "vip",
        "456",
        now,
        fingerprint_story("123", "456", now, None),
        StoryContent(StoryMediaType.IMAGE, None, None, None, ()),
    )
    decision = EngagementDecision(
        engage=True,
        action="like",
        generated_text=None,
        confidence=0.9,
        rationale="relevant",
        model_used="claude-sonnet-4-6",
    )

    async def close():
        pass

    context = ExecutionContext(USER, ACCOUNT, now, SimpleNamespace(close=close))
    result = await wrapper.execute(opportunity, decision, context)
    assert result.status is ExecutionResultStatus.SENT
    assert events == [
        "bind_exact_opportunity",
        "claim",
        "fresh_profile_health",
        "navigate_and_like",
        "sent",
        "fresh_profile_health",
    ]
    repository.duplicate = True
    events.clear()
    result = await wrapper.execute(opportunity, decision, context)
    assert result.status is ExecutionResultStatus.SKIPPED
    assert events == ["bind_exact_opportunity", "claim"]


async def test_bound_executor_runs_follow_before_a_story_reply(monkeypatch):
    from datetime import UTC
    from types import SimpleNamespace

    from engagemate.intelligence.schemas import EngagementDecision
    from engagemate.worker import wiring
    from engagemate.worker.executor import (
        ExecutionContext,
        ExecutionResult,
        ExecutionResultStatus,
    )
    from engagemate.worker.story_fingerprint import fingerprint_story
    from engagemate.worker.story_types import (
        StoryContent,
        StoryMediaType,
        StoryOpportunity,
    )

    now = datetime.now(UTC)
    events = []

    class Adapter:
        action_evidence = "reply-ready"

        def __init__(self, *args, **kwargs):
            assert callable(kwargs["reply_authorized"])
            events.append("bind_exact_opportunity")

    class FakeExecutor:
        def __init__(self, *args):
            pass

        async def execute(self, opportunity, decision, context, *, before_action=None):
            assert before_action is not None
            events.append("claim")
            assert await before_action(JOB, context)
            events.append("send")
            return ExecutionResult(ExecutionResultStatus.SENT, JOB)

    monkeypatch.setattr(wiring, "LiveStoryAdapter", Adapter)
    monkeypatch.setattr(wiring, "Executor", FakeExecutor)
    wrapper = wiring._SessionBoundExecutor(
        FakeSession(), object(), DiscardingArtifactSink(), SimpleNamespace(),
        pool=object(), job=SimpleNamespace(id=JOB, claimed_by="worker"),
        allow_replies=True,
    )

    async def follow(*args):
        events.append("follow")
        return object()

    monkeypatch.setattr(wrapper, "_prepare_reply_follow_claim", follow)
    opportunity = StoryOpportunity(
        JOB, "123", "prospect", "vip", "456", now,
        fingerprint_story("123", "456", now, None),
        StoryContent(StoryMediaType.IMAGE, None, None, None, ()),
    )
    decision = EngagementDecision(
        engage=True, action="reply", generated_text="hello", confidence=0.9,
        rationale="relevant", model_used="claude-sonnet-4-6",
    )

    async def close():
        pass

    result = await wrapper.execute(
        opportunity, decision, ExecutionContext(USER, ACCOUNT, now, SimpleNamespace(close=close))
    )
    assert result.status is ExecutionResultStatus.SENT
    assert events == ["bind_exact_opportunity", "claim", "follow", "send"]


@pytest.mark.parametrize("source, suffix", [
    (None, ""), ("untrusted source body", ""),
    ("post_receipt_health_failed", ",post_receipt_health=failed"),
])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_session_executor_returns_receipt_evidence(monkeypatch, source, suffix, cancelled):
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace

    from engagemate.worker import wiring
    from engagemate.worker.executor import ExecutionResult, ExecutionResultStatus

    evidence = "action=reply,stage=receipt,request=forwarded,receipt=rejected,http=200,health=none"
    result = ExecutionResult(ExecutionResultStatus.SENT, uuid4(), action_evidence=source)

    async def page(_session):
        return object()

    class Adapter:
        action_evidence = None

        def __init__(self, *args, **kwargs):
            self.action_evidence = evidence

    class FakeExecutor:
        def __init__(self, *args):
            pass
        async def execute(self, *args):
            if cancelled:
                error = asyncio.CancelledError()
                error._engagemate_story_result = result
                raise error
            return result

    monkeypatch.setattr(wiring, "resolve_page", page)
    monkeypatch.setattr(wiring, "LiveStoryAdapter", Adapter)
    monkeypatch.setattr(wiring, "Executor", FakeExecutor)
    executor = wiring._SessionBoundExecutor(object(), object(), object(), SimpleNamespace())
    if cancelled:
        with pytest.raises(asyncio.CancelledError) as error:
            await executor.execute(object(), object(), object())
        observed = error.value._engagemate_story_result
    else:
        observed = await executor.execute(object(), object(), object())
    assert observed == replace(result, action_evidence=evidence + suffix)


@pytest.mark.parametrize("through_mcp", [False, True])
async def test_session_preflight_cancellation_cannot_forge_confirmation(monkeypatch, through_mcp):
    import asyncio

    from engagemate.worker import wiring
    from engagemate.worker.execution_mcp_tools import _mutate
    from engagemate.worker.executor import ExecutionResult, ExecutionResultStatus

    from tests.worker.test_execution_mcp_tools import (
        ActiveRunRegistry,
        _active_run,
        _opportunity,
    )

    error = asyncio.CancelledError()
    error._engagemate_story_result = ExecutionResult(ExecutionResultStatus.SENT, uuid4())
    async def broken_page(_session):
        raise error
    monkeypatch.setattr(wiring, "resolve_page", broken_page)
    run, registry, opaque = _active_run(), ActiveRunRegistry(), uuid4()
    run.executor = wiring._SessionBoundExecutor(None, None, None, None)
    run.opportunities[opaque] = _opportunity()
    with registry.activate(run), pytest.raises(asyncio.CancelledError) as caught:
        if through_mcp:
            await _mutate(registry, str(opaque), "preflight_cancel_fixture", "like", None)
        else:
            await run.executor.execute(None, None, None)
    assert caught.value is error and not hasattr(error, "_engagemate_story_result")
    assert [row["status"] for row in run.tool_repository.finished] == (["uncertain"] if through_mcp else [])
