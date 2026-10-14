"""One-shot growth collector command contracts."""

from __future__ import annotations

import base64
import logging
import os
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import date
from types import SimpleNamespace

import pytest

from engagemate.config import Settings
from engagemate.growth import main as growth_main
from engagemate.growth.repository import DueAccount


def _settings(**values: object) -> Settings:
    configured = {
        "DATABASE_URL": "postgresql://localhost/engagemate",
        "SESSION_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32))
        .decode()
        .rstrip("="),
        "INTERNAL_API_KEY": "internal-test-key",
        "_env_file": None,
    }
    configured.update(values)
    return Settings(**configured)


def test_growth_settings_have_bounded_defaults_and_environment_aliases() -> None:
    defaults = _settings()
    configured = _settings(
        GROWTH_SNAPSHOT_BATCH_SIZE="12",
        GROWTH_SNAPSHOT_CONCURRENCY="3",
    )

    assert (defaults.growth_snapshot_batch_size, defaults.growth_snapshot_concurrency) == (
        25,
        4,
    )
    assert (
        configured.growth_snapshot_batch_size,
        configured.growth_snapshot_concurrency,
    ) == (12, 3)


def test_parse_args_accepts_dry_run_and_scoped_user() -> None:
    user_id = uuid.uuid4()

    options = growth_main.parse_args(["--dry-run", "--user-id", str(user_id)])

    assert options.dry_run is True
    assert options.user_id == user_id


def test_parse_args_rejects_invalid_user_id() -> None:
    with pytest.raises(SystemExit):
        growth_main.parse_args(["--user-id", "not-a-uuid"])


def test_main_returns_zero_after_the_batch_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    async def execute(options):
        seen.append(options)

    monkeypatch.setattr(growth_main, "execute", execute)
    user_id = uuid.uuid4()

    exit_code = growth_main.main(["--dry-run", "--user-id", str(user_id)])

    assert exit_code == 0
    assert len(seen) == 1
    assert seen[0].dry_run is True
    assert seen[0].user_id == user_id


def test_main_returns_nonzero_without_logging_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail(_options):
        raise RuntimeError("raw-provider-body-secret")

    monkeypatch.setattr(growth_main, "execute", fail)

    with caplog.at_level(logging.ERROR):
        exit_code = growth_main.main([])

    assert exit_code == 1
    assert "batch_failed" in caplog.text
    assert "raw-provider-body-secret" not in caplog.text


def test_command_boundary_runs_real_collector_with_external_io_substituted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    account = DueAccount(
        user_id,
        uuid.uuid4(),
        "managed.account",
        "UTC",
        date(2026, 8, 14),
    )

    class Pool:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Repository:
        def __init__(self) -> None:
            self.list_calls = []
            self.writes = []

        def list_due(self, now, limit, *, user_id=None):
            self.list_calls.append((now, limit, user_id))
            return [account]

        def upsert(self, *args):
            self.writes.append(args)
            return True

    class HyperCrawl:
        urls = []

        async def profiles(self, urls):
            self.urls.extend(urls)
            return [{"followersCount": 100}]

    class Http(AbstractAsyncContextManager):
        async def __aexit__(self, *_args):
            return None

    pool = Pool()
    repository = Repository()
    provider = HyperCrawl()
    monkeypatch.setattr(
        growth_main,
        "Settings",
        lambda: SimpleNamespace(
            database_url="postgresql://external-io-substituted",
            hypercrawl_url="https://hypercrawl.example.test",
            hypercrawl_rest_token=None,
            hypercrawl_timeout_seconds=10,
            growth_snapshot_batch_size=7,
            growth_snapshot_concurrency=2,
        ),
    )
    monkeypatch.setattr(growth_main, "create_pool", lambda *_args, **_kwargs: pool)
    monkeypatch.setattr(
        growth_main, "GrowthSnapshotRepository", lambda _pool: repository
    )
    monkeypatch.setattr(
        growth_main, "HyperCrawlInstagramClient", lambda **_kwargs: provider
    )
    monkeypatch.setattr(growth_main.httpx, "AsyncClient", Http)

    exit_code = growth_main.main(["--user-id", str(user_id)])

    assert exit_code == 0
    assert repository.list_calls[0][1:] == (7, user_id)
    assert len(repository.writes) == 1
    assert provider.urls == ["https://www.instagram.com/managed.account/"]
    assert pool.closed is True
