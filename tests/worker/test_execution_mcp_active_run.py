from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from engagemate.worker.active_run import (
    ActiveRun,
    ActiveRunConflict,
    ActiveRunRegistry,
    NoActiveRun,
)


def _run() -> ActiveRun:
    async def lease_is_live() -> bool:
        return True

    async def pace(action, opportunity) -> None:
        return None

    return ActiveRun(
        run_id=uuid4(),
        job=SimpleNamespace(),
        context=SimpleNamespace(),
        config_version=1,
        config_snapshot={},
        discovery_context=SimpleNamespace(),
        execution_context=SimpleNamespace(),
        budget=SimpleNamespace(),
        text_gate=SimpleNamespace(),
        lease_is_live=lease_is_live,
        discovery=SimpleNamespace(),
        executor=SimpleNamespace(),
        pace=pace,
        opportunities={},
    )


def test_registry_refuses_two_active_runs_and_retains_the_first() -> None:
    registry = ActiveRunRegistry()
    run_a = _run()
    run_b = _run()

    with registry.activate(run_a):
        assert registry.require() is run_a
        with pytest.raises(ActiveRunConflict, match="already owns"):
            with registry.activate(run_b):
                pass
        assert registry.require() is run_a

    with pytest.raises(NoActiveRun, match="no scheduled"):
        registry.require()


def test_registry_clears_the_active_run_after_an_exception() -> None:
    registry = ActiveRunRegistry()

    with pytest.raises(RuntimeError, match="fixture failure"):
        with registry.activate(_run()):
            raise RuntimeError("fixture failure")

    with pytest.raises(NoActiveRun):
        registry.require()
