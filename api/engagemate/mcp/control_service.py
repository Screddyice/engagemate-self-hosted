"""Validation and confirmation policy for customer MCP controls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import asdict
from typing import Any, Callable, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from engagemate.domain.budgets import BudgetPolicy
from engagemate.mcp.principal import McpPrincipal


class _Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def validated_changes(self) -> dict[str, Any]:
        changes = self.model_dump(exclude_none=True)
        if not changes:
            raise ValueError("patch cannot be empty")
        return changes


class PersonaPatch(_Patch):
    brand_description: str | None = Field(default=None, max_length=1000)
    goals: list[str] | None = Field(default=None, max_length=10)
    tone: str | None = Field(default=None, max_length=500)
    voice: str | None = Field(default=None, max_length=1000)
    preferred_vocabulary: list[str] | None = Field(default=None, max_length=50)
    prohibited_language: list[str] | None = Field(default=None, max_length=50)
    story_reply_guidance: str | None = Field(default=None, max_length=1000)
    dm_guidance: str | None = Field(default=None, max_length=1000)
    expected_language: str | None = Field(
        default=None, pattern=r"^[a-z]{2}(-[A-Za-z0-9]{2,8})?$"
    )


class SchedulePatch(_Patch):
    timezone: str | None = Field(default=None, max_length=64)
    schedule_hour: int | None = Field(default=None, ge=0, le=23)


class AudienceTargetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(pattern=r"^[A-Za-z0-9._]{1,64}$")
    tier: Literal["vip", "broad", "discovery"]


class SourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(pattern=r"^[A-Za-z0-9._]{1,64}$")


class LimitsPatch(_Patch):
    daily_like_limit: int | None = Field(default=None, ge=0)
    daily_reply_limit: int | None = Field(default=None, ge=0)
    daily_dm_limit: int | None = Field(default=None, ge=0, le=0)
    daily_view_limit: int | None = Field(default=None, ge=0)
    daily_reel_message_limit: int | None = Field(default=None, ge=0, le=20)


# The worker refuses to spend past these, so the control plane must approve
# against the same numbers. They are read from the budget policy rather than
# copied, because a copy that drifted would approve a raise the worker then
# silently declines to use.
_PLAN_TOTALS = BudgetPolicy._PLAN_TOTALS
_PLAN_VIEWS = BudgetPolicy._PLAN_VIEW_TOTALS
_WARMUP = BudgetPolicy._WARMUP_BRACKETS
_PLAN_TARGETS = {"starter": 25, "pro": 100, "scale": 300, "agency": 300, "custom": 300, "self_hosted": 300}


class ControlService:
    def __init__(
        self,
        repository: Any,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        public_app_url: str | None = None,
    ) -> None:
        self.repository = repository
        self._now = now
        self.public_app_url = public_app_url.rstrip("/") if public_app_url else None

    @staticmethod
    def _user(principal: McpPrincipal | UUID) -> UUID:
        return principal.user_id if isinstance(principal, McpPrincipal) else principal

    def _config(self, user_id: UUID, account_id: UUID) -> dict[str, Any]:
        config = self.repository.get_config(user_id, account_id)
        if config is None:
            raise LookupError("account configuration not found")
        return config

    def growth(self, user_id: UUID, account_id: UUID, *, days: int = 30) -> dict[str, Any]:
        if not 1 <= days <= 90:
            raise ValueError("days must be between 1 and 90")
        end = self._now()
        start = end - timedelta(days=days)
        rows = self.repository.growth_snapshots(user_id, account_id, start, end)
        valid = [row for row in rows if row.get("follower_count") is not None]
        if not valid:
            return {
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "current_followers": None,
                "change": None,
                "freshness": "missing",
                "snapshots": [],
            }
        current = valid[-1]
        first = valid[0]
        captured = current.get("captured_at")
        freshness = (
            "fresh"
            if isinstance(captured, datetime) and end - captured <= timedelta(hours=36)
            else "stale"
        )
        return {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "current_followers": current["follower_count"],
            "change": current["follower_count"] - first["follower_count"],
            "freshness": freshness,
            "snapshots": rows,
        }

    def list_accounts(self, principal: McpPrincipal) -> dict[str, Any]:
        return {"accounts": [_serialize(asdict(row)) for row in self.repository.list_accounts(principal.user_id)]}

    def account_status(self, principal: McpPrincipal, account_id: UUID) -> dict[str, Any]:
        status = self.repository.account_status(principal.user_id, account_id)
        if status is None:
            raise LookupError("account not found")
        return _serialize(asdict(status))

    def overview(
        self, principal: McpPrincipal, account_id: UUID, *, days: int
    ) -> dict[str, Any]:
        if not 1 <= days <= 90:
            raise ValueError("days must be between 1 and 90")
        end = self._now()
        start = end - timedelta(days=days)
        result = self.repository.stats_overview(principal.user_id, account_id, start, end)
        if result is None:
            raise LookupError("account not found")
        payload = _serialize(asdict(result))
        payload["freshness"] = (
            "missing"
            if result.snapshot_captured_at is None
            else "fresh"
            if end - result.snapshot_captured_at <= timedelta(hours=36)
            else "stale"
        )
        return payload

    def activity(
        self, principal: McpPrincipal, account_id: UUID, *, days: int
    ) -> dict[str, Any]:
        if not 1 <= days <= 90:
            raise ValueError("days must be between 1 and 90")
        if self.repository.account_status(principal.user_id, account_id) is None:
            raise LookupError("account not found")
        end = self._now()
        start = end - timedelta(days=days)
        return {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "activity": self.repository.activity(principal.user_id, account_id, start, end),
        }

    def persona(self, principal: McpPrincipal, account_id: UUID) -> dict[str, Any]:
        config = self._config(principal.user_id, account_id)
        return {"version": config["version"], "profile": config.get("profile") or {}}

    def audience(self, principal: McpPrincipal, account_id: UUID) -> dict[str, Any]:
        self._config(principal.user_id, account_id)
        return {"targets": self.repository.list_targets(principal.user_id, account_id)}

    def sources(self, principal: McpPrincipal, account_id: UUID) -> dict[str, Any]:
        self._config(principal.user_id, account_id)
        return {"sources": self.repository.list_sources(principal.user_id, account_id)}

    def schedule(self, principal: McpPrincipal, account_id: UUID) -> dict[str, Any]:
        config = self._config(principal.user_id, account_id)
        return {
            "version": config["version"],
            "timezone": config["timezone"],
            "schedule_hour": config["schedule_hour"],
        }

    def limits(self, principal: McpPrincipal, account_id: UUID) -> dict[str, Any]:
        config = self._config(principal.user_id, account_id)
        return {
            "version": config["version"],
            "daily_like_limit": config["daily_like_limit"],
            "daily_reply_limit": config["daily_reply_limit"],
            "daily_dm_limit": config["daily_dm_limit"],
            "daily_view_limit": config["daily_view_limit"],
            "daily_reel_message_limit": config.get("daily_reel_message_limit"),
            "warmup_exempt": config.get("warmup_exempt", False),
        }

    def update_persona(
        self, principal: McpPrincipal, account_id: UUID, patch: PersonaPatch
    ) -> dict[str, Any]:
        changes = patch.validated_changes()
        current = self._config(principal.user_id, account_id)
        profile = dict(current.get("profile") or {})
        profile.update(changes)
        db_patch: dict[str, Any] = {"profile": profile}
        if "voice" in changes or "tone" in changes or "brand_description" in changes:
            db_patch["persona"] = changes.get("voice") or changes.get("tone") or changes.get(
                "brand_description"
            )
        if "prohibited_language" in changes:
            db_patch["banned_phrases"] = changes["prohibited_language"]
        if "expected_language" in changes:
            db_patch["expected_language"] = changes["expected_language"]
        updated = self.repository.apply_config_patch(
            principal.user_id,
            account_id,
            db_patch,
            "mcp",
            principal.client_id,
            "persona.update",
        )
        return {"status": "updated", "version": updated["version"], "profile": profile}

    def update_schedule(
        self, principal: McpPrincipal, account_id: UUID, patch: SchedulePatch
    ) -> dict[str, Any]:
        changes = patch.validated_changes()
        if "timezone" in changes:
            try:
                ZoneInfo(changes["timezone"])
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("timezone must be a valid IANA name") from exc
        self._config(principal.user_id, account_id)
        updated = self.repository.apply_config_patch(
            principal.user_id,
            account_id,
            changes,
            "mcp",
            principal.client_id,
            "schedule.update",
        )
        return {
            "status": "updated",
            "version": updated["version"],
            "timezone": updated["timezone"],
            "schedule_hour": updated["schedule_hour"],
        }

    @staticmethod
    def _warmup_limits(
        started_at: datetime, now: datetime
    ) -> tuple[int, int, int, int, int]:
        """Today's (like, reply, dm, view, private_message) warm-up ceilings."""

        day = (now - started_at).days + 1
        if day < 1:
            raise ValueError("warm-up start is invalid")
        for final_day, limits in _WARMUP:
            if final_day is None or day <= final_day:
                return limits
        raise AssertionError("unreachable")

    def update_limits(
        self, principal: McpPrincipal, account_id: UUID, patch: LimitsPatch
    ) -> dict[str, Any]:
        changes = patch.validated_changes()
        current = self._config(principal.user_id, account_id)
        current = {**current, "daily_reel_message_limit": current.get("daily_reel_message_limit")}
        merged = {
            key: changes.get(key, current[key])
            for key in (
                "daily_like_limit",
                "daily_reply_limit",
                "daily_dm_limit",
                "daily_view_limit",
                "daily_reel_message_limit",
            )
        }
        before = {key: current[key] for key in merged}
        raised = any((merged[key] or 0) > (before[key] or 0) for key in merged)
        if raised:
            # Saved maxima may exceed today's effective warm-up allowance.
            # Reductions must remain available, including before first Launch.
            started_at = current.get("warmup_started_at")
            if not isinstance(started_at, datetime) or started_at.tzinfo is None:
                raise ValueError("warm-up state is unavailable")
            # A saved maximum is a ceiling on what the worker may spend, not a
            # promise it will. Warm-up clamps the pair down to one shared
            # private-message allowance at run time, so this only has to keep
            # each raised maximum inside its own bracket.
            bracket = (_WARMUP[-1][1] if current.get("warmup_exempt") is True
                       else self._warmup_limits(started_at, self._now()))
            per_action = (*bracket[:4], bracket[-1])
            increases = {key: value for key, value in merged.items() if (value or 0) > (before[key] or 0)}
            warmup_ceilings = dict(zip(merged, per_action, strict=True))
            if any(value > warmup_ceilings[key] for key, value in increases.items()):
                raise ValueError("limit exceeds the current warm-up ceiling")
            plan = str(current["plan"])
            if plan not in _PLAN_TOTALS:
                raise ValueError("plan is invalid")
            # Per-action maxima can sum above the plan budget. The worker
            # enforces aggregate usage; unchanged maxima must not block edits.
            if any(
                value > (_PLAN_VIEWS[plan] if key == "daily_view_limit" else _PLAN_TOTALS[plan])
                for key, value in increases.items()
            ):
                raise ValueError("limit exceeds the plan ceiling")
            kind = (
                "enable_dm"
                if before["daily_dm_limit"] == 0 and merged["daily_dm_limit"] > 0
                else "raise_limit"
            )
            return self._pending(
                principal,
                account_id,
                kind,
                changes,
                before,
                merged,
                "limits.update",
            )
        updated = self.repository.apply_config_patch(
            principal.user_id,
            account_id,
            changes,
            "mcp",
            principal.client_id,
            "limits.update",
        )
        return {"status": "updated", "version": updated["version"], "limits": merged}

    def add_audience(
        self,
        principal: McpPrincipal,
        account_id: UUID,
        target: AudienceTargetInput,
    ) -> dict[str, Any]:
        current = self._config(principal.user_id, account_id)
        existing = self.repository.list_targets(principal.user_id, account_id)
        plan = str(current["plan"])
        if len(existing) >= _PLAN_TARGETS.get(plan, 0):
            raise ValueError("target limit reached for plan")
        username = target.username.lower()
        if any(str(row["username"]).lower() == username for row in existing):
            raise ValueError("target already exists")
        addition = {"username": username, "tier": target.tier}
        return self._pending(
            principal,
            account_id,
            "broaden_audience",
            addition,
            [str(row["username"]).lower() for row in existing],
            [*[str(row["username"]).lower() for row in existing], username],
            "audience.add",
        )

    def _pending(
        self,
        principal: McpPrincipal,
        account_id: UUID,
        kind: str,
        patch: dict[str, Any],
        before: Any,
        after: Any,
        tool_name: str,
    ) -> dict[str, Any]:
        expires_at = self._now() + timedelta(minutes=15)
        change = self.repository.create_pending_change(
            principal.user_id,
            account_id,
            principal.client_id,
            kind,
            patch,
            before,
            after,
            expires_at,
            tool_name,
        )
        return {
            "status": "confirmation_required",
            "confirmation_id": str(change["id"]),
            "expires_at": change["expires_at"].isoformat(),
            "preview": {
                "before": change["before_state"],
                "after": change["after_state"],
            },
        }

    def confirm_change(
        self,
        principal: McpPrincipal,
        confirmation_id: UUID,
    ) -> dict[str, Any]:
        result = self.repository.confirm_pending_change(
            principal.user_id,
            principal.client_id,
            confirmation_id,
            self._now(),
        )
        confirmed_at = result["confirmed_at"]
        return {
            "status": "updated",
            "kind": result["kind"],
            "confirmed_at": confirmed_at.isoformat(),
            "result": result.get("result"),
        }

    def remove_audience(
        self, principal: McpPrincipal, account_id: UUID, target_id: UUID
    ) -> dict[str, str]:
        if not self.repository.remove_target(principal.user_id, account_id, target_id):
            raise LookupError("target not found")
        return {"status": "updated"}

    def remove_source(
        self, principal: McpPrincipal, account_id: UUID, source_id: UUID
    ) -> dict[str, str]:
        if not self.repository.remove_source(principal.user_id, account_id, source_id):
            raise LookupError("source not found")
        return {"status": "updated"}

    def pause(self, principal: McpPrincipal, account_id: UUID) -> dict[str, str]:
        if not self.repository.pause_automation(
            principal.user_id, account_id, principal.client_id
        ):
            raise LookupError("account not found")
        return {"status": "paused"}

    def resume(self, principal: McpPrincipal, account_id: UUID) -> dict[str, str]:
        self.repository.resume_automation(
            principal.user_id,
            account_id,
            principal.client_id,
            self._now(),
        )
        return {"status": "running"}

    def connection_status(
        self, principal: McpPrincipal, account_id: UUID
    ) -> dict[str, Any]:
        account = self.repository.account_status(principal.user_id, account_id)
        if account is None:
            raise LookupError("account not found")
        now = self._now()
        return {
            "account_id": str(account.id),
            "username": account.username,
            "connected": account.session_captured_at is not None,
            "session_fresh": account.session_captured_at is not None
            and (account.session_expires_at is None or account.session_expires_at > now),
            "proxy_bound": account.proxy_bound,
            "health_status": account.health_status,
        }

    def start_desktop_setup(self, principal: McpPrincipal) -> dict[str, str]:
        if not self.public_app_url:
            raise RuntimeError("ENGAGEMATE_WEB_ORIGIN is not configured")
        return {
            "status": "desktop_required",
            "setup_url": f"{self.public_app_url}/accounts/connect/instagram",
        }

    def add_source(
        self, principal: McpPrincipal, account_id: UUID, source: SourceInput
    ) -> dict[str, Any]:
        self._config(principal.user_id, account_id)
        existing = self.repository.list_sources(principal.user_id, account_id)
        username = source.username.lower()
        if len(existing) >= 5:
            raise ValueError("source limit reached")
        if any(str(row["username"]).lower() == username for row in existing):
            raise ValueError("source already exists")
        return {
            "status": "updated",
            "source": self.repository.add_source(principal.user_id, account_id, username),
        }


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value
