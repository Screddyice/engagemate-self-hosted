"""Reject a release when the account quota schema is absent."""
from contextlib import contextmanager
import pytest
from deploy import healthcheck
from tests.worker.test_canary_scripts import database_url, world


def test_message_quota_preflight_requires_migration_0032(world, database_url, monkeypatch):
    with world['pool'].connection() as connection, connection.transaction(force_rollback=True):
        @contextmanager
        def connect(*args, **kwargs):
            yield connection
        monkeypatch.setattr(healthcheck, '_connect_postgres', connect)
        healthcheck.probe_follow_reel_messages({'DATABASE_URL': database_url})
        connection.execute('ALTER TABLE agent_configs DROP COLUMN warmup_exempt')
        with pytest.raises(ValueError, match='migration 0032 is incomplete'):
            healthcheck.probe_follow_reel_messages({'DATABASE_URL': database_url})
