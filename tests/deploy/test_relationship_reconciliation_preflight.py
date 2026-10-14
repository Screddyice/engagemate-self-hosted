"""The deployment gate validates migration 0026 against real PostgreSQL."""

from contextlib import contextmanager

import psycopg
import pytest
from psycopg.rows import dict_row

from deploy import healthcheck
from tests.worker import test_canary_scripts as canary_fixtures

database_url = canary_fixtures.database_url


def _probe(connection, database_url, **extra):
    @contextmanager
    def connect(_url, *, connect_timeout):
        assert connect_timeout == 15
        yield connection

    healthcheck.probe_relationship_reconciliation(
        {"DATABASE_URL": database_url, **extra}, connect=connect
    )


def test_reconciliation_accepts_migrations_with_empty_search_path(database_url):
    healthcheck.probe_relationship_reconciliation(
        {"DATABASE_URL": database_url + "?options=-csearch_path%3D"}
    )


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (
            "DROP TABLE relationship_reconciliation_attempts",
            "relationship_reconciliation_attempts",
        ),
        (
            "ALTER TABLE relationship_reconciliation_attempts DROP COLUMN next_check_at",
            "could not query",
        ),
        (
            "DROP INDEX relationship_reconciliation_due_idx",
            "reconciliation budget/due indexes",
        ),
        (
            "DROP INDEX relationship_reconciliation_budget_idx",
            "reconciliation budget/due indexes",
        ),
        (
            "ALTER TABLE relationship_reconciliation_attempts"
            " DROP CONSTRAINT relationship_reconciliation_scope_fk",
            "relationship_reconciliation_scope_fk",
        ),
        (
            "ALTER TABLE relationship_reconciliation_attempts"
            " DROP CONSTRAINT relationship_reconciliation_scope_fk;"
            " ALTER TABLE relationship_follows"
            " DROP CONSTRAINT relationship_follows_scope_unique",
            "relationship_follows_scope_unique",
        ),
    ],
)
def test_reconciliation_rejects_missing_structure(database_url, mutation, expected):
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        try:
            connection.execute(mutation)
            with pytest.raises(ValueError) as raised:
                _probe(connection, database_url)
            message = str(raised.value)
            assert expected in message
            if expected != "could not query":
                assert "0026" in message
        finally:
            connection.rollback()


def test_reconciliation_uses_explicit_schema(database_url):
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        try:
            connection.execute("ALTER SCHEMA public RENAME TO reconciliation_preflight")
            _probe(
                connection,
                database_url,
                DATABASE_SCHEMA="reconciliation_preflight",
            )
        finally:
            connection.rollback()


def test_reconciliation_rejects_unsafe_schema_and_redacts_failure():
    with pytest.raises(ValueError, match="DATABASE_URL"):
        healthcheck.probe_relationship_reconciliation({})
    with pytest.raises(ValueError, match="plain identifier"):
        healthcheck.probe_relationship_reconciliation(
            {"DATABASE_URL": "postgresql:///x", "DATABASE_SCHEMA": 'a"; DROP'}
        )

    def fail(*args, **kwargs):
        raise RuntimeError("password=fixture-secret")

    with pytest.raises(ValueError) as raised:
        healthcheck.probe_relationship_reconciliation(
            {"DATABASE_URL": "postgresql://fixture-secret@invalid/db"}, connect=fail
        )
    assert "could not query PostgreSQL" in str(raised.value)
    assert "fixture-secret" not in str(raised.value)


def test_reconciliation_cli_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(healthcheck, "probe_relationship_reconciliation", calls.append)
    assert healthcheck._main(["relationship-reconciliation"]) == 0
    assert len(calls) == 1


