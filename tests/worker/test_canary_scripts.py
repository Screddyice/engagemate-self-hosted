"""The canary operator scripts, against a really migrated database.

These run the project's own migrations rather than a hand-written schema, so a
constraint the scripts depend on cannot pass here and fail in production.
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from engagemate.db.pool import create_pool

ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "app" / "src" / "db" / "migrations"
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64
TEXT = "congrats on the launch"
TEXT_HASH = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
COOKIE = "sessionid=super-secret-cookie-value"
PROXY_PASSWORD = "proxy-password-should-not-leak"


def _load(name: str):
    """Import an operator script by path.

    They live in scripts/ rather than the package because they are run by a
    human against production, not imported by the worker. dataclasses resolves
    annotations through sys.modules, so the module has to be registered there
    before it executes.
    """

    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prepare = _load("prepare_instagram_canary")
evidence = _load("verify_execution_evidence")


@pytest.fixture(scope="module")
def database_url():
    if subprocess.run(["pg_isready"], capture_output=True, check=False).returncode != 0:
        pytest.skip("PostgreSQL is unavailable")
    database = f"engagemate_canary_{uuid.uuid4().hex[:12]}"
    created = subprocess.run(["createdb", database], capture_output=True, check=False)
    if created.returncode != 0:
        pytest.skip("cannot create a test database")
    url = f"postgresql:///{database}"
    try:
        import psycopg

        with psycopg.connect(url, autocommit=True) as connection:
            for migration in sorted(MIGRATIONS.glob("[0-9]*.sql")):
                for statement in migration.read_text().split("--> statement-breakpoint"):
                    if statement.strip():
                        connection.execute(statement)
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
        "target_id": uuid.uuid4(),
        "other_user_id": uuid.uuid4(),
        "other_account_id": uuid.uuid4(),
        "other_target_id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "account_username": "canary.account",
    }
    try:
        with pool.connection() as connection:
            connection.execute(
                "TRUNCATE users, ig_accounts, targets, jobs, "
                "hermes_execution_runs, hermes_tool_calls, live_action_approvals, "
                "story_engagements, engagement_usage_events, run_summaries, "
                "account_health_events, agent_configs CASCADE"
            )
            for user_id in (ids["user_id"], ids["other_user_id"]):
                connection.execute(
                    "INSERT INTO users (id, owner_key, email, plan) "
                    "VALUES (%s, %s, %s, 'pro')",
                    (user_id, f"clerk-{user_id}", f"{user_id}@example.test"),
                )
            for account_id, user_id, username in (
                (ids["account_id"], ids["user_id"], ids["account_username"]),
                (ids["other_account_id"], ids["other_user_id"], "other.account"),
            ):
                connection.execute(
                    "INSERT INTO ig_accounts (id, user_id, username, "
                    "warmup_started_at, login_country, login_region, login_city) "
                    "VALUES (%s, %s, %s, %s, 'US', 'California', 'San Francisco')",
                    (account_id, user_id, username, NOW - timedelta(days=90)),
                )
                connection.execute(
                    "INSERT INTO agent_configs (user_id, ig_account_id, enabled, "
                    "daily_like_limit, daily_reply_limit, daily_dm_limit, "
                    "daily_view_limit, persona) "
                    "VALUES (%s, %s, true, 20, 5, 2, 1000, 'A friendly founder.')",
                    (user_id, account_id),
                )
            for target_id, user_id, account_id, username in (
                (ids["target_id"], ids["user_id"], ids["account_id"], "target.one"),
                (
                    ids["other_target_id"],
                    ids["other_user_id"],
                    ids["other_account_id"],
                    "target.two",
                ),
            ):
                connection.execute(
                    "INSERT INTO targets (id, user_id, ig_account_id, username, "
                    "ig_user_id, tier, source) "
                    "VALUES (%s, %s, %s, %s, %s, 'vip', 'manual')",
                    (target_id, user_id, account_id, username, f"ig-{username}"),
                )
            connection.execute(
                "INSERT INTO jobs (id, user_id, ig_account_id, schedule_key, "
                "scheduled_for, status) VALUES (%s, %s, %s, %s, %s, 'running')",
                (
                    ids["job_id"],
                    ids["user_id"],
                    ids["account_id"],
                    f"canary-{ids['job_id']}",
                    NOW,
                ),
            )
            connection.execute(
                "INSERT INTO hermes_execution_runs (id, job_id, user_id, "
                "ig_account_id, hermes_session_id, config_version, "
                "config_snapshot, status) "
                "VALUES (%s, %s, %s, %s, %s, 7, '{}'::jsonb, 'running')",
                (
                    ids["run_id"],
                    ids["job_id"],
                    ids["user_id"],
                    ids["account_id"],
                    f"session-{ids['run_id']}",
                ),
            )
        ids["pool"] = pool
        yield ids
    finally:
        pool.close()


def denied_call(world, *, action="reply", text=TEXT, target_id=None, key=None):
    """Record the attempt canary mode denies for lack of an approval."""

    call_id = uuid.uuid4()
    with world["pool"].connection() as connection:
        connection.execute(
            "INSERT INTO hermes_tool_calls (id, run_id, tool_name, "
            "opportunity_id, target_id, story_fingerprint, generated_text_hash, "
            "proposed_text, idempotency_key, status, failure_class, started_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'denied', "
            "'canary_approval_required', %s)",
            (
                call_id,
                world["run_id"],
                action,
                uuid.uuid4(),
                target_id or world["target_id"],
                FINGERPRINT,
                hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
                text,
                key or f"canary-{call_id.hex}",
                NOW - timedelta(minutes=1),
            ),
        )
    return call_id


# --- preview -----------------------------------------------------------------


def test_preview_reports_the_action_awaiting_approval(world):
    denied_call(world)

    seen = prepare.preview(
        world["pool"], account_username=world["account_username"], now=NOW
    )

    assert seen.action == "reply"
    assert seen.target_username == "target.one"
    assert seen.proposed_text == TEXT
    assert seen.fingerprint_prefix == FINGERPRINT[:12]
    assert seen.remaining_private_messages == 5


def test_preview_prints_no_secret_that_shares_the_database(world):
    """Redaction has to hold when the same database holds the secrets."""

    denied_call(world)
    with world["pool"].connection() as connection:
        connection.execute(
            "INSERT INTO ig_sessions (user_id, ig_account_id, "
            "encrypted_cookie_blob, encryption_version, captured_user_agent, "
            "captured_at, expires_at) VALUES (%s, %s, %s, 1, %s, %s, %s)",
            (
                world["user_id"],
                world["account_id"],
                COOKIE,
                f"Mozilla/5.0 {PROXY_PASSWORD}",
                NOW - timedelta(days=1),
                NOW + timedelta(days=10),
            ),
        )

    rendered = prepare.render_preview(
        prepare.preview(
            world["pool"], account_username=world["account_username"], now=NOW
        )
    )

    assert COOKIE not in rendered
    assert PROXY_PASSWORD not in rendered
    assert FINGERPRINT not in rendered  # only the prefix
    assert "target.one" in rendered


def test_preview_refuses_when_nothing_awaits_approval(world):
    with pytest.raises(prepare.CanaryError, match="no approval-pending"):
        prepare.preview(
            world["pool"], account_username=world["account_username"], now=NOW
        )


def test_preview_refuses_an_account_that_is_no_longer_healthy(world):
    denied_call(world)
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE ig_accounts SET health_status = 'challenged' WHERE id = %s",
            (world["account_id"],),
        )

    with pytest.raises(prepare.CanaryError, match="not active"):
        prepare.preview(
            world["pool"], account_username=world["account_username"], now=NOW
        )


def test_preview_refuses_a_message_action_with_no_recorded_text(world):
    denied_call(world, action="dm", text=None)

    with pytest.raises(prepare.CanaryError, match="no proposed text"):
        prepare.preview(
            world["pool"], account_username=world["account_username"], now=NOW
        )


# --- approve -----------------------------------------------------------------


def _decoded(row):
    """A SQL_ASCII development cluster hands back bytes; CI runs UTF8."""

    return {
        key: value.decode("utf-8") if isinstance(value, bytes) else value
        for key, value in row.items()
    }


def _approval_rows(world):
    with world["pool"].connection() as connection:
        return [
            _decoded(row)
            for row in connection.execute(
                "SELECT * FROM live_action_approvals ORDER BY approved_at"
            ).fetchall()
        ]


def test_approve_writes_one_single_use_grant_matching_the_attempt(world):
    call_id = denied_call(world)

    prepare.approve(
        world["pool"],
        preview_id=call_id,
        approved_by="operator",
        confirmation="APPROVE\n",
        now=NOW,
    )

    row = _approval_rows(world)[0]
    assert row["action"] == "reply"
    assert row["target_id"] == world["target_id"]
    assert row["story_fingerprint"] == FINGERPRINT
    assert row["generated_text_hash"] == TEXT_HASH
    assert row["consumed_at"] is None
    assert row["expires_at"] == NOW + timedelta(minutes=15)


def test_the_grant_carries_the_hash_the_worker_will_match_on(world):
    """begin_mutation matches on the text hash, so a different message fails."""

    call_id = denied_call(world)
    prepare.approve(
        world["pool"],
        preview_id=call_id,
        approved_by="operator",
        confirmation="APPROVE",
        now=NOW,
    )

    row = _approval_rows(world)[0]
    other = hashlib.sha256(b"a completely different message").hexdigest()
    assert row["generated_text_hash"] == TEXT_HASH
    assert row["generated_text_hash"] != other


@pytest.mark.parametrize(
    "confirmation", ["approve", "APPROVED", "APPROVE ", "", "yes", "APPROVE\nAPPROVE"]
)
def test_approve_refuses_anything_but_the_exact_word(world, confirmation):
    call_id = denied_call(world)

    with pytest.raises(prepare.CanaryError, match="exactly APPROVE"):
        prepare.approve(
            world["pool"],
            preview_id=call_id,
            approved_by="operator",
            confirmation=confirmation,
            now=NOW,
        )
    assert _approval_rows(world) == []


@pytest.mark.parametrize("approver", ["", "someone", "OPERATOR", "operator "])
def test_approve_refuses_an_unnamed_approver(world, approver):
    call_id = denied_call(world)

    with pytest.raises(prepare.CanaryError, match="not permitted"):
        prepare.approve(
            world["pool"],
            preview_id=call_id,
            approved_by=approver,
            confirmation="APPROVE",
            now=NOW,
        )
    assert _approval_rows(world) == []


def test_approve_refuses_a_second_grant_while_one_is_live(world):
    call_id = denied_call(world)
    prepare.approve(
        world["pool"],
        preview_id=call_id,
        approved_by="operator",
        confirmation="APPROVE",
        now=NOW,
    )

    with pytest.raises(prepare.CanaryError, match="already exists"):
        prepare.approve(
            world["pool"],
            preview_id=call_id,
            approved_by="operator",
            confirmation="APPROVE",
            now=NOW + timedelta(minutes=1),
        )
    assert len(_approval_rows(world)) == 1


def test_a_grant_that_has_expired_does_not_block_a_fresh_one(world):
    call_id = denied_call(world)
    prepare.approve(
        world["pool"],
        preview_id=call_id,
        approved_by="operator",
        confirmation="APPROVE",
        now=NOW,
    )

    prepare.approve(
        world["pool"],
        preview_id=call_id,
        approved_by="operator",
        confirmation="APPROVE",
        now=NOW + timedelta(minutes=16),
    )

    assert len(_approval_rows(world)) == 2


def test_approve_refuses_a_target_belonging_to_another_tenant(world):
    call_id = denied_call(world, target_id=world["other_target_id"])

    with pytest.raises(prepare.CanaryError, match="does not belong"):
        prepare.approve(
            world["pool"],
            preview_id=call_id,
            approved_by="operator",
            confirmation="APPROVE",
            now=NOW,
        )
    assert _approval_rows(world) == []


def test_approve_refuses_a_call_that_was_never_denied_for_approval(world):
    call_id = denied_call(world)
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE hermes_tool_calls SET status = 'confirmed', "
            "failure_class = NULL WHERE id = %s",
            (call_id,),
        )

    with pytest.raises(prepare.CanaryError, match="not an approval-pending"):
        prepare.approve(
            world["pool"],
            preview_id=call_id,
            approved_by="operator",
            confirmation="APPROVE",
            now=NOW,
        )


def test_approve_refuses_an_unknown_preview(world):
    with pytest.raises(prepare.CanaryError, match="unknown"):
        prepare.approve(
            world["pool"],
            preview_id=uuid.uuid4(),
            approved_by="operator",
            confirmation="APPROVE",
            now=NOW,
        )


# --- evidence ----------------------------------------------------------------


def confirmed_action(world, *, action="like", key="canary-confirmed-0001"):
    """Write the full chain one clean canary leaves behind."""

    engagement_id = uuid.uuid4()
    with world["pool"].connection() as connection:
        connection.execute(
            "INSERT INTO hermes_tool_calls (run_id, tool_name, opportunity_id, "
            "target_id, story_fingerprint, idempotency_key, status, started_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'confirmed', %s)",
            (
                world["run_id"],
                action,
                uuid.uuid4(),
                world["target_id"],
                FINGERPRINT,
                key,
                NOW - timedelta(minutes=5),
            ),
        )
        connection.execute(
            "INSERT INTO story_engagements (id, user_id, ig_account_id, target_id, "
            "story_fingerprint, action, status, created_at, executed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'sent', %s, %s)",
            (
                engagement_id,
                world["user_id"],
                world["account_id"],
                world["target_id"],
                FINGERPRINT,
                action,
                NOW - timedelta(minutes=4),
                NOW - timedelta(minutes=4),
            ),
        )
        if action != "view":
            connection.execute(
                "INSERT INTO engagement_usage_events (user_id, ig_account_id, "
                "engagement_id, action, status, finalized_at, occurred_at) "
                "VALUES (%s, %s, %s, %s, 'billable', %s, %s)",
                (
                    world["user_id"],
                    world["account_id"],
                    engagement_id,
                    action,
                    NOW - timedelta(minutes=3),
                    NOW - timedelta(minutes=4),
                ),
            )
        connection.execute(
            "INSERT INTO run_summaries "
            "(user_id, ig_account_id, job_id, likes_sent, replies_sent, dms_sent) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (world["user_id"], world["account_id"], world["job_id"],
             int(action == "like"), int(action == "reply"), int(action == "dm")),
        )
    return key, engagement_id


@pytest.mark.parametrize("action", ["like", "reply", "dm"])
def test_a_clean_canary_verifies(world, action):
    key, engagement_id = confirmed_action(world, action=action)

    result = evidence.verify(
        world["pool"],
        run_id=world["run_id"],
        idempotency_key=key,
        instagram_confirmed=True,
        now=NOW,
    )

    assert result.action == action
    assert result.engagement_id == engagement_id
    assert result.usage_status == "billable"
    assert result.health_status == "active"


def test_a_view_verifies_without_a_usage_event(world):
    key, _ = confirmed_action(world, action="view", key="canary-view-0001")

    result = evidence.verify(
        world["pool"],
        run_id=world["run_id"],
        idempotency_key=key,
        instagram_confirmed=True,
        now=NOW,
    )

    assert result.usage_status is None


def test_a_view_that_billed_someone_fails(world):
    key, engagement_id = confirmed_action(world, action="view", key="canary-view-0002")
    with world["pool"].connection() as connection:
        connection.execute(
            "INSERT INTO engagement_usage_events (user_id, ig_account_id, "
            "engagement_id, action, status, finalized_at) "
            "VALUES (%s, %s, %s, 'like', 'billable', %s)",
            (world["user_id"], world["account_id"], engagement_id, NOW),
        )

    with pytest.raises(evidence.EvidenceError, match="must not"):
        evidence.verify(
            world["pool"],
            run_id=world["run_id"],
            idempotency_key=key,
            instagram_confirmed=True,
            now=NOW,
        )


def test_evidence_refuses_without_an_operator_seeing_instagram(world):
    key, _ = confirmed_action(world)

    with pytest.raises(evidence.EvidenceError, match="visible on Instagram"):
        evidence.verify(
            world["pool"],
            run_id=world["run_id"],
            idempotency_key=key,
            instagram_confirmed=False,
            now=NOW,
        )


def test_the_database_itself_forbids_a_duplicate_engagement(world):
    """The real protection against a double action is an index, not a check.

    story_engagements_dedupe_idx makes a second engagement on the same Story
    and action impossible, so the verifier's own duplicate check can never
    fire while that index exists. This pins the index rather than the check.
    """

    import psycopg

    confirmed_action(world)

    with (
        pytest.raises(psycopg.errors.UniqueViolation),
        world["pool"].connection() as connection,
    ):
        connection.execute(
            "INSERT INTO story_engagements (user_id, ig_account_id, "
            "target_id, story_fingerprint, action, status) "
            "VALUES (%s, %s, %s, %s, 'like', 'sent')",
            (
                world["user_id"],
                world["account_id"],
                world["target_id"],
                FINGERPRINT,
            ),
        )


@pytest.mark.parametrize(
    ("rows", "message"),
    [([], "no widgets"), ([1, 2], "2 widgets where exactly one is required")],
)
def test_evidence_refuses_multiplicity_if_a_dedupe_guard_is_ever_lost(rows, message):
    """The verifier must not pick a winner when the database stops being sure."""

    with pytest.raises(evidence.EvidenceError, match=message):
        evidence._one(rows, "widgets")


def test_evidence_refuses_usage_still_reserved(world):
    key, engagement_id = confirmed_action(world)
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE engagement_usage_events SET status = 'reserved', "
            "finalized_at = NULL WHERE engagement_id = %s",
            (engagement_id,),
        )

    with pytest.raises(evidence.EvidenceError, match="still reserved"):
        evidence.verify(
            world["pool"],
            run_id=world["run_id"],
            idempotency_key=key,
            instagram_confirmed=True,
            now=NOW,
        )


def test_evidence_refuses_health_that_deteriorated_during_the_action(world):
    key, _ = confirmed_action(world)
    with world["pool"].connection() as connection:
        connection.execute(
            "INSERT INTO account_health_events (user_id, ig_account_id, "
            "event_type, severity, evidence, created_at) "
            "VALUES (%s, %s, 'challenge', 'warning', '{}'::jsonb, %s)",
            (world["user_id"], world["account_id"], NOW - timedelta(minutes=2)),
        )

    with pytest.raises(evidence.EvidenceError, match="restricted the account"):
        evidence.verify(
            world["pool"],
            run_id=world["run_id"],
            idempotency_key=key,
            instagram_confirmed=True,
            now=NOW,
        )


def test_a_proxy_fault_during_the_action_is_not_a_restriction(world):
    key, _ = confirmed_action(world)
    with world["pool"].connection() as connection:
        connection.execute(
            "INSERT INTO account_health_events (user_id, ig_account_id, "
            "event_type, severity, evidence, created_at) "
            "VALUES (%s, %s, 'proxy_health_failed', 'warning', '{}'::jsonb, %s)",
            (world["user_id"], world["account_id"], NOW - timedelta(minutes=2)),
        )

    assert evidence.verify(
        world["pool"],
        run_id=world["run_id"],
        idempotency_key=key,
        instagram_confirmed=True,
        now=NOW,
    ).health_status == "active"


def test_evidence_refuses_a_tool_call_that_never_confirmed(world):
    key, _ = confirmed_action(world)
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE hermes_tool_calls SET status = 'uncertain' "
            "WHERE run_id = %s AND idempotency_key = %s",
            (world["run_id"], key),
        )

    with pytest.raises(evidence.EvidenceError, match="not confirmed"):
        evidence.verify(
            world["pool"],
            run_id=world["run_id"],
            idempotency_key=key,
            instagram_confirmed=True,
            now=NOW,
        )


def test_evidence_refuses_an_unknown_idempotency_key(world):
    with pytest.raises(evidence.EvidenceError, match="no tool calls"):
        evidence.verify(
            world["pool"],
            run_id=world["run_id"],
            idempotency_key="canary-never-happened",
            instagram_confirmed=True,
            now=NOW,
        )


@pytest.mark.parametrize("action", ["like", "reply", "dm"])
def test_evidence_refuses_released_usage_for_sent_billable_action(world, action):
    key, engagement_id = confirmed_action(world, action=action)
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE engagement_usage_events SET status='released' WHERE engagement_id=%s",
            (engagement_id,),
        )
        finalized = connection.execute(
            "SELECT finalized_at FROM engagement_usage_events WHERE engagement_id=%s",
            (engagement_id,),
        ).fetchone()["finalized_at"]
    assert finalized is not None
    with pytest.raises(evidence.EvidenceError, match="sent engagement usage is not billable"):
        evidence.verify(
            world["pool"], run_id=world["run_id"], idempotency_key=key,
            instagram_confirmed=True, now=NOW,
        )


@pytest.mark.parametrize("action", ["like", "reply", "dm"])
def test_evidence_refuses_summary_that_omits_sent_action(world, action):
    key, _ = confirmed_action(world, action=action)
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE run_summaries SET likes_sent=0, replies_sent=0, dms_sent=0 "
            "WHERE job_id=%s", (world["job_id"],),
        )
    with pytest.raises(evidence.EvidenceError, match="summary action counts"):
        evidence.verify(world["pool"], run_id=world["run_id"], idempotency_key=key,
                        instagram_confirmed=True, now=NOW)


def test_single_action_evidence_refuses_additional_run_attempt(world):
    key, _ = confirmed_action(world)
    with world["pool"].connection() as connection:
        connection.execute(
            "INSERT INTO hermes_tool_calls "
            "(run_id, tool_name, opportunity_id, idempotency_key, status, started_at) "
            "VALUES (%s, 'reply', %s, 'extra-canary-attempt', 'uncertain', %s)",
            (world["run_id"], uuid.uuid4(), NOW),
        )
    with pytest.raises(evidence.EvidenceError, match="action attempts"):
        evidence.verify(world["pool"], run_id=world["run_id"], idempotency_key=key,
                        instagram_confirmed=True, now=NOW)


def test_view_evidence_refuses_summary_with_an_unexpected_like(world):
    key, _ = confirmed_action(world, action="view")
    with world["pool"].connection() as connection:
        connection.execute("UPDATE run_summaries SET likes_sent=1 WHERE job_id=%s",
                           (world["job_id"],))
    with pytest.raises(evidence.EvidenceError, match="summary action counts"):
        evidence.verify(world["pool"], run_id=world["run_id"], idempotency_key=key,
                        instagram_confirmed=True, now=NOW)
