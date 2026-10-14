from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from engagemate.mcp.control_repository import AccountStatus
from engagemate.mcp.control_service import (
    AudienceTargetInput,
    ControlService,
    LimitsPatch,
    PersonaPatch,
    SchedulePatch,
    SourceInput,
)
from engagemate.mcp.principal import McpPrincipal


USER_ID = UUID("11111111-1111-4111-8111-111111111111")
ACCOUNT_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
PRINCIPAL = McpPrincipal(USER_ID, "client-test", frozenset({"config:write"}))


class FakeControlRepository:
    def __init__(self) -> None:
        self.pending = []
        self.pending_by_id = {}
        self.applied = []
        self.targets = []
        self.atomic_resume_calls = []
        self.config = {
            "user_id": USER_ID,
            "ig_account_id": ACCOUNT_ID,
            "plan": "pro",
            "warmup_started_at": NOW - timedelta(days=40),
            "version": 1,
            "profile": {},
            "timezone": "UTC",
            "schedule_hour": 9,
            "daily_like_limit": 5,
            "daily_reply_limit": 1,
            "daily_dm_limit": 0,
            "daily_view_limit": 50,
            "enabled": False,
            "persona": "Direct and useful",
            "launched_at": NOW,
        }

    def account_status(self, user_id, account_id):
        if (user_id, account_id) != (USER_ID, ACCOUNT_ID):
            return None
        return AccountStatus(
            id=ACCOUNT_ID,
            username="owned",
            health_status="active",
            automation_enabled=False,
            session_captured_at=NOW,
            session_expires_at=NOW + timedelta(days=10),
            proxy_bound=True,
        )

    def growth_snapshots(self, user_id, account_id, period_start, period_end):
        assert (user_id, account_id) == (USER_ID, ACCOUNT_ID)
        return []

    def get_config(self, user_id, account_id):
        return dict(self.config) if (user_id, account_id) == (USER_ID, ACCOUNT_ID) else None

    def apply_config_patch(self, user_id, account_id, patch, actor_type, actor_id, tool_name):
        assert user_id == USER_ID and account_id == ACCOUNT_ID
        self.config.update(patch)
        self.config["version"] += 1
        self.applied.append((patch, actor_type, actor_id, tool_name))
        return dict(self.config)

    def create_pending_change(self, user_id, account_id, client_id, kind, patch, before, after, expires_at, tool_name):
        change = {
            "id": uuid4(),
            "expires_at": expires_at,
            "before_state": before,
            "after_state": after,
            "patch": patch,
            "kind": kind,
        }
        self.pending.append(change)
        self.pending_by_id[change["id"]] = change
        return change

    def list_targets(self, user_id, account_id):
        return list(self.targets)

    def resume_automation(self, user_id, account_id, client_id, now):
        self.atomic_resume_calls.append((user_id, account_id, client_id, now))
        if (user_id, account_id) != (USER_ID, ACCOUNT_ID):
            raise LookupError("account not found")
        if self.config.get("launched_at") is None:
            raise ValueError("complete onboarding launch before using resume")
        if not str(self.config.get("persona") or "").strip():
            raise ValueError("save the account voice before resume")
        if not any(target.get("enabled") for target in self.targets):
            raise ValueError("enable at least one target before resume")
        self.config["enabled"] = True
        self.applied.append(({"enabled": True}, "mcp", client_id, "automation.resume"))
        return None

    def list_sources(self, user_id, account_id):
        return []

    def add_source(self, user_id, account_id, username):
        return {"id": str(uuid4()), "username": username, "position": 0}

    def confirm_pending_change(self, user_id, client_id, change_id, now):
        change = self.pending_by_id.get(change_id)
        if change is None or user_id != USER_ID or client_id != "client-test":
            raise LookupError("pending change not found")
        if change.get("confirmed_at") is not None:
            raise ValueError("pending change was already confirmed")
        if change["expires_at"] <= now:
            raise ValueError("pending change expired")
        change["confirmed_at"] = now
        if change["kind"] in {"raise_limit", "enable_dm"}:
            self.config.update(change["patch"])
        return {"kind": change["kind"], "confirmed_at": now}


def test_stats_mark_missing_snapshots_instead_of_returning_zero() -> None:
    service = ControlService(FakeControlRepository(), now=lambda: NOW)

    result = service.growth(USER_ID, ACCOUNT_ID, days=30)

    assert result["current_followers"] is None
    assert result["change"] is None
    assert result["freshness"] == "missing"


def test_persona_and_schedule_reject_empty_or_invalid_patches() -> None:
    with pytest.raises(ValueError, match="empty"):
        PersonaPatch().validated_changes()
    with pytest.raises(ValidationError):
        SchedulePatch(schedule_hour=24)
    with pytest.raises(ValueError, match="timezone"):
        ControlService(FakeControlRepository(), now=lambda: NOW).update_schedule(
            PRINCIPAL,
            ACCOUNT_ID,
            SchedulePatch(timezone="Not/A_Real_Zone"),
        )


def test_safe_persona_patch_applies_immediately_and_increments_version() -> None:
    repository = FakeControlRepository()
    service = ControlService(repository, now=lambda: NOW)

    result = service.update_persona(
        PRINCIPAL,
        ACCOUNT_ID,
        PersonaPatch(tone="Warm and concise", prohibited_language=["spam"]),
    )

    assert result["status"] == "updated"
    assert result["version"] == 2
    assert repository.applied[0][1:] == ("mcp", "client-test", "persona.update")


def test_raising_limit_creates_15_minute_confirmation_without_applying() -> None:
    repository = FakeControlRepository()
    service = ControlService(repository, now=lambda: NOW)

    result = service.update_limits(
        PRINCIPAL,
        ACCOUNT_ID,
        LimitsPatch(daily_like_limit=6),
    )

    assert result["status"] == "confirmation_required"
    assert result["expires_at"] == (NOW + timedelta(minutes=15)).isoformat()
    assert repository.applied == []
    assert repository.pending[0]["kind"] == "raise_limit"


def test_limit_patch_cannot_exceed_plan_or_warmup_ceiling() -> None:
    repository = FakeControlRepository()
    repository.config["warmup_started_at"] = NOW - timedelta(days=1)
    repository.config["daily_reply_limit"] = 0
    service = ControlService(repository, now=lambda: NOW)

    with pytest.raises(ValueError, match="warm-up"):
        service.update_limits(
            PRINCIPAL,
            ACCOUNT_ID,
            LimitsPatch(daily_reply_limit=1),
        )

    repository.config["warmup_started_at"] = NOW - timedelta(days=40)
    repository.config["plan"] = "starter"
    with pytest.raises(ValueError, match="plan"):
        service.update_limits(
            PRINCIPAL,
            ACCOUNT_ID,
            LimitsPatch(daily_like_limit=12),
        )


@pytest.mark.parametrize("plan,age", [("scale", 40), ("agency", 8), ("starter", 40)])
def test_valid_increase_ignores_unchanged_saved_maxima(plan, age):
    repository = FakeControlRepository()
    repository.config.update(
        plan=plan, warmup_started_at=NOW - timedelta(days=age),
        daily_like_limit=20, daily_reply_limit=5,
        daily_dm_limit=0, daily_view_limit=1000,
    )
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        LimitsPatch(daily_dm_limit=1)
    assert repository.config["daily_dm_limit"] == 0



@pytest.mark.parametrize("started_at", [None, NOW])
def test_lowering_maxima_is_allowed_before_launch_and_during_warmup(started_at):
    repository = FakeControlRepository()
    repository.config.update(
        warmup_started_at=started_at, daily_like_limit=20,
        daily_reply_limit=5, daily_dm_limit=2, daily_view_limit=1000,
    )
    result = ControlService(repository, now=lambda: NOW).update_limits(
        PRINCIPAL, ACCOUNT_ID, LimitsPatch(daily_dm_limit=0),
    )
    assert result["status"] == "updated"
    assert repository.config["daily_dm_limit"] == 0
    assert repository.config["daily_like_limit"] == 20
    assert repository.pending == []


def test_adding_target_requires_confirmation_and_normalizes_username() -> None:
    repository = FakeControlRepository()
    service = ControlService(repository, now=lambda: NOW)

    result = service.add_audience(
        PRINCIPAL,
        ACCOUNT_ID,
        AudienceTargetInput(username="Example.Brand", tier="vip"),
    )

    assert result["status"] == "confirmation_required"
    assert repository.pending[0]["kind"] == "broaden_audience"
    assert repository.pending[0]["patch"]["username"] == "example.brand"


def test_confirmation_is_same_client_single_use_and_expiring() -> None:
    repository = FakeControlRepository()
    service = ControlService(repository, now=lambda: NOW)
    pending = service.update_limits(
        PRINCIPAL,
        ACCOUNT_ID,
        LimitsPatch(daily_like_limit=6),
    )
    change_id = UUID(pending["confirmation_id"])

    confirmed = service.confirm_change(PRINCIPAL, change_id)
    assert confirmed["status"] == "updated"
    assert repository.config["daily_like_limit"] == 6

    with pytest.raises(ValueError, match="already confirmed"):
        service.confirm_change(PRINCIPAL, change_id)
    wrong_client = McpPrincipal(USER_ID, "other-client", PRINCIPAL.scopes)
    with pytest.raises(LookupError, match="not found"):
        service.confirm_change(wrong_client, change_id)

    expiring = service.update_limits(
        PRINCIPAL,
        ACCOUNT_ID,
        LimitsPatch(daily_reply_limit=2),
    )
    expiring_id = UUID(expiring["confirmation_id"])
    repository.pending_by_id[expiring_id]["expires_at"] = NOW
    with pytest.raises(ValueError, match="expired"):
        service.confirm_change(PRINCIPAL, expiring_id)


def test_source_addition_normalizes_and_enforces_five_source_limit() -> None:
    repository = FakeControlRepository()
    service = ControlService(repository, now=lambda: NOW)

    added = service.add_source(
        PRINCIPAL,
        ACCOUNT_ID,
        SourceInput(username="Example.Source"),
    )
    assert added["source"]["username"] == "example.source"

    repository.list_sources = lambda *_args: [
        {"username": f"source{index}"} for index in range(5)
    ]
    with pytest.raises(ValueError, match="source limit"):
        service.add_source(
            PRINCIPAL,
            ACCOUNT_ID,
            SourceInput(username="sixth"),
        )


def test_resume_cannot_be_used_for_first_launch() -> None:
    repository = FakeControlRepository()
    repository.config["launched_at"] = None

    with pytest.raises(ValueError, match="onboarding launch"):
        ControlService(repository, now=lambda: NOW).resume(PRINCIPAL, ACCOUNT_ID)

    assert repository.config["enabled"] is False
    assert repository.applied == []


@pytest.mark.parametrize(
    ("config_patch", "targets", "message"),
    [
        ({"persona": ""}, [{"enabled": True}], "voice"),
        ({"persona": "Direct"}, [], "target"),
    ],
)
def test_resume_rechecks_saved_voice_and_enabled_target(
    config_patch, targets, message
) -> None:
    repository = FakeControlRepository()
    repository.config.update(config_patch)
    repository.targets = targets

    with pytest.raises(ValueError, match=message):
        ControlService(repository, now=lambda: NOW).resume(PRINCIPAL, ACCOUNT_ID)

    assert repository.config["enabled"] is False
    assert repository.applied == []


def test_resume_uses_one_atomic_repository_transition() -> None:
    repository = FakeControlRepository()
    repository.targets = [{"enabled": True}]

    result = ControlService(repository, now=lambda: NOW).resume(PRINCIPAL, ACCOUNT_ID)

    assert result == {"status": "running"}
    assert repository.atomic_resume_calls == [
        (USER_ID, ACCOUNT_ID, "client-test", NOW)
    ]


def test_control_plane_approves_against_the_worker_s_own_ceilings() -> None:
    """The two halves must not hold separate copies of the same numbers.

    Before this, control_service carried its own literal plan totals, view
    totals, and warm-up brackets. A raise approved there against a stale copy
    is one the worker then silently refuses to spend.
    """

    from engagemate.domain.budgets import BudgetPolicy
    from engagemate.mcp import control_service

    assert control_service._PLAN_TOTALS is BudgetPolicy._PLAN_TOTALS
    assert control_service._PLAN_VIEWS is BudgetPolicy._PLAN_VIEW_TOTALS
    assert control_service._WARMUP is BudgetPolicy._WARMUP_BRACKETS


def test_desktop_setup_link_uses_the_configured_origin() -> None:
    service = ControlService(FakeControlRepository(), now=lambda: NOW, public_app_url="https://app.example.com/")
    assert service.start_desktop_setup(None)["setup_url"] == (
        "https://app.example.com/accounts/connect/instagram"
    )


def test_desktop_setup_without_an_origin_fails_instead_of_guessing() -> None:
    service = ControlService(FakeControlRepository(), now=lambda: NOW)
    with pytest.raises(RuntimeError, match="ENGAGEMATE_WEB_ORIGIN"):
        service.start_desktop_setup(None)
