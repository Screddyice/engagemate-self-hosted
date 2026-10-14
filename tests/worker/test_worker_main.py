"""Task 12 — the worker loop that drives run_once."""

from uuid import UUID
import asyncio

import pytest
from engagemate.domain.models import JobStatus
from engagemate.worker import main
from engagemate.worker.main import run_loop, worker_id
from engagemate.worker.runner import RunSummary

JOB = UUID("00000000-0000-0000-0000-000000000005")
USER = UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT = UUID("00000000-0000-0000-0000-000000000002")


def summary() -> RunSummary:
    return RunSummary(
        job_id=JOB,
        user_id=USER,
        ig_account_id=ACCOUNT,
        status=JobStatus.COMPLETED,
        considered=1,
        engaged=1,
        skipped=0,
        gated=0,
        failed=0,
        stop_reason=None,
    )


class FakeRunner:
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def run_once(self, worker_id):
        self.calls += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def stop_after(count):
    state = {"remaining": count}

    def should_continue():
        if state["remaining"] <= 0:
            return False
        state["remaining"] -= 1
        return True

    return should_continue


async def test_loop_runs_until_it_is_told_to_stop():
    runner = FakeRunner([summary(), summary(), summary()])
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    await run_loop(
        runner, "worker-1", idle_seconds=5.0, sleep=sleep,
        should_continue=stop_after(3),
    )

    assert runner.calls == 3
    assert slept == []


async def test_loop_idles_when_no_job_is_claimable():
    runner = FakeRunner([None, summary()])
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    await run_loop(
        runner, "worker-1", idle_seconds=5.0, sleep=sleep,
        should_continue=stop_after(2),
    )

    assert runner.calls == 2
    assert slept == [5.0]


async def test_loop_survives_a_failing_run():
    runner = FakeRunner([RuntimeError("boom"), summary()])
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    await run_loop(
        runner, "worker-1", idle_seconds=5.0, sleep=sleep,
        should_continue=stop_after(2),
    )

    assert runner.calls == 2
    assert slept == [5.0]


async def test_loop_stops_immediately_when_never_permitted_to_run():
    runner = FakeRunner([])

    await run_loop(
        runner, "worker-1", idle_seconds=5.0, sleep=None,
        should_continue=stop_after(0),
    )

    assert runner.calls == 0


async def test_idle_seconds_must_be_positive():
    with pytest.raises(ValueError):
        await run_loop(
            FakeRunner([]), "worker-1", idle_seconds=0.0,
            should_continue=stop_after(0),
        )


async def test_heartbeat_seconds_must_be_positive():
    with pytest.raises(ValueError):
        await run_loop(
            FakeRunner([]), "worker-1", idle_seconds=5.0,
            heartbeat_seconds=0.0, should_continue=stop_after(0),
        )


def _clock(ticks):
    """Return a monotonic-style clock that walks `ticks` then holds the last."""

    remaining = list(ticks)
    state = {"last": remaining[0] if remaining else 0.0}

    def now():
        if remaining:
            state["last"] = remaining.pop(0)
        return state["last"]

    return now


async def test_idle_worker_logs_a_heartbeat_once_the_interval_passes(caplog):
    runner = FakeRunner([None, None])

    async def sleep(seconds):
        return None

    with caplog.at_level("INFO", logger="engagemate.worker.main"):
        await run_loop(
            runner, "worker-1", idle_seconds=5.0, sleep=sleep,
            heartbeat_seconds=300.0,
            # start, first idle tick (below interval), second idle tick (past it)
            now=_clock([0.0, 10.0, 400.0]),
            should_continue=stop_after(2),
        )

    beats = [r for r in caplog.records if r.message.startswith("worker idle")]
    assert len(beats) == 1
    # Idleness is measured from when the worker went quiet, not from the beat.
    assert "idle_seconds=390" in beats[0].getMessage()


async def test_a_completed_run_resets_the_heartbeat_window(caplog):
    runner = FakeRunner([None, summary(), None])

    async def sleep(seconds):
        return None

    with caplog.at_level("INFO", logger="engagemate.worker.main"):
        await run_loop(
            runner, "worker-1", idle_seconds=5.0, sleep=sleep,
            heartbeat_seconds=300.0,
            # The 400.0 tick lands on the run that succeeded, so the idle tick
            # after it starts a fresh window and must not beat.
            now=_clock([0.0, 10.0, 400.0, 410.0]),
            should_continue=stop_after(3),
        )

    assert not [r for r in caplog.records if r.message.startswith("worker idle")]


async def test_worker_id_must_not_be_empty():
    with pytest.raises(ValueError):
        await run_loop(
            FakeRunner([]), "", idle_seconds=5.0, should_continue=stop_after(0)
        )


def test_configured_worker_id_is_used(monkeypatch):
    monkeypatch.setenv("WORKER_ID", "story-worker-3")

    assert worker_id() == "story-worker-3"


def test_generated_worker_ids_are_unique(monkeypatch):
    monkeypatch.delenv("WORKER_ID", raising=False)

    first, second = worker_id(), worker_id()

    assert first != second
    assert 0 < len(first) <= 128


def test_blank_configured_worker_id_falls_back_to_a_generated_one(monkeypatch):
    monkeypatch.setenv("WORKER_ID", "   ")

    assert worker_id().strip() != ""


def test_idle_seconds_defaults_and_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WORKER_IDLE_SECONDS", raising=False)
    assert main.idle_seconds() == main._DEFAULT_IDLE_SECONDS

    monkeypatch.setenv("WORKER_IDLE_SECONDS", "900")
    assert main.idle_seconds() == 900.0


@pytest.mark.parametrize("raw", ["0", "-1", "", " ", "soon"])
def test_idle_seconds_rejects_values_that_would_busy_loop_or_confuse(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    # An empty or blank value falls back rather than raising; anything that
    # parses to a non-positive number would spin the worker against the
    # database, which is the exact cost this setting exists to control.
    monkeypatch.setenv("WORKER_IDLE_SECONDS", raw)
    if raw.strip() == "":
        assert main.idle_seconds() == main._DEFAULT_IDLE_SECONDS
    else:
        with pytest.raises(ValueError, match="WORKER_IDLE_SECONDS"):
            main.idle_seconds()


async def test_service_supervisor_cancels_the_peer_when_one_exits() -> None:
    peer_cancelled = asyncio.Event()

    async def exits():
        return None

    async def peer():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            peer_cancelled.set()
            raise

    with pytest.raises(RuntimeError, match="exited unexpectedly"):
        await main.run_worker_services(exits(), peer(), stopping=asyncio.Event())

    assert peer_cancelled.is_set()


async def test_service_supervisor_stops_both_services_on_shutdown() -> None:
    stopping = asyncio.Event()
    cancelled = 0

    async def service():
        nonlocal cancelled
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled += 1
            raise

    async def request_stop():
        await asyncio.sleep(0)
        stopping.set()

    trigger = asyncio.create_task(request_stop())
    await main.run_worker_services(service(), service(), stopping=stopping)
    await trigger

    assert cancelled == 2


def test_execution_key_is_required_at_worker_startup() -> None:
    with pytest.raises(RuntimeError, match="execution MCP key"):
        main.require_execution_key(None)
    assert main.require_execution_key(type("Secret", (), {"get_secret_value": lambda _: "k" * 48})()) == "k" * 48


@pytest.mark.parametrize(
    "entrypoint,execution_seconds,canary_seconds,expected",
    [("worker", 1800, 300, 1800), ("worker", 3600, 300, 3600),
     ("canary", 1800, 300, 300), ("canary", 60, 300, 60)],
)
async def test_entrypoints_use_execution_timeout_and_canary_ceiling(
    monkeypatch, entrypoint, execution_seconds, canary_seconds, expected
):
    from contextlib import asynccontextmanager, nullcontext
    from types import SimpleNamespace

    import httpx
    import playwright.async_api
    from pydantic import SecretStr
    from deploy import healthcheck
    from engagemate import config
    from engagemate.db import pool as pool_module
    from engagemate.worker import canary

    settings = SimpleNamespace(
        execution_mcp_host="127.0.0.1", execution_mcp_port=8766,
        execution_mcp_key=SecretStr("T9x_2AbCdEfGhJkLmNpQrStUvWxYz0123456789-"), database_url="test-pool",
        job_lease_seconds=120, hermes_agent_timeout_seconds=2,
        hermes_execution_timeout_seconds=execution_seconds,
    )
    closed = []
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr(healthcheck, "validate_worker_environment", lambda _: None)
    monkeypatch.setattr(pool_module, "create_pool", lambda *_, **__: SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_: None)
    monkeypatch.setattr(canary, "_bind_mcp_socket", lambda *_: nullcontext(object()))

    @asynccontextmanager
    async def browser_context():
        yield object()

    monkeypatch.setattr(playwright.async_api, "async_playwright", browser_context)

    class StopBeforeHTTP(Exception):
        pass

    def http_client(**kwargs):
        assert kwargs["timeout"].read == expected
        if entrypoint == "canary":
            assert kwargs["timeout"].connect == 15
        raise StopBeforeHTTP

    monkeypatch.setattr(httpx, "AsyncClient", http_client)
    with pytest.raises(StopBeforeHTTP):
        if entrypoint == "worker":
            await main.main()
        else:
            await canary.run_canary(
                canary.CanaryScope(USER, ACCOUNT, JOB, "test-worker"),
                timeout_seconds=canary_seconds,
            )
    assert closed == [True]
