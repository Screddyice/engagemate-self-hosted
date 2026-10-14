"""Follower API and worker releases require complete migrations 0018 and 0019."""

from contextlib import contextmanager

import psycopg
import pytest
from psycopg.rows import dict_row

from deploy import healthcheck
from tests.worker import test_canary_scripts as canary_fixtures

database_url = canary_fixtures.database_url


def test_follower_preflight_accepts_all_migrations_with_empty_search_path(database_url):
    healthcheck.probe_follower_audience(
        {"DATABASE_URL": database_url + "?options=-csearch_path%3D"}
    )


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (
            "ALTER TABLE brand_profiles DROP COLUMN follower_audience_approved_at",
            "brand_profiles.follower_audience_approved_at",
        ),
        (
            "ALTER TABLE follower_source_states DROP COLUMN cursor",
            "follower_source_states.cursor",
        ),
        (
            "ALTER TABLE follower_candidates ALTER COLUMN status DROP DEFAULT",
            "follower_candidates.status default",
        ),
        (
            "ALTER TABLE follower_candidates DROP CONSTRAINT follower_candidates_status_check",
            "follower_candidates check constraint",
        ),
        (
            "DROP TABLE follower_candidate_sources",
            "follower_candidate_sources primary key",
        ),
        (
            "DROP INDEX follower_candidates_due_idx; CREATE INDEX follower_candidates_due_idx ON follower_candidates(ig_account_id,user_id,next_check_at,created_at)",
            "follower_candidates_due_idx",
        ),
        (
            "ALTER TABLE hermes_execution_runs ALTER COLUMN discovery_report DROP NOT NULL",
            "hermes_execution_runs.discovery_report",
        ),
        (
            "ALTER TABLE hermes_execution_runs ALTER COLUMN discovery_report DROP DEFAULT",
            "hermes_execution_runs.discovery_report default",
        ),
        (
            "ALTER TABLE hermes_execution_runs DROP CONSTRAINT hermes_execution_runs_discovery_report_check",
            "hermes_execution_runs check constraint",
        ),
        (
            "ALTER TABLE onboarding_profile_cache DROP CONSTRAINT onboarding_profile_cache_profile_check",
            "onboarding_profile_cache check constraint",
        ),
        (
            "ALTER TABLE profile_check_state DROP COLUMN failure_reason",
            "profile_check_state.failure_reason",
        ),
        ("DROP TABLE profile_check_state", "profile_check_state primary key"),
    ],
)
def test_follower_preflight_rejects_partial_migration(database_url, mutation, expected):
    with psycopg.connect(database_url, row_factory=dict_row) as connection:

        @contextmanager
        def connect(_url, *, connect_timeout):
            assert connect_timeout == 15
            yield connection

        try:
            connection.execute(mutation)
            with pytest.raises(ValueError) as raised:
                healthcheck.probe_follower_audience(
                    {"DATABASE_URL": database_url}, connect=connect
                )
            assert "0018/0019" in str(raised.value)
            assert expected in str(raised.value)
        finally:
            connection.rollback()


@pytest.mark.parametrize(
    "replacement",
    [
        "FOREIGN KEY (ig_account_id) REFERENCES ig_accounts(id) ON DELETE CASCADE",
        "FOREIGN KEY (ig_account_id,user_id) REFERENCES ig_accounts(id,user_id) ON DELETE CASCADE NOT VALID",
        "FOREIGN KEY (ig_account_id,user_id) REFERENCES ig_accounts(id,user_id)",
    ],
)
def test_follower_preflight_validates_tenant_fk_shape_validation_and_cascade(
    database_url, replacement
):
    with psycopg.connect(database_url, row_factory=dict_row) as connection:

        @contextmanager
        def connect(_url, *, connect_timeout):
            yield connection

        try:
            connection.execute(
                "ALTER TABLE onboarding_profile_cache DROP CONSTRAINT onboarding_profile_cache_ig_account_id_user_id_fkey"
            )
            connection.execute(
                "ALTER TABLE onboarding_profile_cache ADD CONSTRAINT replacement_fk "
                + replacement
            )
            with pytest.raises(
                ValueError, match="onboarding_profile_cache foreign key to ig_accounts"
            ):
                healthcheck.probe_follower_audience(
                    {"DATABASE_URL": database_url}, connect=connect
                )
        finally:
            connection.rollback()


def test_follower_preflight_rejects_missing_provenance_relationship(database_url):
    with psycopg.connect(database_url, row_factory=dict_row) as connection:

        @contextmanager
        def connect(_url, *, connect_timeout):
            yield connection

        try:
            constraint = connection.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid='follower_candidate_sources'::regclass AND confrelid='follower_source_states'::regclass"
            ).fetchone()["conname"]
            if isinstance(constraint, bytes):
                constraint = constraint.decode("utf-8")
            connection.execute(
                psycopg.sql.SQL(
                    "ALTER TABLE follower_candidate_sources DROP CONSTRAINT {}"
                ).format(psycopg.sql.Identifier(constraint))
            )
            with pytest.raises(
                ValueError,
                match="follower_candidate_sources foreign key to follower_source_states",
            ):
                healthcheck.probe_follower_audience(
                    {"DATABASE_URL": database_url}, connect=connect
                )
        finally:
            connection.rollback()


def test_follower_preflight_requires_database_and_redacts_connection_errors():
    with pytest.raises(ValueError, match="DATABASE_URL"):
        healthcheck.probe_follower_audience({})

    def refuse(*_args, **_kwargs):
        raise RuntimeError("password=fixture-private")

    with pytest.raises(ValueError) as raised:
        healthcheck.probe_follower_audience(
            {"DATABASE_URL": "postgresql://fixture-private@invalid/db"}, connect=refuse
        )
    assert "fixture-private" not in str(raised.value)
    assert "could not query PostgreSQL" in str(raised.value)


def test_follower_preflight_cli_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(healthcheck, "probe_follower_audience", calls.append)
    assert healthcheck._main(["follower-audience"]) == 0
    assert len(calls) == 1


def test_follower_preflight_uses_explicit_schema(database_url):
    with psycopg.connect(database_url, row_factory=dict_row) as connection:

        @contextmanager
        def connect(_url, *, connect_timeout):
            yield connection

        try:
            connection.execute("ALTER SCHEMA public RENAME TO follower_preflight")
            healthcheck.probe_follower_audience(
                {"DATABASE_URL": database_url, "DATABASE_SCHEMA": "follower_preflight"},
                connect=connect,
            )
        finally:
            connection.rollback()
