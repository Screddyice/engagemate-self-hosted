"""The single read-only transaction behind RunContextLoader."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
API_DIR = ROOT_DIR / "api"
sys.path.insert(0, str(API_DIR))

from engagemate.db.pool import create_pool
from engagemate.worker.run_context import RunContextError, RunContextRows
from engagemate.worker.run_context_reader import PostgresRunContextReader, learning_feedback

SCHEMA_SQL = """
CREATE TABLE users (
    id uuid PRIMARY KEY,
    plan text NOT NULL DEFAULT 'starter'
);

CREATE TABLE ig_accounts (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id),
    username text NOT NULL,
    login_country text,
    login_region text,
    login_city text,
    warmup_started_at timestamptz,
    health_status text NOT NULL DEFAULT 'active',
    paused_at timestamptz,
    pause_reason text,
    UNIQUE (id, user_id)
);

CREATE TABLE ig_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    encrypted_cookie_blob text NOT NULL,
    encryption_version integer NOT NULL DEFAULT 1,
    captured_user_agent text NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT now(),
    last_validated_at timestamptz,
    expires_at timestamptz
);

CREATE TABLE agent_configs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    timezone text NOT NULL DEFAULT 'UTC',
    schedule_hour integer NOT NULL DEFAULT 9,
    daily_like_limit integer NOT NULL DEFAULT 0,
    daily_reply_limit integer NOT NULL DEFAULT 0,
    daily_dm_limit integer NOT NULL DEFAULT 0,
    daily_view_limit integer NOT NULL DEFAULT 0,
    follow_after_engagement boolean NOT NULL DEFAULT false,
    follow_reel_message_enabled boolean NOT NULL DEFAULT false,
    growth_goal integer,
    persona text,
    banned_phrases text[] NOT NULL DEFAULT '{}'::text[],
    expected_language text NOT NULL DEFAULT 'en'
    ,version integer NOT NULL DEFAULT 1
    ,profile jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE brand_profiles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'complete',
    follower_audience_approved_at timestamptz
);

CREATE TABLE brand_reference_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_profile_id uuid NOT NULL REFERENCES brand_profiles(id),
    username text NOT NULL,
    profile_url text NOT NULL,
    position integer NOT NULL
);

CREATE TABLE targets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    username text NOT NULL,
    ig_user_id text,
    tier text NOT NULL,
    source text NOT NULL DEFAULT 'manual',
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE account_health_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    event_type text NOT NULL,
    severity text NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE story_engagements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    target_id uuid NOT NULL,
    story_fingerprint text NOT NULL,
    action text NOT NULL,
    generated_text text,
    model_used text,
    rationale text,
    confidence numeric(5, 4),
    status text NOT NULL,
    failure_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    executed_at timestamptz
);
"""

SCHEMA_SQL += """
CREATE TABLE hermes_execution_runs (
 id uuid PRIMARY KEY, user_id uuid NOT NULL, ig_account_id uuid NOT NULL
);
CREATE TABLE hermes_tool_calls (
 id uuid PRIMARY KEY, run_id uuid REFERENCES hermes_execution_runs(id),
 tool_name text, target_id uuid, story_fingerprint text, status text, started_at timestamptz
);
CREATE TABLE follow_reel_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL, ig_account_id uuid NOT NULL,
    status text NOT NULL, reserved_at timestamptz, attempted_at timestamptz
);
CREATE TABLE relationship_follows (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL,
 ig_account_id uuid NOT NULL, target_id uuid NOT NULL, owner_id text NOT NULL,
 prerequisite_engagement_id uuid NOT NULL, confirmed_follow_at timestamptz,
 unfollowed_at timestamptz, evidence jsonb NOT NULL DEFAULT '{}'
);
"""

NOW = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
SCHEMA_SQL += (ROOT_DIR / "app/src/db/migrations/0032_separate_message_quotas.sql").read_text()
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


@pytest.fixture(scope="module")
def database_url():
    if subprocess.run(["pg_isready"], capture_output=True, check=False).returncode != 0:
        pytest.skip("local PostgreSQL is unavailable")

    database = f"engagemate_reader_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    created = subprocess.run(
        ["createdb", database], capture_output=True, text=True, check=False
    )
    if created.returncode != 0:
        pytest.fail(f"could not create disposable database: {created.stderr}")

    url = f"postgresql://localhost/{database}"
    try:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(SCHEMA_SQL)
        yield url
    finally:
        subprocess.run(
            ["dropdb", "--if-exists", "--force", database],
            capture_output=True,
            check=False,
        )


@pytest.fixture()
def world(database_url):
    pool = create_pool(database_url, min_size=1, max_size=2)
    ids = {
        "user_id": uuid.uuid4(),
        "account_id": uuid.uuid4(),
        "other_user_id": uuid.uuid4(),
        "other_account_id": uuid.uuid4(),
        "target_a": uuid.uuid4(),
        "target_b": uuid.uuid4(),
    }
    try:
        with pool.connection() as connection:
            connection.execute(
                "TRUNCATE relationship_follows, account_health_events, brand_reference_accounts, "
                "brand_profiles, story_engagements, "
                "targets, agent_configs, ig_sessions, "
                "ig_accounts, users CASCADE"
            )
            connection.execute(
                "INSERT INTO users (id, plan) VALUES (%s, 'pro')", (ids["user_id"],)
            )
            connection.execute(
                "INSERT INTO users (id, plan) VALUES (%s, 'pro')",
                (ids["other_user_id"],),
            )
            for account_id, user_id in (
                (ids["account_id"], ids["user_id"]),
                (ids["other_account_id"], ids["other_user_id"]),
            ):
                connection.execute(
                    "INSERT INTO ig_accounts (id, user_id, username, login_country, "
                    "login_region, login_city, warmup_started_at) "
                    "VALUES (%s, %s, 'account.one', 'US', 'California', "
                    "'San Francisco', %s)",
                    (account_id, user_id, NOW - timedelta(days=60)),
                )
            connection.execute(
                "INSERT INTO ig_sessions (user_id, ig_account_id, "
                "encrypted_cookie_blob, captured_user_agent, captured_at, expires_at) "
                "VALUES (%s, %s, 'cipher-new', %s, %s, %s)",
                (
                    ids["user_id"],
                    ids["account_id"],
                    USER_AGENT,
                    NOW - timedelta(days=1),
                    NOW + timedelta(days=10),
                ),
            )
            connection.execute(
                "INSERT INTO agent_configs (user_id, ig_account_id, enabled, "
                "daily_like_limit, daily_reply_limit, daily_dm_limit, daily_view_limit, persona, "
                "banned_phrases, expected_language, version, profile) "
                "VALUES (%s, %s, true, 20, 5, 2, 50, 'A friendly founder.', "
                "%s, 'en', 7, '{\"tone\": \"warm\"}'::jsonb)",
                (ids["user_id"], ids["account_id"], ["buy now", "dm me"]),
            )
            profile_id = connection.execute(
                "INSERT INTO brand_profiles (user_id, ig_account_id) VALUES (%s, %s) RETURNING id",
                (ids["user_id"], ids["account_id"]),
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO brand_reference_accounts "
                "(brand_profile_id, username, profile_url, position) "
                "VALUES (%s, 'reference.one', 'https://instagram.com/reference.one', 0)",
                (profile_id,),
            )
            for target_id, username, tier, enabled in (
                (ids["target_a"], "target.one", "vip", True),
                (ids["target_b"], "target.two", "broad", False),
            ):
                connection.execute(
                    "INSERT INTO targets (id, user_id, ig_account_id, username, "
                    "ig_user_id, tier, enabled) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        target_id,
                        ids["user_id"],
                        ids["account_id"],
                        username,
                        f"owner-{username}",
                        tier,
                        enabled,
                    ),
                )
        ids["pool"] = pool
        ids["reader"] = PostgresRunContextReader(pool, clock=lambda: NOW)
        yield ids
    finally:
        pool.close()


def insert_engagement(pool, world, **overrides):
    values = {
        "user_id": world["user_id"],
        "ig_account_id": world["account_id"],
        "target_id": world["target_a"],
        "story_fingerprint": "a" * 64,
        "action": "reply",
        "generated_text": "nice shot",
        "model_used": "claude-sonnet-4-6",
        "status": "sent",
        "created_at": NOW,
    }
    values.update(overrides)
    values.setdefault("executed_at", values["created_at"])
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO story_engagements (user_id, ig_account_id, target_id, "
            "story_fingerprint, action, generated_text, model_used, status, "
            "created_at, executed_at) VALUES (%(user_id)s, %(ig_account_id)s, %(target_id)s, "
            "%(story_fingerprint)s, %(action)s, %(generated_text)s, %(model_used)s, "
            "%(status)s, %(created_at)s, %(executed_at)s)",
            values,
        )


async def test_read_returns_every_row_for_the_tenant(world):
    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert type(rows) is RunContextRows
    assert rows.account.user_id == world["user_id"]
    assert rows.account.ig_account_id == world["account_id"]
    assert rows.account.username == "account.one"
    assert rows.account.health == "active"
    assert rows.account.plan == "pro"
    assert rows.account.geo_country == "US"
    assert rows.account.geo_region == "California"
    assert rows.account.geo_city == "San Francisco"
    assert rows.account.persona_text == "A friendly founder."
    assert rows.account.daily_like_limit == 20
    assert rows.session.ciphertext == "cipher-new"
    assert rows.account.config_version == 7
    assert rows.account.config_snapshot["version"] == 7
    assert rows.account.config_snapshot["profile"] == {"tone": "warm"}
    assert rows.account.config_snapshot["targets"] == [
        {
            "id": str(world["target_a"]),
            "username": "target.one",
            "tier": "vip",
            "source": "manual",
            "enabled": True,
        }
    ]
    assert rows.account.config_snapshot["source_accounts"] == [
        {"username": "reference.one", "position": 0}
    ]
    assert rows.session.captured_user_agent == USER_AGENT


async def test_source_accounts_are_scoped_to_the_running_account(world):
    second_account_id = uuid.uuid4()
    with world["pool"].connection() as connection:
        connection.execute(
            """
            INSERT INTO ig_accounts (
                id, user_id, username, login_country, login_region, login_city,
                warmup_started_at
            ) VALUES (%s, %s, 'account.two', 'US', 'California', 'Oakland', %s)
            """,
            (
                second_account_id,
                world["user_id"],
                NOW - timedelta(days=60),
            ),
        )
        second_profile_id = connection.execute(
            """
            INSERT INTO brand_profiles (user_id, ig_account_id)
            VALUES (%s, %s)
            RETURNING id
            """,
            (world["user_id"], second_account_id),
        ).fetchone()["id"]
        connection.execute(
            """
            INSERT INTO brand_reference_accounts (
                brand_profile_id, username, profile_url, position
            ) VALUES (
                %s, 'reference.two', 'https://instagram.com/reference.two', 0
            )
            """,
            (second_profile_id,),
        )

    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert rows.account.config_snapshot["source_accounts"] == [
        {"username": "reference.one", "position": 0}
    ]


async def test_gate_configuration_is_read_from_the_account(world):
    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert rows.account.banned_phrases == ("buy now", "dm me")
    assert rows.account.expected_language == "en"


async def test_follow_reel_opt_in_reaches_the_budget_context(world):
    with world['pool'].connection() as connection:
        connection.execute('UPDATE agent_configs SET follow_after_engagement=true, follow_reel_message_enabled=true WHERE ig_account_id=%s', (world['account_id'],))
    rows = await world['reader'].read(world['user_id'],world['account_id'])
    assert rows.account.follow_after_engagement is True
    assert rows.account.follow_reel_message_enabled is True
    assert rows.account.config_snapshot['follow_reel_message_enabled'] is True


async def test_only_enabled_targets_are_returned(world):
    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert len(rows.targets) == 1
    assert rows.targets[0].id == world["target_a"]
    assert rows.targets[0].handle == "target.one"
    assert rows.targets[0].owner_id == "owner-target.one"
    assert rows.targets[0].tier == "vip"


async def test_a_target_without_an_owner_id_is_excluded(world):
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE targets SET ig_user_id = NULL WHERE id = %s", (world["target_a"],)
        )

    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert rows.targets == ()


async def test_the_newest_session_wins(world):
    with world["pool"].connection() as connection:
        connection.execute(
            "INSERT INTO ig_sessions (user_id, ig_account_id, encrypted_cookie_blob, "
            "captured_user_agent, captured_at) VALUES (%s, %s, 'cipher-newer', %s, %s)",
            (
                world["user_id"],
                world["account_id"],
                USER_AGENT,
                NOW - timedelta(hours=1),
            ),
        )

    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert rows.session.ciphertext == "cipher-newer"


async def test_a_missing_account_fails_closed(world):
    with pytest.raises(RunContextError) as error:
        await world["reader"].read(world["user_id"], uuid.uuid4())

    assert error.value.reason == "account_missing"


async def test_another_tenants_account_is_never_readable(world):
    with pytest.raises(RunContextError) as error:
        await world["reader"].read(world["user_id"], world["other_account_id"])

    assert error.value.reason == "account_missing"


async def test_a_missing_session_fails_closed(world):
    with world["pool"].connection() as connection:
        connection.execute(
            "DELETE FROM ig_sessions WHERE ig_account_id = %s", (world["account_id"],)
        )

    with pytest.raises(RunContextError) as error:
        await world["reader"].read(world["user_id"], world["account_id"])

    assert error.value.reason == "session_missing"


async def test_a_missing_agent_config_fails_closed(world):
    with world["pool"].connection() as connection:
        connection.execute(
            "DELETE FROM agent_configs WHERE ig_account_id = %s",
            (world["account_id"],),
        )

    with pytest.raises(RunContextError) as error:
        await world["reader"].read(world["user_id"], world["account_id"])

    assert error.value.reason == "config_missing"


async def test_a_disabled_agent_stops_the_run(world):
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE agent_configs SET enabled = false WHERE ig_account_id = %s",
            (world["account_id"],),
        )

    with pytest.raises(RunContextError) as error:
        await world["reader"].read(world["user_id"], world["account_id"])

    assert error.value.reason == "agent_disabled"


async def test_usage_counts_only_todays_sent_actions(world):
    insert_engagement(world["pool"], world, action="like", created_at=NOW)
    insert_engagement(world["pool"], world, action="like", created_at=NOW)
    insert_engagement(
        world["pool"], world, action="like", created_at=NOW - timedelta(days=2)
    )
    insert_engagement(world["pool"], world, action="dm", status="failed")

    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert rows.usage.likes == 2
    assert rows.usage.dms == 0


async def test_usage_ignores_other_tenants(world):
    insert_engagement(
        world["pool"],
        world,
        user_id=world["other_user_id"],
        ig_account_id=world["other_account_id"],
        action="like",
    )

    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert rows.usage.likes == 0


async def test_recent_sent_messages_feed_the_duplicate_check(world):
    insert_engagement(world["pool"], world, generated_text="first message")
    insert_engagement(world["pool"], world, generated_text="second message")

    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert set(rows.history.sent_messages) == {"first message", "second message"}


async def test_likes_contribute_no_message_history(world):
    insert_engagement(world["pool"], world, action="like", generated_text=None)

    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert rows.history.sent_messages == ()


async def test_history_summary_is_never_empty(world):
    """IntelligenceHistory rejects a blank summary, so the reader must not emit one."""

    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert rows.history.recent_interaction_summary.strip() != ""


async def test_the_result_loads_through_run_context_loader(world):
    """The reader's output must satisfy RunContextLoader end to end."""

    from engagemate.browser.fingerprint import (
        FingerprintFactory,
        FingerprintGeoRegistry,
    )
    from engagemate.domain.budgets import ActionBudget
    from engagemate.domain.models import ClaimedJob, JobStatus
    from engagemate.proxy.base import Geo, ProxyEndpoint
    from engagemate.worker.run_context import RunContextLoader

    geo = Geo("US", "California", "San Francisco")
    endpoint = ProxyEndpoint(
        provider="iproyal",
        provider_reference="lease-1",
        proxy_type="isp",
        geo=geo,
        host="proxy.example.com",
        port=8080,
        username="user",
        password="secret",
    )

    class Cipher:
        def decrypt(self, token):
            return {"cookies": [{"name": "sessionid", "value": "x"}]}

    class Bindings:
        def ensure_binding(self, user_id, ig_account_id, requested, *, job_id=None):
            return endpoint

    loader = RunContextLoader(
        reader=world["reader"],
        cipher_for=lambda ig_account_id: Cipher(),
        fingerprints=FingerprintFactory(
            "s" * 64,
            FingerprintGeoRegistry.from_json(
                '[{"country": "US", "region": "California", '
                '"city": "San Francisco", "timezone": "America/Los_Angeles", '
                '"locale": "en-US"}]'
            ),
        ),
        proxy_bindings=Bindings(),
        clock=lambda: NOW,
    )
    job = ClaimedJob(
        id=uuid.uuid4(),
        user_id=world["user_id"],
        ig_account_id=world["account_id"],
        schedule_key="2026-07-31T08:00",
        scheduled_for=NOW,
        status=JobStatus.RUNNING,
        claimed_by="worker-1",
        lease_expires_at=NOW + timedelta(seconds=120),
        heartbeat_at=NOW,
        attempts=1,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
    )

    context = await loader.load(job)

    assert context.username == "account.one"
    assert context.geo == geo
    assert context.budget == ActionBudget(
        20, 5, 0, plan_total=20, views=50, private_messages=5
    )
    assert context.banned_phrases == ("buy now", "dm me")
    assert len(context.targets) == 1


def insert_health_event(pool, world, event_type, severity, created_at, **overrides):
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO account_health_events (user_id, ig_account_id, event_type, "
            "severity, evidence, created_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, %s)",
            (
                overrides.get("user_id", world["user_id"]),
                overrides.get("ig_account_id", world["account_id"]),
                event_type,
                severity,
                created_at,
            ),
        )


async def test_only_the_newest_qualifying_reconnect_is_read(world):
    insert_health_event(
        world["pool"], world, "rate_limit", "warning", NOW - timedelta(days=9)
    )
    insert_health_event(
        world["pool"], world, "warmup_restart", "info", NOW - timedelta(days=2)
    )

    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert rows.account.last_restricted_at == NOW - timedelta(days=2)


async def test_an_account_never_restricted_reports_no_setback(world):
    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert rows.account.last_restricted_at is None


async def test_recent_login_required_does_not_reset_warmup(world):
    insert_health_event(
        world["pool"], world, "login_required", "warning", NOW - timedelta(hours=1)
    )
    rows = await world["reader"].read(world["user_id"], world["account_id"])
    assert rows.account.last_restricted_at is None


async def test_our_own_proxy_faults_never_set_warm_up_back(world):
    """A bad proxy is our infrastructure failing, not Instagram objecting.

    Both land in account_health_events at severity 'warning', so filtering on
    severity would charge the customer's ramp for our outage.
    """

    insert_health_event(
        world["pool"], world, "proxy_health_failed", "warning", NOW - timedelta(hours=1)
    )
    insert_health_event(
        world["pool"], world, "proxy_rebound", "info", NOW - timedelta(hours=1)
    )

    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert rows.account.last_restricted_at is None


async def test_another_tenants_restriction_never_sets_this_account_back(world):
    insert_health_event(
        world["pool"],
        world,
        "warmup_restart",
        "info",
        NOW - timedelta(hours=1),
        user_id=world["other_user_id"],
        ig_account_id=world["other_account_id"],
    )

    rows = await world["reader"].read(world["user_id"], world["account_id"])

    assert rows.account.last_restricted_at is None


async def test_a_qualifying_reconnect_collapses_the_budget_to_the_first_bracket(world):
    before = await world["reader"].read(world["user_id"], world["account_id"])
    insert_health_event(
        world["pool"], world, "warmup_restart", "info", NOW - timedelta(hours=3)
    )
    after = await world["reader"].read(world["user_id"], world["account_id"])

    from engagemate.domain.budgets import (
        AccountBudgetState,
        AgentConfigLimits,
        BudgetPolicy,
        DailyActionUsage,
    )

    def budget(account):
        return BudgetPolicy.remaining(
            AccountBudgetState(account.warmup_started_at, account.last_restricted_at),
            AgentConfigLimits(
                account.plan,
                account.daily_like_limit,
                account.daily_reply_limit,
                account.daily_dm_limit,
                account.daily_view_limit,
            ),
            DailyActionUsage(0, 0, 0, 0),
            NOW,
        )

    assert budget(before.account).likes == 20
    assert budget(after.account).likes == 3
    assert budget(after.account).private_messages == 0


async def test_follower_sources_require_current_approval(world):
    rows = await world["reader"].read(world["user_id"], world["account_id"])
    assert rows.account.config_snapshot["follower_sources"] == []
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE brand_profiles SET follower_audience_approved_at = now() WHERE ig_account_id = %s",
            (world["account_id"],),
        )
        source_id = connection.execute(
            "SELECT source.id FROM brand_reference_accounts source JOIN brand_profiles p ON p.id = source.brand_profile_id WHERE p.ig_account_id = %s",
            (world["account_id"],),
        ).fetchone()["id"]
    rows = await world["reader"].read(world["user_id"], world["account_id"])
    assert rows.account.config_snapshot["follower_sources"] == [{"id": str(source_id), "username": "reference.one"}]
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE brand_profiles SET follower_audience_approved_at = NULL WHERE ig_account_id = %s",
            (world["account_id"],),
        )
    rows = await world["reader"].read(world["user_id"], world["account_id"])
    assert rows.account.config_snapshot["follower_sources"] == []


async def test_dynamic_followers_never_become_fixed_targets(world):
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE targets SET source = 'source-followers' WHERE ig_account_id = %s",
            (world["account_id"],),
        )
    rows = await world["reader"].read(world["user_id"], world["account_id"])
    assert rows.targets == ()
    assert rows.account.config_snapshot["targets"] == []
    assert rows.account.config_snapshot["follower_sources"] == []


async def test_usage_uses_execution_time_and_explicit_utc_day(world):
    insert_engagement(world["pool"], world, action="like", created_at=NOW-timedelta(days=2), executed_at=NOW)
    for executed in (None, NOW+timedelta(seconds=1), NOW-timedelta(days=1)):
        insert_engagement(world["pool"], world, action="like", executed_at=executed)
    rows = await world["reader"].read(world["user_id"], world["account_id"])
    assert rows.usage.likes == 1


async def test_usage_resets_at_the_account_pacific_midnight(world):
    with world["pool"].connection() as connection:
        connection.execute("UPDATE agent_configs SET timezone='America/Los_Angeles',daily_reel_message_limit=3,warmup_exempt=true")
        for at in (NOW.replace(hour=6), NOW.replace(hour=7)):
            connection.execute("INSERT INTO follow_reel_messages(user_id,ig_account_id,status,reserved_at) VALUES (%s,%s,'uncertain',%s)",
                               (world["user_id"],world["account_id"],at))
    insert_engagement(world["pool"], world, action="reply", executed_at=NOW.replace(hour=6))
    insert_engagement(world["pool"], world, action="reply", executed_at=NOW.replace(hour=7))
    rows = await world["reader"].read(world["user_id"], world["account_id"])
    assert rows.usage.replies == rows.usage.reel_messages == rows.usage.dms == 1
    assert rows.account.daily_reel_message_limit == 3
    assert rows.account.warmup_exempt is True


def feedback_row(owner="1", action="like", **changes):
    row = {"owner_id": owner, "action": action, "confirmed_follow_at": NOW-timedelta(days=5),
           "unfollowed_at": NOW-timedelta(hours=2),
           "evidence": {"follow": {"outcome": "confirmed", "followed_by": False},
                        "reconciliation": {"owner_id": owner, "identity_matches": True,
                                           "followed_by": True, "observed_at": NOW.isoformat()}}}
    row.update(changes)
    return row


def test_feedback_preserves_positive_and_deduplicates_owners():
    row = feedback_row()
    row["evidence"]["first_follow_back"] = {**row["evidence"]["reconciliation"], "observed_at": (NOW-timedelta(days=2)).isoformat()}
    row["evidence"]["reconciliation"]["followed_by"] = False
    result = learning_feedback([row, row], NOW)
    assert result["groups"]["like"] == {"mature": 1, "checked": 1, "follow_backs": 1, "unknown": 0}
    assert result["status"] == "insufficient_evidence"
    assert result["comment_outcomes"] is None


@pytest.mark.parametrize("patch", [
    {"observed_at": "invalid"}, {"observed_at": (NOW+timedelta(seconds=1)).isoformat()},
    {"observed_at": (NOW-timedelta(days=6)).isoformat()}, {"owner_id": "999"},
    {"identity_matches": "true"}, {"followed_by": "true"},
    {"followed_by": False, "observed_at": (NOW-timedelta(days=2)).isoformat()},
])
def test_feedback_unknown_observations_do_not_become_negative_or_positive(patch):
    row = feedback_row()
    row["evidence"]["reconciliation"].update(patch)
    assert learning_feedback([row], NOW)["groups"]["like"]["unknown"] == 1


def test_feedback_maturity_baseline_and_comparison_threshold():
    rows = [feedback_row(str(i), "like" if i < 20 else "reply") for i in range(40)]
    for row in rows[:20]:
        row["evidence"]["reconciliation"]["followed_by"] = False
    result = learning_feedback(rows, NOW)
    assert result["status"] == "observational_comparison"
    assert result["preferred_action"] == "reply"
    assert learning_feedback(rows[:-1], NOW)["preferred_action"] is None
    row = feedback_row(confirmed_follow_at=NOW-timedelta(hours=1))
    assert learning_feedback([row], NOW)["groups"]["like"]["mature"] == 0
    row = feedback_row()
    row["evidence"]["follow"]["followed_by"] = None
    assert learning_feedback([row], NOW)["groups"]["like"]["mature"] == 0


async def test_feedback_scopes_both_relationship_and_prerequisite_receipt(world):
    from psycopg.types.json import Jsonb
    with world["pool"].connection() as connection:
        for user, account, receipt_user, receipt_account in (
            (world["user_id"], world["account_id"], world["user_id"], world["account_id"]),
            (world["other_user_id"], world["other_account_id"], world["other_user_id"], world["other_account_id"]),
            (world["user_id"], world["other_account_id"], world["user_id"], world["other_account_id"]),
            (world["user_id"], world["account_id"], world["other_user_id"], world["account_id"]),
        ):
            receipt = connection.execute("INSERT INTO story_engagements (user_id,ig_account_id,target_id,story_fingerprint,action,status,executed_at) VALUES (%s,%s,%s,'x','like','sent',%s) RETURNING id", (receipt_user,receipt_account,world["target_a"],NOW-timedelta(days=6))).fetchone()["id"]
            row = feedback_row(str(uuid.uuid4()))
            connection.execute("INSERT INTO relationship_follows (user_id,ig_account_id,target_id,owner_id,prerequisite_engagement_id,confirmed_follow_at,evidence) VALUES (%s,%s,%s,%s,%s,%s,%s)", (user,account,world["target_a"],row["owner_id"],receipt,row["confirmed_follow_at"],Jsonb(row["evidence"])))
    rows = await world["reader"].read(world["user_id"], world["account_id"])
    assert rows.account.config_snapshot["learning_feedback"]["groups"]["like"]["follow_backs"] == 1


def test_legacy_observation_requires_identity_owner_and_valid_timestamp():
    row = feedback_row()
    row["evidence"]["unfollow"] = row["evidence"].pop("reconciliation")
    assert learning_feedback([row], NOW)["groups"]["like"]["follow_backs"] == 1
    row["unfollowed_at"] = NOW+timedelta(seconds=1)
    assert learning_feedback([row], NOW)["groups"]["like"]["unknown"] == 1


def test_feedback_rejects_malformed_evidence_and_out_of_window_records():
    rows = [feedback_row(evidence=None), feedback_row(evidence=[]),
            feedback_row(confirmed_follow_at=NOW-timedelta(days=31)),
            feedback_row(confirmed_follow_at=NOW+timedelta(days=1))]
    assert learning_feedback(rows, NOW)["groups"]["like"]["mature"] == 0


def test_conflicting_owner_cohorts_and_truncation_do_not_support_ranking():
    rows = [feedback_row(str(i), "like" if i < 20 else "reply") for i in range(40)]
    assert learning_feedback(rows+[feedback_row("0", "reply")], NOW)["status"] == "insufficient_evidence"
    assert learning_feedback(rows+[feedback_row("0")]*5000, NOW)["status"] == "insufficient_evidence"
