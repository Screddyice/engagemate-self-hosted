"""Policy reads and progress reports against the complete migration chain."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from engagemate.worker.execution_mcp_repository import ExecutionMcpRepository

from tests.worker import test_canary_scripts as canary_fixtures

database_url = canary_fixtures.database_url
world = canary_fixtures.world


@pytest.fixture
def follower_world(world):
    pool = world["pool"]
    profile, source = uuid4(), uuid4()
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO brand_profiles (id,user_id,ig_account_id,reference_url,reference_username,follower_audience_approved_at)
            VALUES (%s,%s,%s,'https://www.instagram.com/inspiration/','inspiration',%s)
        """,
            (profile, world["user_id"], world["account_id"], datetime.now(UTC)),
        )
        connection.execute(
            """
            INSERT INTO brand_reference_accounts (id,brand_profile_id,username,profile_url,position)
            VALUES (%s,%s,'inspiration','https://www.instagram.com/inspiration/',0)
        """,
            (source, profile),
        )
    run = NS(
        run_id=world["run_id"],
        context=NS(user_id=world["user_id"], ig_account_id=world["account_id"]),
        config_snapshot={
            "follower_sources": [{"id": str(source), "username": "inspiration"}]
        },
    )
    return world, ExecutionMcpRepository(pool), run, profile, source


async def test_discovery_policy_rechecks_approval_and_source_membership(follower_world):
    world, repository, run, profile, source = follower_world
    assert await repository.discovery_stop_reason(run) is None
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE brand_profiles SET follower_audience_approved_at=NULL WHERE id=%s",
            (profile,),
        )
    assert await repository.discovery_stop_reason(run) == "source_changed"
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE brand_profiles SET follower_audience_approved_at=now() WHERE id=%s",
            (profile,),
        )
        connection.execute(
            "DELETE FROM brand_reference_accounts WHERE id=%s", (source,)
        )
    assert await repository.discovery_stop_reason(run) == "source_changed"


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (
            "UPDATE agent_configs SET enabled=false WHERE ig_account_id=%s",
            "account_disabled",
        ),
        (
            "UPDATE ig_accounts SET health_status='paused' WHERE id=%s",
            "account_unhealthy",
        ),
        (
            "UPDATE ig_accounts SET paused_at=now() WHERE id=%s",
            "account_unhealthy",
        ),
        (
            "UPDATE hermes_execution_runs SET status='completed' WHERE ig_account_id=%s",
            "run_unavailable",
        ),
    ],
)
async def test_discovery_policy_rechecks_account_and_run_state(
    follower_world, mutation, reason
):
    world, repository, run, _, _ = follower_world
    with world["pool"].connection() as connection:
        connection.execute(mutation, (world["account_id"],))
    assert await repository.discovery_stop_reason(run) == reason


async def test_discovery_policy_scopes_controls_and_active_run_to_tenant(
    follower_world,
):
    world, repository, run, _, _ = follower_world
    with world["pool"].connection() as connection:
        connection.execute(
            "INSERT INTO instagram_execution_controls (scope,user_id,blocked,reason_code) VALUES ('tenant',%s,true,'operator_stop')",
            (world["other_user_id"],),
        )
    assert await repository.discovery_stop_reason(run) is None
    with world["pool"].connection() as connection:
        connection.execute(
            "INSERT INTO instagram_execution_controls (scope,user_id,blocked,reason_code) VALUES ('tenant',%s,true,'operator_stop')",
            (world["user_id"],),
        )
    assert await repository.discovery_stop_reason(run) == "safety_stop"
    run.context = NS(
        user_id=world["other_user_id"], ig_account_id=world["other_account_id"]
    )
    assert await repository.discovery_stop_reason(run) == "run_unavailable"


async def test_report_write_cannot_update_other_tenant_or_completed_run(follower_world):
    world, repository, run, _, _ = follower_world
    report = {
        "counters": {"checked": 17, "sourced": 30},
        "stop_reason": "scan_limit",
        "can_continue": False,
    }
    await repository.record_discovery(
        run_id=run.run_id,
        user_id=world["other_user_id"],
        ig_account_id=world["account_id"],
        report=report,
    )
    with world["pool"].connection() as connection:
        assert (
            connection.execute(
                "SELECT discovery_report FROM hermes_execution_runs WHERE id=%s",
                (run.run_id,),
            ).fetchone()["discovery_report"]
            == {}
        )
    await repository.record_discovery(
        run_id=run.run_id,
        user_id=world["user_id"],
        ig_account_id=world["account_id"],
        report=report,
    )
    with world["pool"].connection() as connection:
        assert (
            connection.execute(
                "SELECT discovery_report FROM hermes_execution_runs WHERE id=%s",
                (run.run_id,),
            ).fetchone()["discovery_report"]
            == report
        )
        connection.execute(
            "UPDATE hermes_execution_runs SET status='completed' WHERE id=%s",
            (run.run_id,),
        )
    await repository.record_discovery(
        run_id=run.run_id,
        user_id=world["user_id"],
        ig_account_id=world["account_id"],
        report={},
    )
    with world["pool"].connection() as connection:
        assert (
            connection.execute(
                "SELECT discovery_report FROM hermes_execution_runs WHERE id=%s",
                (run.run_id,),
            ).fetchone()["discovery_report"]
            == report
        )


async def test_action_control_blocks_execution_without_blocking_read_only_discovery(
    follower_world,
):
    world, repository, run, _, _ = follower_world
    with world["pool"].connection() as connection:
        connection.execute(
            "INSERT INTO instagram_execution_controls (scope,action,blocked,reason_code) VALUES ('action','like',true,'operator_stop')"
        )
    assert await repository.discovery_stop_reason(run) is None
    assert await repository.discovery_stop_reason(run, action="view") is None
    assert await repository.discovery_stop_reason(run, action="like") == "safety_stop"


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


@pytest.mark.parametrize(
    "reason,signal",
    [
        ("rate_limited", "rate_limit"),
        ("checkpoint_required", "checkpoint"),
        ("authentication_required", "login_required"),
    ],
)
@pytest.mark.parametrize("initial_health", ["active", "degraded"])
async def test_restricted_discovery_pauses_and_records_one_canonical_signal(
    follower_world, reason, signal, initial_health
):
    import asyncio

    world, repository, run, _, _ = follower_world
    report = {"counters": {"checked": 2}, "stop_reason": reason, "can_continue": False}
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE ig_accounts SET health_status=%s WHERE id=%s",
            (initial_health, world["account_id"]),
        )
    values = {
        "run_id": run.run_id,
        "user_id": world["user_id"],
        "ig_account_id": world["account_id"],
        "report": report,
    }
    await asyncio.gather(
        repository.record_discovery(**values), repository.record_discovery(**values)
    )
    with world["pool"].connection() as connection:
        account = connection.execute(
            "SELECT health_status,paused_at,pause_reason FROM ig_accounts WHERE id=%s",
            (world["account_id"],),
        ).fetchone()
        events = connection.execute(
            "SELECT event_type,severity,evidence FROM account_health_events WHERE user_id=%s AND ig_account_id=%s",
            (world["user_id"], world["account_id"]),
        ).fetchall()
        saved = connection.execute(
            "SELECT discovery_report FROM hermes_execution_runs WHERE id=%s",
            (run.run_id,),
        ).fetchone()["discovery_report"]
    assert _text(account["health_status"]) == "paused"
    assert _text(account["pause_reason"]) == signal
    assert account["paused_at"] is not None
    assert saved == report
    assert len(events) == 1
    assert _text(events[0]["event_type"]) == signal
    assert _text(events[0]["severity"]) == "warning"
    assert events[0]["evidence"] == {
        "source": "follower_discovery",
        "run_id": str(run.run_id),
        "reason": reason,
    }
    assert await repository.discovery_stop_reason(run) == "account_unhealthy"
    await repository.record_discovery(**values)
    with world["pool"].connection() as connection:
        assert (
            connection.execute(
                "SELECT paused_at FROM ig_accounts WHERE id=%s", (world["account_id"],)
            ).fetchone()["paused_at"]
            == account["paused_at"]
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM account_health_events WHERE ig_account_id=%s",
                (world["account_id"],),
            ).fetchone()["count"]
            == 1
        )


@pytest.mark.parametrize("scope", ["wrong_user", "wrong_account", "completed"])
async def test_restricted_report_cannot_pause_foreign_or_completed_run(
    follower_world, scope
):
    world, repository, run, _, _ = follower_world
    user_id, account_id = world["user_id"], world["account_id"]
    if scope == "wrong_user":
        user_id = world["other_user_id"]
    elif scope == "wrong_account":
        account_id = world["other_account_id"]
    else:
        with world["pool"].connection() as connection:
            connection.execute(
                "UPDATE hermes_execution_runs SET status='completed' WHERE id=%s",
                (run.run_id,),
            )
    await repository.record_discovery(
        run_id=run.run_id,
        user_id=user_id,
        ig_account_id=account_id,
        report={"stop_reason": "rate_limited", "can_continue": False},
    )
    with world["pool"].connection() as connection:
        accounts = connection.execute(
            "SELECT health_status,paused_at FROM ig_accounts WHERE id=ANY(%s)",
            ([world["account_id"], world["other_account_id"]],),
        ).fetchall()
        assert all(
            _text(row["health_status"]) == "active" and row["paused_at"] is None
            for row in accounts
        )
        assert (
            connection.execute("SELECT count(*) FROM account_health_events").fetchone()[
                "count"
            ]
            == 0
        )
        assert (
            connection.execute(
                "SELECT discovery_report FROM hermes_execution_runs WHERE id=%s",
                (run.run_id,),
            ).fetchone()["discovery_report"]
            == {}
        )


@pytest.mark.parametrize("health", ["paused", "challenged", "banned"])
async def test_restricted_report_preserves_existing_protective_state(
    follower_world, health
):
    world, repository, run, _, _ = follower_world
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE ig_accounts SET health_status=%s,pause_reason='existing_protection' WHERE id=%s",
            (health, world["account_id"]),
        )
    await repository.record_discovery(
        run_id=run.run_id,
        user_id=world["user_id"],
        ig_account_id=world["account_id"],
        report={"stop_reason": "rate_limited", "can_continue": False},
    )
    with world["pool"].connection() as connection:
        account = connection.execute(
            "SELECT health_status,pause_reason FROM ig_accounts WHERE id=%s",
            (world["account_id"],),
        ).fetchone()
        assert _text(account["health_status"]) == health
        assert _text(account["pause_reason"]) == "existing_protection"
        assert (
            connection.execute("SELECT count(*) FROM account_health_events").fetchone()[
                "count"
            ]
            == 0
        )


async def test_health_event_failure_rolls_back_report_and_pause(follower_world):
    import psycopg

    world, repository, run, _, _ = follower_world
    with world["pool"].connection() as connection:
        connection.execute(
            "ALTER TABLE account_health_events ADD CONSTRAINT test_reject_restriction CHECK (event_type <> 'rate_limit')"
        )
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            await repository.record_discovery(
                run_id=run.run_id,
                user_id=world["user_id"],
                ig_account_id=world["account_id"],
                report={"stop_reason": "rate_limited", "can_continue": False},
            )
        with world["pool"].connection() as connection:
            account = connection.execute(
                "SELECT health_status,paused_at FROM ig_accounts WHERE id=%s",
                (world["account_id"],),
            ).fetchone()
            assert _text(account["health_status"]) == "active"
            assert account["paused_at"] is None
            assert (
                connection.execute(
                    "SELECT discovery_report FROM hermes_execution_runs WHERE id=%s",
                    (run.run_id,),
                ).fetchone()["discovery_report"]
                == {}
            )
    finally:
        with world["pool"].connection() as connection:
            connection.execute(
                "ALTER TABLE account_health_events DROP CONSTRAINT test_reject_restriction"
            )


@pytest.mark.parametrize(
    "reason",
    [
        "scan_limit",
        "source_exhausted",
        "profile_unreadable",
        "story_metadata_unverified",
    ],
)
async def test_nonrestriction_discovery_stop_does_not_pause_account(
    follower_world, reason
):
    world, repository, run, _, _ = follower_world
    await repository.record_discovery(
        run_id=run.run_id,
        user_id=world["user_id"],
        ig_account_id=world["account_id"],
        report={"stop_reason": reason, "can_continue": False},
    )
    with world["pool"].connection() as connection:
        account = connection.execute(
            "SELECT health_status,paused_at FROM ig_accounts WHERE id=%s",
            (world["account_id"],),
        ).fetchone()
        assert _text(account["health_status"]) == "active"
        assert account["paused_at"] is None
        assert (
            connection.execute("SELECT count(*) FROM account_health_events").fetchone()[
                "count"
            ]
            == 0
        )


@pytest.mark.parametrize("reason,signal", [
    ("rate_limited", "rate_limit"),
    ("checkpoint_required", "checkpoint"),
    ("authentication_required", "login_required"),
])
async def test_preclaim_profile_restriction_uses_existing_pause_transaction(follower_world, reason, signal):
    from engagemate.worker.execution_mcp_tools import _follower_action_stop_reason

    world, repository, run, _, _ = follower_world

    async def recheck(*_):
        error = RuntimeError("private browser detail")
        error.reason = reason
        raise error

    run.execution_mode = "autonomous"
    run.tool_repository = repository
    run.discovery = NS(
        recheck_prospect=recheck,
        follower_report=lambda: {"counters": {"checked": 2}, "stop_reason": None, "can_continue": True},
    )
    assert await _follower_action_stop_reason(run, NS(), "like") == reason
    with world["pool"].connection() as connection:
        account = connection.execute(
            "SELECT health_status,pause_reason FROM ig_accounts WHERE id=%s", (world["account_id"],)
        ).fetchone()
        events = connection.execute(
            "SELECT event_type FROM account_health_events WHERE ig_account_id=%s", (world["account_id"],)
        ).fetchall()
        claims = connection.execute(
            "SELECT count(*) AS count FROM story_engagements WHERE ig_account_id=%s", (world["account_id"],)
        ).fetchone()["count"]
    assert _text(account["health_status"]) == "paused"
    assert _text(account["pause_reason"]) == signal
    assert [_text(event["event_type"]) for event in events] == [signal]
    assert claims == 0


@pytest.mark.parametrize("privacy", [True, None, 0, "false"])
@pytest.mark.parametrize("when", ["before_claim", "after_pacing"])
async def test_fresh_privacy_recheck_blocks_action_and_discards_prior_proof(privacy, when):
    from engagemate.worker.active_run import ActiveRunRegistry
    from engagemate.worker.execution_mcp_server import build_execution_mcp
    from engagemate.worker.wiring import _SessionBoundDiscovery

    from tests.worker.test_execution_mcp_followers import follower_run
    from tests.worker.test_execution_mcp_tools import KEY, _opportunity

    run = follower_run()
    run.context.username = "sender"
    opportunity = _opportunity()
    current_privacy = privacy if when == "before_claim" else False

    class Browser:
        async def inspect(self, handle):
            assert handle == opportunity.target_handle
            return {
                "owner_id": opportunity.owner_id, "handle": handle,
                "private": current_privacy, "following": False,
                "followed_by": False, "has_story": True,
            }

    discovery = _SessionBoundDiscovery(NS(), None)
    discovery._follower_browser = Browser()
    discovery._verified_profile = (opportunity.target_handle, opportunity.owner_id, 0)
    run.discovery = discovery

    async def pace(*_):
        nonlocal current_privacy
        current_privacy = privacy

    run.pace = pace
    opaque_id = uuid4()
    run.opportunities[opaque_id] = opportunity
    registry = ActiveRunRegistry()
    server = build_execution_mcp(registry, KEY)
    with registry.activate(run):
        result = (await server.call_tool("instagram.like", {
            "opportunity_id": str(opaque_id), "idempotency_key": "private_profile_recheck",
        })).structured_content
    assert result == {"status": "denied", "reason": "excluded" if privacy is True else "unknown"}
    assert discovery._verified_profile is None
    assert run.executor.calls == []
    if when == "before_claim":
        assert run.tool_repository.begun == []
    else:
        assert run.tool_repository.finished[-1]["safe_result"] == result
