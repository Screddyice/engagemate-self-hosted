"""The deployment gate validates migrations 0020/0033 against real PostgreSQL."""

from contextlib import contextmanager

import psycopg
import pytest
from psycopg.rows import dict_row

from deploy import healthcheck
from tests.worker import test_canary_scripts as canary_fixtures

database_url = canary_fixtures.database_url


def test_follow_lifecycle_accepts_migrations_with_empty_search_path(database_url):
    healthcheck.probe_follow_lifecycle(
        {"DATABASE_URL": database_url + "?options=-csearch_path%3D"}
    )


@pytest.mark.parametrize(
    "mutation,expected",
    [
        # Later migrations reference this ledger. Remove those dependent constraints
        # too so the fixture still reaches the intended missing-table preflight.
        ("DROP TABLE relationship_follows CASCADE", "relationship_follows.id"),
        (
            "ALTER TABLE agent_configs DROP COLUMN auto_unfollow_enabled",
            "agent_configs.auto_unfollow_enabled",
        ),
        (
            "ALTER TABLE agent_configs DROP COLUMN follow_enabled_at",
            "agent_configs.follow_enabled_at",
        ),
        (
            "ALTER TABLE agent_configs ALTER COLUMN follow_after_engagement DROP NOT NULL",
            "agent_configs.follow_after_engagement",
        ),
        (
            "ALTER TABLE agent_configs ALTER COLUMN daily_follow_limit SET DEFAULT 20",
            "agent_configs.daily_follow_limit default",
        ),
        (
            "ALTER TABLE relationship_follows DROP COLUMN next_attempt_at",
            "relationship_follows.next_attempt_at",
        ),
        (
            "ALTER TABLE relationship_follows DROP COLUMN follow_reserved_at",
            "relationship_follows.follow_reserved_at",
        ),
        (
            "ALTER TABLE relationship_follows ALTER COLUMN evidence DROP DEFAULT",
            "relationship_follows.evidence default",
        ),
        (
            "ALTER TABLE relationship_follows DROP CONSTRAINT relationship_follows_account_owner_unique",
            "relationship_follows_account_owner_unique",
        ),
        (
            "ALTER TABLE relationship_follows DROP CONSTRAINT relationship_follows_target_scope_fk",
            "relationship_follows_target_scope_fk",
        ),
        (
            "ALTER TABLE relationship_follows DROP CONSTRAINT relationship_follows_engagement_scope_fk",
            "relationship_follows_engagement_scope_fk",
        ),
        (
            "ALTER TABLE relationship_follows DROP CONSTRAINT relationship_follows_target_scope_fk; "
            "ALTER TABLE relationship_follows ADD CONSTRAINT relationship_follows_target_scope_fk "
            "FOREIGN KEY (target_id,ig_account_id,user_id) REFERENCES targets(id,ig_account_id,user_id) ON DELETE CASCADE",
            "relationship_follows_target_scope_fk",
        ),
        (
            "ALTER TABLE relationship_follows DROP CONSTRAINT relationship_follows_engagement_scope_fk; "
            "ALTER TABLE relationship_follows ADD CONSTRAINT relationship_follows_engagement_scope_fk "
            "FOREIGN KEY (prerequisite_engagement_id,ig_account_id,user_id) REFERENCES story_engagements(id,ig_account_id,user_id) ON DELETE CASCADE",
            "relationship_follows_engagement_scope_fk",
        ),
        (
            "ALTER TABLE relationship_follows DROP CONSTRAINT relationship_follows_due_check; ALTER TABLE relationship_follows ADD CONSTRAINT relationship_follows_due_check CHECK (true)",
            "relationship_follows_due_check",
        ),
        (
            "ALTER TABLE relationship_follows DROP CONSTRAINT relationship_follows_lease_check",
            "relationship_follows_lease_check",
        ),
        (
            "ALTER TABLE relationship_follows DROP CONSTRAINT relationship_follows_owned_check",
            "relationship_follows_owned_check",
        ),
        (
            "ALTER TABLE relationship_follows DROP CONSTRAINT relationship_follows_status_check",
            "relationship_follows_status_check",
        ),
        (
            "ALTER TABLE relationship_follows DROP CONSTRAINT relationship_follows_evidence_check",
            "relationship_follows_evidence_check",
        ),
        (
            "ALTER TABLE relationship_follows DROP CONSTRAINT relationship_follows_owner_id_check",
            "relationship_follows_owner_id_check",
        ),
        (
            "ALTER TABLE agent_configs DROP CONSTRAINT agent_configs_daily_follow_limit_check",
            "agent_configs_daily_follow_limit_check",
        ),
        (
            "ALTER TABLE instagram_execution_controls DROP CONSTRAINT instagram_execution_controls_action_check; ALTER TABLE instagram_execution_controls ADD CONSTRAINT instagram_execution_controls_action_check CHECK(action IS NULL OR action IN ('view','like','reply','dm'))",
            "instagram_execution_controls_action_check",
        ),
        (
            "DROP INDEX relationship_follows_due_idx; CREATE INDEX relationship_follows_due_idx ON relationship_follows(unfollow_due_at,next_attempt_at,created_at)",
            "relationship_follows_due_idx",
        ),
        (
            "DROP INDEX relationship_follows_account_status_idx; CREATE INDEX relationship_follows_account_status_idx ON relationship_follows(ig_account_id,user_id,status)",
            "relationship_follows_account_status_idx",
        ),
        (
            "ALTER TABLE relationship_follows DROP CONSTRAINT relationship_follows_account_user_fk; ALTER TABLE relationship_follows ADD CONSTRAINT relationship_follows_account_user_fk FOREIGN KEY (ig_account_id,user_id) REFERENCES ig_accounts(id,user_id) ON DELETE CASCADE NOT VALID",
            "relationship_follows_account_user_fk",
        ),
    ],
)
def test_follow_lifecycle_rejects_missing_or_weakened_structure(
    database_url, mutation, expected
):
    with psycopg.connect(database_url, row_factory=dict_row) as connection:

        @contextmanager
        def connect(_url, *, connect_timeout):
            assert connect_timeout == 15
            yield connection

        try:
            connection.execute(mutation)
            with pytest.raises(ValueError) as raised:
                healthcheck.probe_follow_lifecycle(
                    {"DATABASE_URL": database_url}, connect=connect
                )
            assert "0020" in str(raised.value)
            assert expected in str(raised.value)
        finally:
            connection.rollback()


def test_follow_lifecycle_uses_explicit_schema(database_url):
    with psycopg.connect(database_url, row_factory=dict_row) as connection:

        @contextmanager
        def connect(_url, *, connect_timeout):
            yield connection

        try:
            connection.execute("ALTER SCHEMA public RENAME TO follow_preflight")
            healthcheck.probe_follow_lifecycle(
                {"DATABASE_URL": database_url, "DATABASE_SCHEMA": "follow_preflight"},
                connect=connect,
            )
        finally:
            connection.rollback()


def test_follow_lifecycle_requires_database_and_redacts_connection_failure():
    with pytest.raises(ValueError, match="DATABASE_URL"):
        healthcheck.probe_follow_lifecycle({})

    def fail(*args, **kwargs):
        raise RuntimeError("password=fixture-secret")

    with pytest.raises(ValueError) as raised:
        healthcheck.probe_follow_lifecycle(
            {"DATABASE_URL": "postgresql://fixture-secret@invalid/db"}, connect=fail
        )
    assert "could not query PostgreSQL" in str(raised.value)
    assert "fixture-secret" not in str(raised.value)


def test_follow_lifecycle_cli_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(healthcheck, "probe_follow_lifecycle", calls.append)
    assert healthcheck._main(["follow-lifecycle"]) == 0
    assert len(calls) == 1
