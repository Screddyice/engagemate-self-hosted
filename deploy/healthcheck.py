"""Fail-closed deployment preflight checks for the Story worker."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

_LEASE_FIELDS = frozenset(
    {
        "reference",
        "host",
        "port",
        "username",
        "password",
        "country",
        "region",
        "city",
    }
)
_GEO_FIELDS = frozenset({"country", "timezone", "locale"})
_CATALOG_FIELDS = frozenset(
    {"country", "region", "city", "product_id", "plan_id", "location_id"}
)
_REPORTING_TIMERS = (
    ("engagemate-growth-snapshot.timer", "engagemate-growth-snapshot.service"),
    ("engagemate-weekly-report.timer", "engagemate-weekly-report.service"),
)
_ENABLED_UNIT_STATES = frozenset({"enabled", "enabled-runtime"})
_PENDING_SERVICE_STATES = frozenset(
    {"activating", "active", "reloading", "deactivating"}
)

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def _connect_postgres(url: str, *, connect_timeout: int) -> Any:
    """Import psycopg only for the schema preflight that needs it."""

    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        url,
        connect_timeout=connect_timeout,
        row_factory=dict_row,
    )


def probe_database_schema(
    environment: Mapping[str, str],
    *,
    connect: Callable[..., Any] = _connect_postgres,
) -> None:
    """Refuse a restart until migration 0015 is complete and usable."""

    database_url = environment.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("DATABASE_URL is required")

    # Never resolve these names through the session search_path.
    #
    # Production reaches Neon through its PgBouncer pooler, and a pooled
    # session arrives with an empty search_path: `show search_path` returns
    # nothing, `current_schema()` is NULL, and `to_regclass('agent_configs')`
    # is NULL too. Every `table_schema = current_schema()` comparison then
    # evaluates to NULL rather than true, so on 2026-09-05 this probe called
    # all five requirements missing against a database where migration 0015
    # was fully applied -- and deploy.sh refuses the release on that answer.
    # Bind the schema instead, so the probe returns the same verdict through
    # the pooler and the direct endpoint.
    schema = environment.get("DATABASE_SCHEMA", "").strip() or "public"

    try:
        with (
            connect(database_url, connect_timeout=15) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                          FROM information_schema.columns
                         WHERE table_schema = %(schema)s
                           AND table_name = 'agent_configs'
                           AND column_name = 'launched_at'
                           AND data_type = 'timestamp with time zone'
                    ) AS agent_configs_launched_at,
                    EXISTS (
                        SELECT 1
                          FROM pg_index AS index_meta
                         WHERE index_meta.indexrelid = to_regclass(
                                   quote_ident(%(schema)s)
                                   || '.ig_sessions_account_user_id_idx'
                               )
                           AND index_meta.indisvalid
                           AND index_meta.indisready
                           AND (
                               SELECT array_agg(
                                          attribute.attname
                                          ORDER BY key.ordinality
                                      )
                                 FROM unnest(index_meta.indkey)
                                      WITH ORDINALITY AS key(attnum, ordinality)
                                 JOIN pg_attribute AS attribute
                                   ON attribute.attrelid = index_meta.indrelid
                                  AND attribute.attnum = key.attnum
                           ) = ARRAY[
                               'ig_account_id', 'user_id', 'id'
                           ]::name[]
                    ) AS ig_sessions_account_user_id_idx,
                    EXISTS (
                        SELECT 1
                          FROM information_schema.columns
                         WHERE table_schema = %(schema)s
                           AND table_name = 'brand_profiles'
                           AND column_name = 'ig_account_id'
                           AND data_type = 'uuid'
                           AND is_nullable = 'YES'
                    ) AS brand_profiles_ig_account_id,
                    EXISTS (
                        SELECT 1
                          FROM pg_index AS index_meta
                         WHERE index_meta.indexrelid = to_regclass(
                                   quote_ident(%(schema)s)
                                   || '.brand_profiles_user_idx'
                               )
                           AND index_meta.indrelid = to_regclass(
                                   quote_ident(%(schema)s) || '.brand_profiles'
                               )
                           AND index_meta.indisunique
                           AND index_meta.indisvalid
                           AND index_meta.indisready
                           AND index_meta.indpred IS NULL
                           AND index_meta.indexprs IS NULL
                           AND index_meta.indnkeyatts = 2
                           AND (
                               SELECT array_agg(
                                          attribute.attname
                                          ORDER BY key.ordinality
                                      )
                                 FROM unnest(index_meta.indkey)
                                      WITH ORDINALITY AS key(attnum, ordinality)
                                 JOIN pg_attribute AS attribute
                                   ON attribute.attrelid = index_meta.indrelid
                                  AND attribute.attnum = key.attnum
                           ) = ARRAY[
                               'user_id', 'ig_account_id'
                           ]::name[]
                    ) AS brand_profiles_user_idx,
                    EXISTS (
                        SELECT 1
                          FROM pg_constraint AS constraint_meta
                         WHERE constraint_meta.conname =
                               'brand_profiles_account_user_fk'
                           AND constraint_meta.contype = 'f'
                           AND constraint_meta.conrelid = to_regclass(
                                   quote_ident(%(schema)s) || '.brand_profiles'
                               )
                           AND constraint_meta.confrelid = to_regclass(
                                   quote_ident(%(schema)s) || '.ig_accounts'
                               )
                           AND constraint_meta.convalidated
                           AND constraint_meta.confdeltype = 'c'
                           AND constraint_meta.confupdtype = 'a'
                           AND (
                               SELECT array_agg(
                                          attribute.attname
                                          ORDER BY key.ordinality
                                      )
                                 FROM unnest(constraint_meta.conkey)
                                      WITH ORDINALITY AS key(attnum, ordinality)
                                 JOIN pg_attribute AS attribute
                                   ON attribute.attrelid = constraint_meta.conrelid
                                  AND attribute.attnum = key.attnum
                           ) = ARRAY[
                               'ig_account_id', 'user_id'
                           ]::name[]
                           AND (
                               SELECT array_agg(
                                          attribute.attname
                                          ORDER BY key.ordinality
                                      )
                                 FROM unnest(constraint_meta.confkey)
                                      WITH ORDINALITY AS key(attnum, ordinality)
                                 JOIN pg_attribute AS attribute
                                   ON attribute.attrelid = constraint_meta.confrelid
                                  AND attribute.attnum = key.attnum
                           ) = ARRAY[
                               'id', 'user_id'
                           ]::name[]
                    ) AS brand_profiles_account_user_fk
                """,
                {"schema": schema},
            )
            row = cursor.fetchone()
    except Exception as error:
        raise ValueError("could not query PostgreSQL for onboarding schema") from error

    ready = row or {}
    requirements = (
        ("agent_configs_launched_at", "agent_configs.launched_at"),
        ("ig_sessions_account_user_id_idx", "ig_sessions_account_user_id_idx"),
        ("brand_profiles_ig_account_id", "brand_profiles.ig_account_id"),
        ("brand_profiles_user_idx", "brand_profiles_user_idx"),
        ("brand_profiles_account_user_fk", "brand_profiles_account_user_fk"),
    )
    missing = [label for key, label in requirements if not ready.get(key, False)]
    if missing:
        raise ValueError("migration 0015 is incomplete; missing " + ", ".join(missing))


def probe_hosted_login_schema(environment, *, connect=_connect_postgres):
    """The disabled rollout needs no table; enabled browsers require migration25."""
    if environment.get("HOSTED_LOGIN_ENABLED", "false").lower() != "true":
        return
    try:
        with (
            connect(environment["DATABASE_URL"], connect_timeout=15) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("""
                SELECT id, user_id, account_id, username, expected_ig_user_id,
                       proxy_id, created_at, expires_at, released_at
                FROM hosted_login_reservations LIMIT 0
            """)
            cursor.execute("""
                SELECT count(*) AS n FROM pg_index
                WHERE indexrelid IN (
                    to_regclass('hosted_login_active_user'),
                    to_regclass('hosted_login_active_proxy')
                ) AND indisunique AND indisvalid AND indisready
                  AND indpred IS NOT NULL
            """)
            if cursor.fetchone()["n"] != 2:
                raise ValueError()
    except Exception:  # noqa: BLE001 - redact database and credential-bearing failures
        raise ValueError("hosted login requires complete migration 0025") from None


def probe_relationship_reconciliation(
    environment: Mapping[str, str],
    *,
    connect: Callable[..., Any] = _connect_postgres,
) -> None:
    """Require migration 0026 before the worker reserves follow-back checks.

    The worker's job claim, follow claim, and reconciliation store all query
    this table, so a release without it fails every run rather than one path.
    """

    url = environment.get("DATABASE_URL", "").strip()
    if not url:
        raise ValueError("DATABASE_URL is required")
    schema = environment.get("DATABASE_SCHEMA", "").strip() or "public"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("DATABASE_SCHEMA must be a plain identifier")
    table = f"{schema}.relationship_reconciliation_attempts"
    try:
        with (
            connect(url, connect_timeout=15) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT
                  to_regclass(%(table)s) IS NOT NULL AS table_exists,
                  (SELECT count(*) FROM pg_index i
                    WHERE i.indexrelid IN (to_regclass(%(budget)s), to_regclass(%(due)s))
                      AND i.indisvalid AND i.indisready) AS indexes,
                  EXISTS (SELECT 1 FROM pg_constraint c
                    WHERE c.conname = 'relationship_follows_scope_unique'
                      AND c.contype = 'u'
                      AND c.conrelid = to_regclass(%(follows)s)) AS scope_unique,
                  EXISTS (SELECT 1 FROM pg_constraint c
                    WHERE c.conname = 'relationship_reconciliation_scope_fk'
                      AND c.contype = 'f' AND c.convalidated
                      AND c.confdeltype = 'c'
                      AND c.conrelid = to_regclass(%(table)s)) AS scope_fk
                """,
                {
                    "table": table,
                    "budget": f"{schema}.relationship_reconciliation_budget_idx",
                    "due": f"{schema}.relationship_reconciliation_due_idx",
                    "follows": f"{schema}.relationship_follows",
                },
            )
            row = cursor.fetchone() or {}
            if row.get("table_exists"):
                cursor.execute(
                    f"""
                    SELECT id, user_id, ig_account_id, relationship_id, worker_id,
                           reserved_at, lease_expires_at, finished_at,
                           next_check_at, outcome, failure_code
                    FROM {table} LIMIT 0
                    """
                )
    except Exception as error:
        raise ValueError(
            "could not query PostgreSQL for relationship reconciliation"
        ) from error
    requirements = (
        ("table_exists", "relationship_reconciliation_attempts"),
        ("scope_unique", "relationship_follows_scope_unique"),
        ("scope_fk", "relationship_reconciliation_scope_fk"),
    )
    missing = [label for key, label in requirements if not row.get(key)]
    if row.get("indexes") != 2:
        missing.append("reconciliation budget/due indexes")
    if missing:
        raise ValueError("migration 0026 is incomplete; missing " + ", ".join(missing))


def probe_onboarding_email_schema(
    environment: Mapping[str, str],
    *,
    connect: Callable[..., Any] = _connect_postgres,
) -> None:
    """Require the onboarding email outbox before the action-emails service polls it."""

    database_url = environment.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    schema = environment.get("DATABASE_SCHEMA", "").strip() or "public"
    try:
        with (
            connect(database_url, connect_timeout=15) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT
                  to_regclass(%(outbox)s) IS NOT NULL AS outbox_exists,
                  to_regclass(%(activation)s) IS NOT NULL AS activation_exists
                """,
                {
                    "outbox": f"{schema}.onboarding_emails",
                    "activation": f"{schema}.onboarding_email_activation",
                },
            )
            row = cursor.fetchone()
    except Exception as error:  # pragma: no cover - driver/network details
        raise ValueError("could not inspect onboarding email schema") from error
    if not row or not row["outbox_exists"] or not row["activation_exists"]:
        raise ValueError(
            "migration 0023 is incomplete; apply onboarding_emails and onboarding_email_activation"
        )


def probe_notification_recipient_schema(environment, *, connect=_connect_postgres):
    database_url = environment.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    schema = environment.get("DATABASE_SCHEMA", "").strip() or "public"
    with connect(database_url, connect_timeout=15) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL AND to_regclass(%s) IS NOT NULL AS present",
                       (f"{schema}.notification_recipients", f"{schema}.notification_email_copies"))
        row = cursor.fetchone()
    if not row or not row["present"]:
        raise ValueError("migration 0029 is incomplete; apply notification recipient tables")


def probe_reconnect_email_schema(environment, *, connect=_connect_postgres):
    """Require the transactional login-reminder outbox before deployment."""
    database_url = environment.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    schema = environment.get("DATABASE_SCHEMA", "").strip() or "public"
    with connect(database_url, connect_timeout=15) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (f"{schema}.reconnect_emails",))
        row = cursor.fetchone()
    if not row or not row["present"]:
        raise ValueError("migration 0028 is incomplete; apply reconnect_emails")


def probe_email_preferences_schema(
    environment: Mapping[str, str],
    *,
    connect: Callable[..., Any] = _connect_postgres,
) -> None:
    """Require migration 0024 before the action-emails service reads its tables."""

    database_url = environment.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    schema = environment.get("DATABASE_SCHEMA", "").strip() or "public"
    try:
        with (
            connect(database_url, connect_timeout=15) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT
                  to_regclass(%(preferences)s) IS NOT NULL AS preferences_exist,
                  to_regclass(%(digest)s) IS NOT NULL AS digest_exists,
                  to_regclass(%(activation)s) IS NOT NULL AS activation_exists
                """,
                {
                    "preferences": f"{schema}.email_preferences",
                    "digest": f"{schema}.activity_digest_emails",
                    "activation": f"{schema}.activity_digest_activation",
                },
            )
            row = cursor.fetchone()
    except Exception as error:  # pragma: no cover - driver/network details
        raise ValueError("could not inspect email preference schema") from error
    if not row or not all(row[key] for key in ("preferences_exist", "digest_exists", "activation_exists")):
        raise ValueError(
            "migration 0024 is incomplete; apply email_preferences, activity_digest_emails "
            "and activity_digest_activation"
        )


def probe_warmup_defaults(environment: Mapping[str, str]) -> None:
    """Require likes-only defaults without changing existing configurations."""
    url = environment.get("DATABASE_URL", "").strip()
    if not url:
        raise ValueError("DATABASE_URL is required")
    try:
        with _connect_postgres(url, connect_timeout=15) as connection:
            row = connection.execute(
                """
                SELECT count(*) AS matched
                  FROM information_schema.columns AS col
                  JOIN (VALUES ('daily_like_limit', '20'),
                               ('daily_reply_limit', '0'),
                               ('daily_dm_limit', '0'),
                               ('daily_view_limit', '0'),
                               ('follow_after_engagement', 'false'),
                               ('follow_reel_message_enabled', 'false'),
                               ('auto_unfollow_enabled', 'false')) AS expected(name, value)
                    ON col.column_name = expected.name
                   AND col.column_default = expected.value
                 WHERE col.table_schema = %s AND col.table_name = 'agent_configs'
                """,
                (environment.get("DATABASE_SCHEMA", "").strip() or "public",),
            ).fetchone()
    except Exception as error:
        raise ValueError("could not query PostgreSQL for warm-up defaults") from error
    if not row or row["matched"] != 7:
        raise ValueError(
            "migration 0034 is incomplete; likes-only onboarding defaults are missing"
        )


def probe_follow_reel_messages(environment: Mapping[str, str]) -> None:
    """Block worker and Story rollout until the shared-message ledger exists."""
    url = environment.get("DATABASE_URL", "").strip()
    if not url:
        raise ValueError("DATABASE_URL is required")
    schema = environment.get("DATABASE_SCHEMA", "").strip() or "public"
    try:
        with _connect_postgres(url, connect_timeout=15) as connection:
            row = connection.execute(
                """SELECT
                     to_regclass(%s) IS NOT NULL AS messages,
                     to_regclass(%s) IS NOT NULL AS rotation,
                     EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema=%s AND table_name='follow_reel_messages'
                         AND column_name='claim_count') AS reservation,
                     (SELECT count(*)=3 FROM information_schema.columns
                       WHERE table_schema=%s AND table_name='agent_configs'
                         AND column_name IN ('follow_reel_message_enabled','follow_reel_message_enabled_at','follow_reel_message_template')) AS opt_in,
                     (SELECT count(*)=2 FROM information_schema.columns
                       WHERE table_schema=%s AND table_name='agent_configs'
                         AND ((column_name='daily_reel_message_limit' AND data_type='integer')
                           OR (column_name='warmup_exempt' AND data_type='boolean'
                             AND is_nullable='NO' AND column_default='false'))) AS quotas""",
                (f"{schema}.follow_reel_messages", f"{schema}.follow_reel_rotation", schema, schema, schema),
            ).fetchone()
    except Exception as error:
        raise ValueError("could not inspect follow reel message schema") from error
    if not row or not all(row[key] for key in ("messages", "rotation", "reservation", "opt_in")):
        raise ValueError("migration 0030 is incomplete; apply the follow reel message ledger")
    if not row.get("quotas"):
        raise ValueError("migration 0032 is incomplete; apply the separate message quotas")


def _schema_text(value: Any) -> Any:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def probe_follower_audience(
    environment: Mapping[str, str],
    *,
    connect: Callable[..., Any] = _connect_postgres,
) -> None:
    """Require the follower queue and live profile schema before restarting."""
    url = environment.get("DATABASE_URL", "").strip()
    if not url:
        raise ValueError("DATABASE_URL is required")
    schema = environment.get("DATABASE_SCHEMA", "").strip() or "public"
    columns = {
        "brand_profiles": {
            "follower_audience_approved_at": ("timestamp with time zone", True)
        },
        "hermes_execution_runs": {"discovery_report": ("jsonb", False)},
        "follower_source_states": {
            "user_id": ("uuid", False),
            "ig_account_id": ("uuid", False),
            "source_id": ("uuid", False),
            "cursor": ("text", True),
            "exhausted": ("boolean", False),
            "updated_at": ("timestamp with time zone", False),
        },
        "follower_candidates": {
            "user_id": ("uuid", False),
            "ig_account_id": ("uuid", False),
            "owner_id": ("text", False),
            "handle": ("text", False),
            "status": ("text", False),
            "last_checked_at": ("timestamp with time zone", True),
            "next_check_at": ("timestamp with time zone", True),
            "created_at": ("timestamp with time zone", False),
        },
        "follower_candidate_sources": {
            "user_id": ("uuid", False),
            "ig_account_id": ("uuid", False),
            "owner_id": ("text", False),
            "source_id": ("uuid", False),
        },
        "onboarding_profile_cache": {
            "user_id": ("uuid", False),
            "ig_account_id": ("uuid", False),
            "username": ("text", False),
            "profile": ("jsonb", False),
            "captured_at": ("timestamp with time zone", False),
        },
        "profile_check_state": {
            "user_id": ("uuid", False),
            "ig_account_id": ("uuid", False),
            "next_allowed_at": ("timestamp with time zone", False),
            "failure_reason": ("text", True),
        },
    }
    try:
        with (
            connect(url, connect_timeout=15) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT table_name, column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema=%(schema)s AND table_name=ANY(%(tables)s)
            """,
                {"schema": schema, "tables": list(columns)},
            )
            actual_columns = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT rel.relname AS table_name, c.contype AS kind,
                       c.conname AS name, c.convalidated AS validated,
                       c.confdeltype AS on_delete, target.relname AS referenced_table,
                       target_ns.nspname AS referenced_schema,
                       ARRAY(SELECT att.attname FROM unnest(c.conkey) WITH ORDINALITY key(num, ord)
                             JOIN pg_attribute att ON att.attrelid=c.conrelid AND att.attnum=key.num
                             ORDER BY key.ord) AS columns,
                       ARRAY(SELECT att.attname FROM unnest(c.confkey) WITH ORDINALITY key(num, ord)
                             JOIN pg_attribute att ON att.attrelid=c.confrelid AND att.attnum=key.num
                             ORDER BY key.ord) AS referenced_columns,
                       pg_get_expr(c.conbin, c.conrelid) AS expression,
                       idx.indisvalid AS index_valid, idx.indisready AS index_ready
                FROM pg_constraint c
                JOIN pg_class rel ON rel.oid=c.conrelid
                JOIN pg_namespace ns ON ns.oid=rel.relnamespace
                LEFT JOIN pg_class target ON target.oid=c.confrelid
                LEFT JOIN pg_namespace target_ns ON target_ns.oid=target.relnamespace
                LEFT JOIN pg_index idx ON idx.indexrelid=c.conindid
                WHERE ns.nspname=%(schema)s AND rel.relname=ANY(%(tables)s)
            """,
                {"schema": schema, "tables": list(columns)},
            )
            constraints = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT idx.indisvalid AS valid, idx.indisready AS ready,
                       idx.indpred IS NULL AND idx.indexprs IS NULL AS unconditional,
                       ARRAY(SELECT att.attname FROM unnest(idx.indkey) WITH ORDINALITY key(num, ord)
                             JOIN pg_attribute att ON att.attrelid=idx.indrelid AND att.attnum=key.num
                             ORDER BY key.ord) AS columns
                FROM pg_index idx
                WHERE idx.indexrelid=to_regclass(quote_ident(%(schema)s) || '.follower_candidates_due_idx')
                  AND idx.indrelid=to_regclass(quote_ident(%(schema)s) || '.follower_candidates')
            """,
                {"schema": schema},
            )
            queue_index = cursor.fetchone()
    except Exception as error:  # redact connection details
        raise ValueError(
            "could not query PostgreSQL for follower audience schema"
        ) from error

    actual = {
        (_schema_text(row["table_name"]), _schema_text(row["column_name"])): row
        for row in actual_columns
    }
    missing = []
    for table, fields in columns.items():
        for name, (data_type, nullable) in fields.items():
            row = actual.get((table, name))
            if (
                row is None
                or _schema_text(row["data_type"]) != data_type
                or (_schema_text(row["is_nullable"]) == "YES") != nullable
            ):
                missing.append(f"{table}.{name}")

    defaults = {
        ("hermes_execution_runs", "discovery_report"): "'{}'::jsonb",
        ("follower_source_states", "exhausted"): "false",
        ("follower_source_states", "updated_at"): "now()",
        ("follower_candidates", "status"): "'new'::text",
        ("follower_candidates", "created_at"): "now()",
        ("onboarding_profile_cache", "captured_at"): "now()",
    }
    for key, expected in defaults.items():
        if _schema_text(actual.get(key, {}).get("column_default")) != expected:
            missing.append(".".join(key) + " default")

    def key_matches(row: dict, table: str, kind: str, keys: list[str]) -> bool:
        return (
            _schema_text(row["table_name"]) == table
            and _schema_text(row["kind"]) == kind
            and row["validated"] is True
            and [_schema_text(key) for key in row["columns"]] == keys
        )

    sender = ["user_id", "ig_account_id"]
    primary_keys = {
        "follower_source_states": [*sender, "source_id"],
        "follower_candidates": [*sender, "owner_id"],
        "follower_candidate_sources": [*sender, "owner_id", "source_id"],
        "onboarding_profile_cache": [*sender, "username"],
        "profile_check_state": sender,
    }
    for table, keys in primary_keys.items():
        if not any(
            key_matches(row, table, "p", keys)
            and row["index_valid"] is True
            and row["index_ready"] is True
            for row in constraints
        ):
            missing.append(table + " primary key")

    foreign_keys = [
        (table, ["ig_account_id", "user_id"], "ig_accounts", ["id", "user_id"])
        for table in (
            "follower_source_states",
            "follower_candidates",
            "onboarding_profile_cache",
            "profile_check_state",
        )
    ] + [
        ("follower_source_states", ["source_id"], "brand_reference_accounts", ["id"]),
        (
            "follower_candidate_sources",
            [*sender, "owner_id"],
            "follower_candidates",
            [*sender, "owner_id"],
        ),
        (
            "follower_candidate_sources",
            [*sender, "source_id"],
            "follower_source_states",
            [*sender, "source_id"],
        ),
    ]
    for table, keys, target, target_keys in foreign_keys:
        if not any(
            key_matches(row, table, "f", keys)
            and _schema_text(row["on_delete"]) == "c"
            and _schema_text(row["referenced_schema"]) == schema
            and _schema_text(row["referenced_table"]) == target
            and [_schema_text(key) for key in row["referenced_columns"]] == target_keys
            for row in constraints
        ):
            missing.append(f"{table} foreign key to {target}")

    expressions = {
        "hermes_execution_runs": "jsonb_typeof(discovery_report) = 'object'::text",
        "onboarding_profile_cache": "jsonb_typeof(profile) = 'object'::text",
        "follower_candidates": "status = ANY (ARRAY['new'::text, 'excluded'::text, 'no_story'::text, 'unknown'::text, 'available'::text])",
    }

    def normalized(expression: Any) -> str:
        return re.sub(r"[\s()]", "", _schema_text(expression) or "")

    for table, expression in expressions.items():
        if not any(
            _schema_text(row["table_name"]) == table
            and _schema_text(row["kind"]) == "c"
            and row["validated"] is True
            and normalized(row["expression"]) == normalized(expression)
            for row in constraints
        ):
            missing.append(table + " check constraint")
    if (
        not queue_index
        or not all(
            queue_index[key] is True for key in ("valid", "ready", "unconditional")
        )
        or [_schema_text(key) for key in queue_index["columns"]]
        != ["user_id", "ig_account_id", "next_check_at", "created_at"]
    ):
        missing.append("follower_candidates_due_idx")
    if missing:
        raise ValueError(
            "migrations 0018/0019 are incomplete; missing " + ", ".join(missing)
        )


def probe_follow_lifecycle(
    environment: Mapping[str, str],
    *,
    connect: Callable[..., Any] = _connect_postgres,
) -> None:
    """Require migrations 0020/0033 before starting relationship workers or routes."""
    url = environment.get("DATABASE_URL", "").strip()
    if not url:
        raise ValueError("DATABASE_URL is required")
    schema = environment.get("DATABASE_SCHEMA", "").strip() or "public"
    ledger = "relationship_follows"
    columns = {
        "agent_configs": {
            "follow_after_engagement": ("boolean", False, "false"),
            "auto_unfollow_enabled": ("boolean", False, "false"),
            "daily_follow_limit": ("integer", False, "3"),
            "follow_enabled_at": ("timestamp with time zone", True, None),
        },
        ledger: {
            "id": ("uuid", False, "gen_random_uuid()"),
            **{
                key: ("uuid", False, None)
                for key in (
                    "user_id",
                    "ig_account_id",
                    "target_id",
                    "prerequisite_engagement_id",
                )
            },
            "owner_id": ("text", False, None),
            "status": ("text", False, "'follow_pending'::text"),
            "claimed_by": ("text", True, None),
            "lease_token": ("uuid", True, None),
            "last_error": ("text", True, None),
            "evidence": ("jsonb", False, "'{}'::jsonb"),
            **{
                key: ("timestamp with time zone", True, None)
                for key in (
                    "confirmed_follow_at",
                    "unfollow_due_at",
                    "unfollowed_at",
                    "lease_expires_at",
                    "attempted_at",
                    "follow_attempted_at",
                    "unfollow_attempted_at",
                    "follow_reserved_at",
                    "unfollow_reserved_at",
                    "next_attempt_at",
                )
            },
            "created_at": ("timestamp with time zone", False, "now()"),
            "updated_at": ("timestamp with time zone", False, "now()"),
        },
    }
    try:
        with (
            connect(url, connect_timeout=15) as connection,
            connection.cursor() as cursor,
        ):
            params = {
                "schema": schema,
                "tables": [*columns, "instagram_execution_controls"],
            }
            cursor.execute(
                """
                SELECT table_name,column_name,data_type,is_nullable,column_default
                FROM information_schema.columns WHERE table_schema=%(schema)s AND table_name=ANY(%(tables)s)
            """,
                params,
            )
            actual = {
                (_schema_text(r["table_name"]), _schema_text(r["column_name"])): dict(r)
                for r in cursor.fetchall()
            }
            cursor.execute(
                """
                SELECT rel.relname AS table_name,c.conname AS name,c.contype AS kind,c.convalidated AS validated,
                    c.confdeltype AS on_delete, target.relname AS target, target_ns.nspname AS target_schema,
                    ARRAY(SELECT att.attname FROM unnest(c.conkey) WITH ORDINALITY key(num,ord)
                      JOIN pg_attribute att ON att.attrelid=c.conrelid AND att.attnum=key.num ORDER BY key.ord) AS columns,
                    ARRAY(SELECT att.attname FROM unnest(c.confkey) WITH ORDINALITY key(num,ord)
                      JOIN pg_attribute att ON att.attrelid=c.confrelid AND att.attnum=key.num ORDER BY key.ord) AS target_columns,
                    pg_get_expr(c.conbin,c.conrelid) AS expression,
                    idx.indisvalid AS valid,idx.indisready AS ready
                FROM pg_constraint c JOIN pg_class rel ON rel.oid=c.conrelid
                JOIN pg_namespace ns ON ns.oid=rel.relnamespace
                LEFT JOIN pg_class target ON target.oid=c.confrelid
                LEFT JOIN pg_namespace target_ns ON target_ns.oid=target.relnamespace
                LEFT JOIN pg_index idx ON idx.indexrelid=c.conindid
                WHERE ns.nspname=%(schema)s AND rel.relname=ANY(%(tables)s)
            """,
                params,
            )
            constraints = {
                (_schema_text(r["table_name"]), _schema_text(r["name"])): dict(r)
                for r in cursor.fetchall()
            }
            cursor.execute(
                """
                SELECT rel.relname AS name, idx.indisvalid AS valid,idx.indisready AS ready,
                    idx.indexprs IS NULL AS plain,pg_get_expr(idx.indpred,idx.indrelid) AS predicate,
                    ARRAY(SELECT att.attname FROM unnest(idx.indkey) WITH ORDINALITY key(num,ord)
                      JOIN pg_attribute att ON att.attrelid=idx.indrelid AND att.attnum=key.num ORDER BY key.ord) AS columns
                FROM pg_index idx JOIN pg_class rel ON rel.oid=idx.indexrelid
                WHERE idx.indrelid=to_regclass(quote_ident(%(schema)s)||'.relationship_follows')
            """,
                params,
            )
            indexes = {_schema_text(r["name"]): dict(r) for r in cursor.fetchall()}
    except Exception as error:
        raise ValueError(
            "could not query PostgreSQL for follow lifecycle schema"
        ) from error

    def norm(value):
        return re.sub(r"[\s()]", "", _schema_text(value) or "")

    def keys(row, key):
        return [_schema_text(v) for v in row.get(key, [])]

    missing = []
    for table, fields in columns.items():
        for name, (kind, nullable, default) in fields.items():
            row = actual.get((table, name), {})
            if (
                _schema_text(row.get("data_type")) != kind
                or (_schema_text(row.get("is_nullable")) == "YES") != nullable
            ):
                missing.append(f"{table}.{name}")
            if _schema_text(row.get("column_default")) != default:
                missing.append(f"{table}.{name} default")

    for name, kind, expected in (
        ("relationship_follows_pkey", "p", ["id"]),
        (
            "relationship_follows_account_owner_unique",
            "u",
            ["ig_account_id", "owner_id"],
        ),
    ):
        row = constraints.get((ledger, name), {})
        if (
            _schema_text(row.get("kind")) != kind
            or row.get("validated") is not True
            or row.get("valid") is not True
            or row.get("ready") is not True
            or keys(row, "columns") != expected
        ):
            missing.append(name)
    for name, source_keys, target, target_keys in (
        ("relationship_follows_user_id_fkey", ["user_id"], "users", ["id"]),
        (
            "relationship_follows_account_user_fk",
            ["ig_account_id", "user_id"],
            "ig_accounts",
            ["id", "user_id"],
        ),
        (
            "relationship_follows_target_scope_fk",
            ["target_id", "ig_account_id", "user_id"],
            "targets",
            ["id", "ig_account_id", "user_id"],
        ),
        (
            "relationship_follows_engagement_scope_fk",
            ["prerequisite_engagement_id", "ig_account_id", "user_id"],
            "story_engagements",
            ["id", "ig_account_id", "user_id"],
        ),
    ):
        row = constraints.get((ledger, name), {})
        on_delete = "a" if target in {"targets", "story_engagements"} else "c"
        if (
            _schema_text(row.get("kind")) != "f"
            or row.get("validated") is not True
            or _schema_text(row.get("on_delete")) != on_delete
            or _schema_text(row.get("target_schema")) != schema
            or _schema_text(row.get("target")) != target
            or keys(row, "columns") != source_keys
            or keys(row, "target_columns") != target_keys
        ):
            missing.append(name)
    checks = {
        (
            "agent_configs",
            "agent_configs_daily_follow_limit_check",
        ): "daily_follow_limit >= 0 AND daily_follow_limit <= 20",
        (
            "instagram_execution_controls",
            "instagram_execution_controls_action_check",
        ): "action IS NULL OR action = ANY (ARRAY['view'::text,'like'::text,'reply'::text,'dm'::text,'follow'::text,'unfollow'::text])",
        (
            ledger,
            "relationship_follows_owner_id_check",
        ): "owner_id ~ '^[0-9]{1,32}$'::text",
        (
            ledger,
            "relationship_follows_status_check",
        ): "status = ANY (ARRAY['follow_pending'::text,'follow_running'::text,'following'::text,'unfollow_running'::text,'unfollowed'::text,'skipped'::text,'uncertain'::text,'cancelled'::text])",
        (
            ledger,
            "relationship_follows_due_check",
        ): "(confirmed_follow_at IS NULL AND unfollow_due_at IS NULL) OR (confirmed_follow_at IS NOT NULL AND unfollow_due_at IS NOT NULL AND unfollow_due_at = confirmed_follow_at + '120:00:00'::interval)",
        (
            ledger,
            "relationship_follows_owned_check",
        ): "status <> ALL (ARRAY['following'::text,'unfollow_running'::text,'unfollowed'::text]) OR confirmed_follow_at IS NOT NULL",
        (
            ledger,
            "relationship_follows_lease_check",
        ): "(status = ANY (ARRAY['follow_running'::text,'unfollow_running'::text]) AND claimed_by IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status <> ALL (ARRAY['follow_running'::text,'unfollow_running'::text]) AND claimed_by IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)",
        (
            ledger,
            "relationship_follows_evidence_check",
        ): "jsonb_typeof(evidence) = 'object'::text",
    }
    for key, expression in checks.items():
        row = constraints.get(key, {})
        if (
            _schema_text(row.get("kind")) != "c"
            or row.get("validated") is not True
            or norm(row.get("expression")) != norm(expression)
        ):
            missing.append(key[1])
    for name, expected, predicate in (
        (
            "relationship_follows_due_idx",
            ["unfollow_due_at", "next_attempt_at", "created_at"],
            "status = ANY (ARRAY['follow_pending'::text,'following'::text])",
        ),
        (
            "relationship_follows_account_status_idx",
            ["user_id", "ig_account_id", "status"],
            "",
        ),
    ):
        row = indexes.get(name, {})
        if (
            not all(row.get(k) is True for k in ("valid", "ready", "plain"))
            or keys(row, "columns") != expected
            or norm(row.get("predicate")) != norm(predicate)
        ):
            missing.append(name)
    if missing:
        raise ValueError("migrations 0020/0033 are incomplete; missing " + ", ".join(missing))


def _json_list(environment: Mapping[str, str], name: str, label: str) -> list[Any]:
    raw = environment.get(name, "").strip()
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be valid JSON") from error
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    return value


def _complete_records(values: list[Any], fields: frozenset[str], label: str) -> None:
    for value in values:
        if not isinstance(value, dict) or not fields.issubset(value):
            raise ValueError(f"{label} contains an incomplete record")
        if any(value[field] is None or value[field] == "" for field in fields):
            raise ValueError(f"{label} contains a blank field")


_GEO_KEYS = ("country", "region", "city")


def _geo_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    return tuple(str(record[key]) for key in _GEO_KEYS)  # type: ignore[return-value]


def validate_managed_proxy_environment(environment: Mapping[str, str]) -> None:
    """Refuse a managed-mode deploy that would stop the services from booting.

    `Settings()` raises during construction when managed mode has no geo
    catalog, and api, worker, and scheduler all load the same env file. Setting
    IPROYAL_MODE=managed without the catalog therefore does not merely stop
    purchasing, it stops the whole stack, at startup, before any healthcheck
    can report why. Verified against production on 2026-08-17: the catalog key
    was absent from .env.api entirely and .env.scheduler was a zero-byte file.

    Only managed mode is gated. Inventory mode buys nothing and needs none of
    this.
    """

    if environment.get("IPROYAL_MODE", "").strip() != "managed":
        return

    catalog = _json_list(environment, "IPROYAL_GEO_CATALOG_JSON", "IPRoyal geo catalog")
    _complete_records(
        catalog,
        frozenset({*_GEO_KEYS, "product_id", "plan_id", "location_id"}),
        "IPRoyal geo catalog",
    )

    # The scheduler is the only service that spends, and it reads this from its
    # own env file. An empty one means managed mode boots and then silently
    # never buys anything.
    if not environment.get("IPROYAL_ACCESS_TOKEN", "").strip():
        raise ValueError(
            "IPROYAL_ACCESS_TOKEN is required for managed mode; "
            "deploy/.env.scheduler is empty"
        )

    supported = _json_list(
        environment, "FINGERPRINT_GEO_MAP_JSON", "fingerprint geo map"
    )
    _complete_records(supported, frozenset(_GEO_KEYS), "fingerprint geo map")
    anchors = _json_list(environment, "PROXY_GEO_ANCHORS_JSON", "proxy geo anchors")
    _complete_records(
        anchors, frozenset({*_GEO_KEYS, "latitude", "longitude"}), "proxy geo anchors"
    )

    catalogued = {_geo_key(entry) for entry in catalog}
    anchored = {_geo_key(entry) for entry in anchors}
    for entry in supported:
        geo = _geo_key(entry)
        label = "/".join(geo)
        # Capture accepts any geography in the fingerprint map. One the
        # scheduler cannot buy for is an account that connects and then never
        # runs, which is the silent dead end already documented in the README.
        if geo not in catalogued:
            raise ValueError(f"{label} has no IPRoyal catalog entry")
        if geo not in anchored:
            raise ValueError(f"{label} has no proxy geo anchor")

    for entry in anchors:
        try:
            latitude = float(entry["latitude"])
            longitude = float(entry["longitude"])
        except (TypeError, ValueError) as error:
            raise ValueError("proxy geo anchors contain a non-numeric point") from error
        label = "/".join(_geo_key(entry))
        if not -90.0 <= latitude <= 90.0:
            raise ValueError(f"{label} anchor latitude is out of range")
        if not -180.0 <= longitude <= 180.0:
            raise ValueError(f"{label} anchor longitude is out of range")


def _require_private_llm_endpoint(url: str) -> None:
    """Reject any LLM endpoint that is not loopback or private.

    A hostname is refused rather than resolved: resolution can change under us,
    and a name that points somewhere private today can point anywhere tomorrow.
    """

    import ipaddress
    import urllib.parse

    if not url:
        raise ValueError("HERMES_AGENT_URL is required")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("HERMES_AGENT_URL must be an http(s) URL")
    host = parts.hostname
    if not host:
        raise ValueError("HERMES_AGENT_URL must include a host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(
            "HERMES_AGENT_URL host must be a literal IP address, not a name: "
            "a name can be repointed after this check passes"
        ) from None
    if not (address.is_loopback or address.is_private):
        raise ValueError(
            "HERMES_AGENT_URL must point at a loopback or private address; "
            "prompts carry scraped story content and must not leave the host"
        )


def validate_worker_environment(environment: Mapping[str, str]) -> None:
    """Validate tenant isolation inputs before Playwright can launch Chrome."""

    for name in (
        "DATABASE_URL",
        "INTERNAL_API_KEY",
        "SESSION_ENCRYPTION_KEY",
    ):
        if not environment.get(name, "").strip():
            raise ValueError(f"{name} is required")

    if environment.get("INTELLIGENCE_PROVIDER", "").strip() != "hermes_agent":
        raise ValueError("Hermes agent intelligence provider is required")
    try:
        job_lease_seconds = int(environment.get("JOB_LEASE_SECONDS", "120").strip())
        heartbeat_interval_seconds = int(
            environment.get("HEARTBEAT_INTERVAL_SECONDS", "30").strip()
        )
    except ValueError:
        raise ValueError(
            "JOB_LEASE_SECONDS and HEARTBEAT_INTERVAL_SECONDS must be integers"
        ) from None
    if job_lease_seconds < 30:
        raise ValueError("JOB_LEASE_SECONDS must be at least 30")
    if heartbeat_interval_seconds < 5:
        raise ValueError("HEARTBEAT_INTERVAL_SECONDS must be at least 5")
    if heartbeat_interval_seconds * 2 > job_lease_seconds:
        raise ValueError(
            "HEARTBEAT_INTERVAL_SECONDS must be no more than half of "
            "JOB_LEASE_SECONDS"
        )
    # The guarantee is that prompts never leave this host or its private
    # network, not that one exact string is used. Pinning the literal
    # 127.0.0.1:8643 broke the worker the moment production moved to the shared
    # private LLM shim on the docker bridge, which is equally private and is the
    # endpoint that holds the correct Codex identity. api/engagemate/config.py
    # enforces the same rule with `validate_hermes_agent_url_is_private`; this
    # check now agrees with it instead of being stricter for no added safety.
    _require_private_llm_endpoint(environment.get("HERMES_AGENT_URL", "").strip())
    if len(environment.get("HERMES_AGENT_API_KEY", "").strip()) < 32:
        raise ValueError("HERMES_AGENT_API_KEY must be at least 32 characters")
    if environment.get("HERMES_AGENT_MODEL", "").strip() != "engagemate-agent":
        raise ValueError("HERMES_AGENT_MODEL must be engagemate-agent")
    try:
        hermes_timeout = float(
            environment.get("HERMES_EXECUTION_TIMEOUT_SECONDS", "1800").strip()
        )
    except ValueError:
        hermes_timeout = 0
    if not 60 <= hermes_timeout <= 3600:
        raise ValueError("HERMES_EXECUTION_TIMEOUT_SECONDS must be between 60 and 3600")

    proxy_mode = environment.get("IPROYAL_MODE", "").strip()
    if proxy_mode == "import":
        leases = _json_list(
            environment,
            "IPROYAL_ISP_LEASES_JSON",
            "lease inventory",
        )
        _complete_records(leases, _LEASE_FIELDS, "lease inventory")
    elif proxy_mode == "managed":
        catalog = _json_list(
            environment,
            "IPROYAL_GEO_CATALOG_JSON",
            "IPRoyal geo catalog",
        )
        _complete_records(catalog, _CATALOG_FIELDS, "IPRoyal geo catalog")
    elif proxy_mode == "inventory":
        pass
    else:
        raise ValueError("IPRoyal import, inventory, or managed mode is required")

    health_url = environment.get("PROXY_HEALTH_URL", "").strip()
    parsed = urlparse(health_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("proxy health URL must use HTTPS")

    seed = environment.get("FINGERPRINT_SEED", "")
    if len(seed.encode("utf-8")) < 32:
        raise ValueError("fingerprint seed must contain at least 32 bytes")

    registry = _json_list(
        environment,
        "FINGERPRINT_GEO_MAP_JSON",
        "geography registry",
    )
    _complete_records(registry, _GEO_FIELDS, "geography registry")


def _tcp_connect(host: str, port: int, timeout: float) -> None:
    """Open and immediately close one TCP connection, or raise OSError."""

    with socket.create_connection((host, port), timeout=timeout):
        return


def probe_intelligence_backend(
    environment: Mapping[str, str],
    *,
    connect: Callable[[str, int, float], None] = _tcp_connect,
    timeout: float = 5.0,
) -> None:
    """Refuse to pass when the configured LLM backend answers nothing.

    Every other preflight in this file reads strings. That is why the whole
    platform could sit unable to generate a single reply while the documented
    `healthcheck.py worker` gate exited 0: the Hermes settings were all
    present and well-formed, and nothing was listening on the port they named.
    A string check cannot tell a live gateway from a dead one, so this opens
    the socket and reports the address it could not reach.

    The probe connects and hangs up. It sends no prompt and runs no inference,
    so it is safe to run on every deploy and costs no model turn.
    """

    provider = environment.get("INTELLIGENCE_PROVIDER", "").strip() or "hermes_agent"
    if provider == "anthropic":
        # The emergency provider is a public API, so reachability is not the
        # thing that breaks; a missing key is. Spending a token on a preflight
        # would be the wrong trade.
        if not environment.get("ANTHROPIC_API_KEY", "").strip():
            raise ValueError(
                "INTELLIGENCE_PROVIDER=anthropic requires ANTHROPIC_API_KEY"
            )
        return
    if provider != "hermes_agent":
        raise ValueError(
            f"INTELLIGENCE_PROVIDER must be hermes_agent or anthropic, got {provider!r}"
        )

    endpoint = environment.get("HERMES_AGENT_URL", "").strip()
    if not endpoint:
        raise ValueError("HERMES_AGENT_URL is required for the hermes_agent provider")
    parsed = urlparse(endpoint)
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        raise ValueError(
            f"HERMES_AGENT_URL must name a host and port, got {endpoint!r}"
        )
    try:
        connect(host, port, timeout)
    except OSError as error:
        raise ValueError(
            f"no LLM backend answered at {host}:{port} "
            f"({type(error).__name__}); EngageMate cannot generate any "
            "engagement text until the Hermes gateway is running and reachable "
            "from this address"
        ) from error


def _systemd_properties(
    unit: str,
    names: Sequence[str],
    *,
    run_command: RunCommand,
) -> dict[str, str]:
    try:
        completed = run_command(
            [
                "/usr/bin/systemctl",
                "show",
                unit,
                *(f"--property={name}" for name in names),
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return {}
    if completed.returncode != 0:
        return {}
    properties: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator and name in names:
            properties[name] = value
    return properties


def report_reporting_timer_health(
    *,
    run_command: RunCommand = subprocess.run,
    write_line: Callable[[str], Any] = print,
) -> bool:
    """Print redacted state for each independent reporting timer."""

    healthy = True
    for timer, service in _REPORTING_TIMERS:
        timer_properties = _systemd_properties(
            timer,
            ("LoadState", "UnitFileState", "ActiveState"),
            run_command=run_command,
        )
        loaded = timer_properties.get("LoadState") == "loaded"
        unit_file_state = timer_properties.get("UnitFileState") or (
            "unknown" if loaded else "not-found"
        )
        active_state = timer_properties.get("ActiveState") or "unknown"

        if not loaded:
            service_result = "not-checked"
            last_run = "not-checked"
            status = "missing"
            healthy = False
        elif unit_file_state not in _ENABLED_UNIT_STATES:
            service_result = "not-checked"
            last_run = "not-checked"
            if unit_file_state == "disabled" and active_state == "inactive":
                status = "disabled"
            elif unit_file_state == "disabled":
                status = "disabled-active"
                healthy = False
            else:
                status = "unavailable"
                healthy = False
        else:
            service_properties = _systemd_properties(
                service,
                (
                    "LoadState",
                    "ActiveState",
                    "Result",
                    "ExecMainExitTimestampMonotonic",
                ),
                run_command=run_command,
            )
            service_result = service_properties.get("Result") or "unknown"
            service_loaded = service_properties.get("LoadState") == "loaded"
            service_state = service_properties.get("ActiveState") or "unknown"
            exited_at = service_properties.get("ExecMainExitTimestampMonotonic")
            if not service_loaded:
                last_run = "unavailable"
            elif service_state in _PENDING_SERVICE_STATES:
                last_run = "pending"
            elif not exited_at or exited_at == "0":
                last_run = "never"
            else:
                last_run = "recorded"
            if active_state != "active":
                status = "inactive"
                healthy = False
            elif not service_loaded:
                status = "failed"
                healthy = False
            elif service_state in _PENDING_SERVICE_STATES:
                status = "pending"
                healthy = False
            elif service_state != "inactive":
                status = "failed"
                healthy = False
            elif last_run != "recorded":
                status = "pending"
                healthy = False
            elif service_result != "success":
                status = "failed"
                healthy = False
            else:
                status = "healthy"

        write_line(
            f"timer={timer} unit_file_state={unit_file_state} "
            f"active_state={active_state} service_result={service_result} "
            f"last_run={last_run} health={status}"
        )
    return healthy


# A value is safe to leave bare only if nothing in it means something to a
# shell. Everything else has to be quoted, because deploy.sh, the container
# watchdog unit, and the documented healthcheck procedure all read these files
# with `set -a; . deploy/.env.api`.
#
# Two real values broke that. `DATABASE_URL` carries `&` between its query
# parameters, and bash reads `NAME=a&b` as a backgrounded assignment, so the
# parent shell never sees NAME at all. `FINGERPRINT_GEO_MAP_JSON` is JSON, and
# the shell eats its double quotes, leaving `[{country:US}]`. Both surfaced as
# "configuration error", which reads like the config is wrong rather than the
# reader.
_SHELL_SAFE_BARE = re.compile(r"^[A-Za-z0-9_./:@=+,-]*$")
_ENV_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def unsafe_env_assignments(text: str) -> list[str]:
    """Return the names whose values a shell would not reproduce verbatim."""

    unsafe: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_ASSIGNMENT.match(line)
        if match is None:
            continue
        name, value = match.group(1), match.group(2)
        if not value:
            continue
        quoted = len(value) > 1 and value[0] == value[-1] and value[0] in "\"'"
        if quoted or _SHELL_SAFE_BARE.match(value):
            continue
        unsafe.append(name)
    return unsafe


def validate_env_file_is_shell_safe(path: str) -> None:
    """Raise when an env file would not survive `set -a; . <file>`."""

    try:
        with open(path, encoding="utf-8") as source:
            text = source.read()
    except OSError as error:
        raise ValueError(f"cannot read {path}: {error}") from error
    unsafe = unsafe_env_assignments(text)
    if unsafe:
        raise ValueError(
            f"{path}: quote these values or a shell will mangle them: "
            + ", ".join(sorted(unsafe))
        )


def _main(arguments: list[str]) -> int:
    if arguments == ["worker"]:
        validate_worker_environment(os.environ)
    elif arguments == ["proxy-managed"]:
        validate_managed_proxy_environment(os.environ)
    elif arguments == ["intelligence"]:
        probe_intelligence_backend(os.environ)
    elif arguments == ["schema"]:
        probe_database_schema(os.environ)
    elif arguments == ["hosted-login"]:
        probe_hosted_login_schema(os.environ)
    elif arguments == ["onboarding-emails"]:
        probe_onboarding_email_schema(os.environ)
    elif arguments == ["notification-recipients"]:
        probe_notification_recipient_schema(os.environ)
    elif arguments == ["reconnect-emails"]:
        probe_reconnect_email_schema(os.environ)
    elif arguments == ["email-preferences"]:
        probe_email_preferences_schema(os.environ)
    elif arguments == ["follow-lifecycle"]:
        probe_follow_lifecycle(os.environ)
    elif arguments == ["follow-reel-messages"]:
        probe_follow_reel_messages(os.environ)
    elif arguments == ["relationship-reconciliation"]:
        probe_relationship_reconciliation(os.environ)
    elif arguments == ["follower-audience"]:
        probe_follower_audience(os.environ)
    elif arguments == ["warmup-defaults"]:
        probe_warmup_defaults(os.environ)
    elif arguments == ["timers"]:
        return 0 if report_reporting_timer_health() else 1
    elif len(arguments) == 2 and arguments[0] == "env-file":
        validate_env_file_is_shell_safe(arguments[1])
    else:
        raise ValueError(
            "usage: healthcheck.py worker|proxy-managed|intelligence"
            "|schema|warmup-defaults|follower-audience|follow-lifecycle|follow-reel-messages|relationship-reconciliation|timers|env-file <path>"
        )
    return 0


__all__ = [
    "probe_database_schema",
    "probe_follow_lifecycle",
    "probe_follower_audience",
    "probe_hosted_login_schema",
    "probe_intelligence_backend",
    "probe_onboarding_email_schema",
    "probe_relationship_reconciliation",
    "probe_warmup_defaults",
    "report_reporting_timer_health",
    "unsafe_env_assignments",
    "validate_env_file_is_shell_safe",
    "validate_managed_proxy_environment",
    "validate_worker_environment",
]


if __name__ == "__main__":
    try:
        raise SystemExit(_main(sys.argv[1:]))
    except ValueError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
