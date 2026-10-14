"""Worker requests drain before the browser and binding close."""
import asyncio
import pytest
from engagemate.worker.active_run import NoActiveRun
from tests.worker.test_runner import WORKER, build_runner


async def test_runner_drains_request_before_browser_and_registry_close():
    started,closed = asyncio.Event(),asyncio.Event()
    requests = []

    class Hermes:
        def new_session_id(self):
            return "em_0123456789abcdefghijklmnopqrstuv"

        async def run(self,active,**kwargs):
            async def request():
                try:
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    assert parts.registry.require() is active
                    assert not parts.browser.closed
                    closed.set()
            task = asyncio.create_task(request())
            requests.append(task)
            active.tool_tasks.add(task)
            await started.wait()
            raise RuntimeError("provider failed")

    runner,parts = build_runner(hermes=Hermes())
    summary = await runner.run_once(WORKER)
    assert summary.stop_reason == "hermes_failed"
    assert closed.is_set() and requests[0].done() and parts.browser.closed
    with pytest.raises(NoActiveRun):
        parts.registry.require()
