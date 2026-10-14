"""Connection-pool construction contracts."""

from typing import Any

from engagemate.db import pool as pool_module


def test_create_pool_checks_connection_before_checkout(monkeypatch: Any) -> None:
    observed: dict[str, Any] = {}

    class FakePool:
        check_connection = staticmethod(lambda _connection: None)

        def __init__(self, **kwargs: Any) -> None:
            observed.update(kwargs)

        def wait(self, *, timeout: float) -> None:
            observed["wait_timeout"] = timeout

    monkeypatch.setattr(pool_module, "ConnectionPool", FakePool)

    pool = pool_module.create_pool(
        "postgresql://example",
        min_size=2,
        max_size=5,
        timeout=7,
    )

    assert isinstance(pool, FakePool)
    assert observed["check"] is FakePool.check_connection
    assert observed["wait_timeout"] == 7
