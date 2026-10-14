"""The release gate must prove migration 0015 is ready before restart."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

import pytest

from deploy import healthcheck

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = REPO_ROOT / "app/src/db/migrations/0015_onboarding_launch_state.sql"


@pytest.fixture(scope="module")
def migrated_database_url() -> str:
    if subprocess.run(
        ["pg_isready"], capture_output=True, check=False
    ).returncode != 0:
        pytest.skip("local PostgreSQL is unavailable")

    database = f"engagemate_schema_preflight_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    created = subprocess.run(
        ["createdb", database], capture_output=True, text=True, check=False
    )
    if created.returncode != 0:
        pytest.fail(
            f"could not create disposable PostgreSQL database: {created.stderr}"
        )

    database_url = os.environ.get(
        "TEST_DATABASE_URL_TEMPLATE", "postgresql:///%s"
    ) % database
    try:
        prerequisites = subprocess.run(
            [
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-d",
                database_url,
                "-c",
                """
                CREATE TABLE ig_accounts (
                    id uuid PRIMARY KEY,
                    user_id uuid NOT NULL,
                    warmup_started_at timestamp with time zone,
                    UNIQUE (id, user_id)
                );
                CREATE TABLE brand_profiles (
                    id uuid PRIMARY KEY,
                    user_id uuid NOT NULL
                );
                CREATE UNIQUE INDEX brand_profiles_user_idx
                    ON brand_profiles (user_id);
                CREATE TABLE agent_configs (
                    id uuid PRIMARY KEY,
                    user_id uuid NOT NULL,
                    ig_account_id uuid NOT NULL,
                    enabled boolean NOT NULL DEFAULT false,
                    created_at timestamp with time zone NOT NULL DEFAULT now(),
                    updated_at timestamp with time zone NOT NULL DEFAULT now()
                );
                CREATE TABLE ig_sessions (
                    id uuid PRIMARY KEY,
                    user_id uuid NOT NULL,
                    ig_account_id uuid NOT NULL
                );
                CREATE TABLE run_summaries (
                    id uuid PRIMARY KEY,
                    user_id uuid NOT NULL,
                    ig_account_id uuid NOT NULL
                );
                """,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if prerequisites.returncode != 0:
            pytest.fail(
                "could not prepare disposable PostgreSQL schema: "
                + prerequisites.stderr
            )

        migrated = subprocess.run(
            [
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-d",
                database_url,
                "-f",
                str(MIGRATION_PATH),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if migrated.returncode != 0:
            pytest.fail(f"could not apply migration 0015: {migrated.stderr}")

        yield database_url
    finally:
        subprocess.run(
            ["dropdb", "--if-exists", "--force", database],
            capture_output=True,
            check=False,
        )


SchemaProbeRow = Mapping[str, bool]


def _schema_probe_row(**overrides: bool) -> SchemaProbeRow:
    row = {
        "agent_configs_launched_at": True,
        "ig_sessions_account_user_id_idx": True,
        "brand_profiles_ig_account_id": True,
        "brand_profiles_user_idx": True,
        "brand_profiles_account_user_fk": True,
    }
    row.update(overrides)
    return row


class _Cursor:
    def __init__(self, row: SchemaProbeRow) -> None:
        self.row = row
        self.query: str | None = None
        self.parameters: Mapping[str, Any] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self, query: str, parameters: Mapping[str, Any] | None = None
    ) -> None:
        self.query = query
        self.parameters = parameters

    def fetchone(self) -> SchemaProbeRow:
        return self.row


class _Connection:
    def __init__(self, row: SchemaProbeRow) -> None:
        self.cursor_instance = _Cursor(row)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self.cursor_instance


def _connect_with(row: SchemaProbeRow, *, record: list[_Connection] | None = None):
    def connect(_url: str, *, connect_timeout: int) -> _Connection:
        assert connect_timeout == 15
        connection = _Connection(row)
        if record is not None:
            record.append(connection)
        return connection

    return connect


def test_schema_probe_accepts_the_column_and_valid_ready_index() -> None:
    healthcheck.probe_database_schema(
        {"DATABASE_URL": "postgresql://example.invalid/engagemate"},
        connect=_connect_with(_schema_probe_row()),
    )


def test_schema_probe_binds_public_when_the_search_path_is_empty() -> None:
    """A pooled Neon session has no search_path, so the schema must be bound.

    `current_schema()` is NULL through the PgBouncer pooler, which made every
    `table_schema = current_schema()` comparison NULL and reported a fully
    migrated database as missing all five requirements.
    """

    connections: list[_Connection] = []
    healthcheck.probe_database_schema(
        {"DATABASE_URL": "postgresql://example.invalid/engagemate"},
        connect=_connect_with(_schema_probe_row(), record=connections),
    )

    cursor = connections[0].cursor_instance
    assert cursor.parameters == {"schema": "public"}
    assert cursor.query is not None
    assert "current_schema()" not in cursor.query


def test_schema_probe_honors_an_explicit_schema() -> None:
    connections: list[_Connection] = []
    healthcheck.probe_database_schema(
        {
            "DATABASE_URL": "postgresql://example.invalid/engagemate",
            "DATABASE_SCHEMA": "engagemate",
        },
        connect=_connect_with(_schema_probe_row(), record=connections),
    )

    assert connections[0].cursor_instance.parameters == {"schema": "engagemate"}


def _execute_sql(database_url: str, sql: str) -> None:
    result = subprocess.run(
        [
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-d",
            database_url,
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_schema_probe_passes_with_an_empty_search_path(
    migrated_database_url: str,
) -> None:
    """Reproduce the pooled Neon session against a real migrated database.

    libpq's `options=-csearch_path=` gives the session the same empty
    search_path a PgBouncer-pooled connection arrives with. Before the schema
    was bound, this raised "migration 0015 is incomplete" listing all five
    requirements even though every one of them exists.
    """

    separator = "&" if "?" in migrated_database_url else "?"
    pooled_url = f"{migrated_database_url}{separator}options=-csearch_path%3D"

    healthcheck.probe_database_schema({"DATABASE_URL": pooled_url})


def test_schema_probe_checks_the_migrated_index_column_order(
    migrated_database_url: str,
) -> None:
    environment = {"DATABASE_URL": migrated_database_url}
    healthcheck.probe_database_schema(environment)

    try:
        _execute_sql(
            migrated_database_url,
            """
            DROP INDEX ig_sessions_account_user_id_idx;
            CREATE INDEX ig_sessions_account_user_id_idx
                ON ig_sessions (user_id, ig_account_id, id);
            """,
        )
        with pytest.raises(ValueError, match="ig_sessions_account_user_id_idx"):
            healthcheck.probe_database_schema(environment)
    finally:
        _execute_sql(
            migrated_database_url,
            """
            DROP INDEX IF EXISTS ig_sessions_account_user_id_idx;
            CREATE INDEX ig_sessions_account_user_id_idx
                ON ig_sessions (ig_account_id, user_id, id);
            """,
        )


def test_schema_probe_checks_the_brand_profile_unique_index_column_order(
    migrated_database_url: str,
) -> None:
    environment = {"DATABASE_URL": migrated_database_url}
    healthcheck.probe_database_schema(environment)

    try:
        _execute_sql(
            migrated_database_url,
            """
            DROP INDEX brand_profiles_user_idx;
            CREATE UNIQUE INDEX brand_profiles_user_idx
                ON brand_profiles (ig_account_id, user_id);
            """,
        )
        with pytest.raises(ValueError, match="brand_profiles_user_idx"):
            healthcheck.probe_database_schema(environment)
    finally:
        _execute_sql(
            migrated_database_url,
            """
            DROP INDEX IF EXISTS brand_profiles_user_idx;
            CREATE UNIQUE INDEX brand_profiles_user_idx
                ON brand_profiles (user_id, ig_account_id);
            """,
        )


@pytest.mark.parametrize(
    "replacement",
    [
        """
        FOREIGN KEY (ig_account_id) REFERENCES ig_accounts(id)
        """,
        """
        FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) NOT VALID
        """,
    ],
    ids=["wrong-shape", "not-validated"],
)
def test_schema_probe_checks_the_brand_profile_account_fk_shape_and_validation(
    migrated_database_url: str,
    replacement: str,
) -> None:
    environment = {"DATABASE_URL": migrated_database_url}
    healthcheck.probe_database_schema(environment)

    try:
        _execute_sql(
            migrated_database_url,
            f"""
            ALTER TABLE brand_profiles
                DROP CONSTRAINT brand_profiles_account_user_fk;
            ALTER TABLE brand_profiles
                ADD CONSTRAINT brand_profiles_account_user_fk {replacement};
            """,
        )
        with pytest.raises(ValueError, match="brand_profiles_account_user_fk"):
            healthcheck.probe_database_schema(environment)
    finally:
        _execute_sql(
            migrated_database_url,
            """
            ALTER TABLE brand_profiles
                DROP CONSTRAINT IF EXISTS brand_profiles_account_user_fk;
            ALTER TABLE brand_profiles
                ADD CONSTRAINT brand_profiles_account_user_fk
                FOREIGN KEY (ig_account_id, user_id)
                REFERENCES ig_accounts(id, user_id)
                ON DELETE CASCADE;
            """,
        )


@pytest.mark.parametrize(
    ("key", "missing"),
    [
        ("agent_configs_launched_at", "agent_configs.launched_at"),
        ("ig_sessions_account_user_id_idx", "ig_sessions_account_user_id_idx"),
        ("brand_profiles_ig_account_id", "brand_profiles.ig_account_id"),
        ("brand_profiles_user_idx", "brand_profiles_user_idx"),
        ("brand_profiles_account_user_fk", "brand_profiles_account_user_fk"),
    ],
)
def test_schema_probe_refuses_each_missing_requirement(
    key: str, missing: str
) -> None:
    with pytest.raises(ValueError, match=missing):
        healthcheck.probe_database_schema(
            {"DATABASE_URL": "postgresql://example.invalid/engagemate"},
            connect=_connect_with(_schema_probe_row(**{key: False})),
        )


def test_schema_probe_requires_database_url() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        healthcheck.probe_database_schema(
            {}, connect=_connect_with(_schema_probe_row())
        )


def test_schema_probe_redacts_connection_failures() -> None:
    def refuse(_url: str, *, connect_timeout: int):
        del connect_timeout
        raise RuntimeError("password=do-not-print")

    with pytest.raises(ValueError) as raised:
        healthcheck.probe_database_schema(
            {"DATABASE_URL": "postgresql://user:do-not-print@example.invalid/db"},
            connect=refuse,
        )

    message = str(raised.value)
    assert "do-not-print" not in message
    assert "could not query PostgreSQL" in message


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_onboarding_email_preflight_names_the_missing_migration() -> None:
    import importlib.util
    import pathlib

    import pytest

    spec = importlib.util.spec_from_file_location(
        "healthcheck_onboarding", pathlib.Path(__file__).resolve().parents[2] / "deploy" / "healthcheck.py")
    healthcheck = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(healthcheck)

    def connect_returning(row):
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params): self.params = params
            def fetchone(self): return row

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return Cursor()

        return lambda url, connect_timeout: Connection()

    env = {"DATABASE_URL": "postgresql://example"}
    healthcheck.probe_onboarding_email_schema(
        env, connect=connect_returning({"outbox_exists": True, "activation_exists": True}))
    with pytest.raises(ValueError, match="migration 0023"):
        healthcheck.probe_onboarding_email_schema(
            env, connect=connect_returning({"outbox_exists": False, "activation_exists": True}))


def test_email_preference_probe_requires_migration_0024() -> None:
    from contextlib import contextmanager

    def connect_returning(row):
        class Cursor:
            def execute(self, *args):
                pass

            def fetchone(self):
                return row

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        @contextmanager
        def connect(*args, **kwargs):
            class Connection:
                def cursor(self):
                    return Cursor()
            yield Connection()

        return connect

    env = {"DATABASE_URL": "postgresql://example"}
    complete = {"preferences_exist": True, "digest_exists": True, "activation_exists": True}
    healthcheck.probe_email_preferences_schema(env, connect=connect_returning(complete))
    with pytest.raises(ValueError, match="migration 0024"):
        healthcheck.probe_email_preferences_schema(
            env, connect=connect_returning(complete | {"digest_exists": False}))
