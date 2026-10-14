"""One-action story executor with idempotency and breaker cleanup."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from engagemate.db.executions import (
    STORY_SKIP_REASONS,
    ClaimMetadata,
    ExecutionRepository,
    UsageLimitExceeded,
)
from engagemate.intelligence.schemas import EngagementDecision
from engagemate.worker.circuit_breaker import CircuitBreaker, HealthSignal
from engagemate.worker.instagram_adapter import ActionOutcome, InstagramAdapter
from engagemate.worker.story_types import StoryContent, StoryMedia, StoryOpportunity


class ClosePort(Protocol):
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    user_id: UUID
    ig_account_id: UUID
    now: datetime
    browser: ClosePort


class ExecutionResultStatus(StrEnum):
    SKIPPED = "skipped"
    SENT = "sent"
    FAILED = "failed"
    BREAKER_TRIPPED = "breaker_tripped"


@dataclass(frozen=True, slots=True, repr=False)
class ExecutionResult:
    status: ExecutionResultStatus
    engagement_id: UUID | None
    failure_code: str | None = None
    action_evidence: str | None = None

    def __repr__(self) -> str:
        return (
            f"ExecutionResult(status={self.status!r}, "
            f"engagement_id={self.engagement_id!r}, "
            f"failure_code={self.failure_code!r})"
        )


class Executor:
    def __init__(
        self, repository: ExecutionRepository, adapter: InstagramAdapter
    ) -> None:
        self._repository = repository
        self._adapter = adapter

    async def execute(
        self,
        opportunity: StoryOpportunity,
        decision: EngagementDecision,
        context: ExecutionContext,
        *,
        before_action: Callable[[UUID, ExecutionContext], Awaitable[bool]] | None = None,
    ) -> ExecutionResult:
        opportunity, decision, context = self._validate_and_copy(
            opportunity, decision, context
        )
        metadata = ClaimMetadata(
            context.user_id,
            context.ig_account_id,
            opportunity.target_id,
            opportunity.fingerprint,
            decision.action,
            decision.generated_text,
            decision.model_used,
            decision.rationale,
            decision.confidence,
        )
        try:
            claim = await self._repository.claim(metadata)
        except asyncio.CancelledError as exc:
            exc.__dict__.pop("_engagemate_story_result", None)
            raise
        except UsageLimitExceeded:
            return ExecutionResult(
                ExecutionResultStatus.SKIPPED, None, "usage_limit_reached"
            )
        except Exception:  # noqa: BLE001 - repository ports have no shared error base
            return ExecutionResult(
                ExecutionResultStatus.FAILED, None, "repository_failed"
            )
        if claim.duplicate:
            return ExecutionResult(ExecutionResultStatus.SKIPPED, claim.engagement_id)

        sent_task = None
        action_started = False
        try:
            health_failure = await self._check_health(context, claim.engagement_id)
            if health_failure is not None:
                return health_failure
            if decision.action == "reply" and before_action is not None:
                try:
                    followed = await before_action(claim.engagement_id, context)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a failed prerequisite must block the reply
                    followed = False
                if followed is not True:
                    await self._mark_failed_bounded(
                        context,
                        claim.engagement_id,
                        "reply_follow_unconfirmed",
                    )
                    return ExecutionResult(
                        ExecutionResultStatus.SKIPPED,
                        claim.engagement_id,
                        "reply_follow_unconfirmed",
                    )
            action_started = True
            outcome = await self._execute_action(opportunity, decision)
            if outcome is ActionOutcome.SKIPPED:
                reason = getattr(self._adapter, "skip_reason", None)
                if reason in STORY_SKIP_REASONS:
                    recorded = await self._repository.mark_skipped(
                        context.user_id, context.ig_account_id,
                        claim.engagement_id, reason,
                    )
                    return ExecutionResult(
                        ExecutionResultStatus.SKIPPED if recorded else ExecutionResultStatus.FAILED,
                        claim.engagement_id, reason if recorded else "repository_failed",
                    )
            if outcome is ActionOutcome.CONFIRMED:
                sent_task = self._start_sent(context, claim.engagement_id)
                if await asyncio.shield(sent_task):
                    evidence = await self._after_confirmed(context, claim.engagement_id)
                    return ExecutionResult(
                        ExecutionResultStatus.SENT, claim.engagement_id,
                        action_evidence=evidence,
                    )
                return ExecutionResult(
                    ExecutionResultStatus.FAILED,
                    claim.engagement_id,
                    "repository_failed",
                )
            health_failure = await self._check_health(context, claim.engagement_id)
            if health_failure is not None:
                return health_failure
            await self._repository.mark_failed(
                context.user_id,
                context.ig_account_id,
                claim.engagement_id,
                "action_failed",
            )
            await self._close_bounded(context.browser)
            return ExecutionResult(
                ExecutionResultStatus.FAILED,
                claim.engagement_id,
                "action_failed",
            )
        except asyncio.CancelledError as exc:
            exc.__dict__.pop("_engagemate_story_result", None)
            await self._cancellation_cleanup(
                context, claim.engagement_id, exc, sent_task, action_started
            )
            raise
        except Exception:  # noqa: BLE001 - infrastructure failures become bounded results
            await self._mark_failed_bounded(
                context, claim.engagement_id, "repository_failed"
            )
            await self._close_bounded(context.browser)
            return ExecutionResult(
                ExecutionResultStatus.FAILED,
                claim.engagement_id,
                "repository_failed",
            )

    async def _after_confirmed(self, context, engagement_id):
        try:
            await self._check_health(context, engagement_id, preserve_receipt=True)
        except Exception:  # noqa: BLE001 - operational failure cannot erase acceptance
            await self._close_bounded(context.browser)
            return "post_receipt_health_failed"
        return None

    async def _check_health(
        self, context: ExecutionContext, engagement_id: UUID, *, preserve_receipt=False
    ) -> ExecutionResult | None:
        signal = await self._adapter.detect_health_signal()
        breaker = CircuitBreaker(context.now).observe(signal)
        if breaker.stop:
            try:
                await self._repository.persist_breaker(
                    context.user_id, context.ig_account_id, breaker
                )
                if not preserve_receipt:
                    await self._repository.mark_failed(
                        context.user_id, context.ig_account_id, engagement_id,
                        "breaker_tripped",
                    )
            finally:
                await self._close_bounded(context.browser)
            return ExecutionResult(
                ExecutionResultStatus.BREAKER_TRIPPED, engagement_id, "breaker_tripped"
            )
        if signal in {HealthSignal.NETWORK_ERROR, HealthSignal.PROXY_ERROR}:
            if not preserve_receipt:
                await self._repository.mark_failed(
                    context.user_id, context.ig_account_id, engagement_id, signal.value
                )
            await self._close_bounded(context.browser)
            return ExecutionResult(
                ExecutionResultStatus.FAILED, engagement_id, signal.value
            )
        return None

    async def _execute_action(
        self, opportunity: StoryOpportunity, decision: EngagementDecision
    ) -> ActionOutcome:
        if decision.action == "view":
            return await self._adapter.view_story(
                opportunity.target_handle, opportunity.fingerprint
            )
        if decision.action == "like":
            return await self._adapter.like_story(
                opportunity.target_handle, opportunity.fingerprint
            )
        if decision.action == "reply":
            return await self._adapter.reply_to_story(
                opportunity.target_handle,
                opportunity.fingerprint,
                decision.generated_text,
            )
        return await self._adapter.dm_about_story(
            opportunity.target_handle,
            opportunity.fingerprint,
            decision.generated_text,
        )

    def _start_sent(self, context, engagement_id):
        task = asyncio.create_task(self._repository.mark_sent(
            context.user_id, context.ig_account_id, engagement_id
        ))
        # A bounded cancellation cleanup may finish before the database operation.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return task

    async def _cancellation_cleanup(
        self, context: ExecutionContext, engagement_id: UUID,
        interruption: asyncio.CancelledError, sent_task, action_started: bool,
    ) -> None:
        async def cleanup() -> None:
            receipt_task = sent_task
            if receipt_task is None and action_started and (
                getattr(self._adapter, "confirmed_before_cleanup", False) is True
            ):
                receipt_task = self._start_sent(context, engagement_id)
            if receipt_task is not None:
                if await asyncio.shield(receipt_task) is True:
                    interruption._engagemate_story_result = ExecutionResult(
                        ExecutionResultStatus.SENT, engagement_id
                    )
                    evidence = await self._after_confirmed(context, engagement_id)
                    interruption._engagemate_story_result = ExecutionResult(
                        ExecutionResultStatus.SENT, engagement_id, action_evidence=evidence
                    )
            else:
                await self._mark_failed_bounded(context, engagement_id, "cancelled")
            await self._close_bounded(context.browser)

        async def bounded_cleanup():
            try:
                async with asyncio.timeout(5):
                    await cleanup()
            except Exception:  # noqa: BLE001 - original cancellation wins
                return

        task = asyncio.create_task(bounded_cleanup())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue

    async def _mark_failed_bounded(
        self,
        context: ExecutionContext,
        engagement_id: UUID,
        failure_code: str,
    ) -> bool:
        try:
            return await self._repository.mark_failed(
                context.user_id,
                context.ig_account_id,
                engagement_id,
                failure_code,
            )
        except Exception:  # noqa: BLE001 - cleanup cannot replace primary result
            return False

    @staticmethod
    async def _close_bounded(browser: ClosePort) -> None:
        try:
            await browser.close()
        except Exception:  # noqa: BLE001 - browser close is best-effort cleanup
            return

    @staticmethod
    def _validate_and_copy(
        opportunity: StoryOpportunity,
        decision: EngagementDecision,
        context: ExecutionContext,
    ) -> tuple[StoryOpportunity, EngagementDecision, ExecutionContext]:
        valid = (
            type(opportunity) is StoryOpportunity
            and type(decision) is EngagementDecision
            and type(context) is ExecutionContext
            and type(context.user_id) is UUID
            and type(context.ig_account_id) is UUID
            and type(context.now) is datetime
            and context.now.tzinfo is timezone.utc
            and hasattr(context.browser, "close")
            and decision.engage is True
            and (
                (decision.action in {"view", "like"} and decision.generated_text is None)
                or (
                    decision.action in {"reply", "dm"}
                    and type(decision.generated_text) is str
                )
            )
            and opportunity.target_handle == opportunity.target_handle.strip()
            and bool(opportunity.target_handle)
            and opportunity.fingerprint == opportunity.fingerprint.strip()
            and re.fullmatch(r"[0-9a-f]{64}", opportunity.fingerprint) is not None
            and type(opportunity.target_id) is UUID
            and type(opportunity.owner_id) is str
            and bool(opportunity.owner_id)
            and len(opportunity.owner_id) <= 256
            and opportunity.tier in {"vip", "broad", "discovery"}
            and type(opportunity.taken_at) is datetime
            and opportunity.taken_at.tzinfo is not None
            and type(opportunity.content) is StoryContent
        )
        if not valid:
            raise ValueError("invalid execution input")
        content = StoryContent(
            opportunity.content.media_type,
            opportunity.content.caption_text,
            opportunity.content.sticker_text,
            (
                StoryMedia(
                    opportunity.content.image.mime_type,
                    bytes(opportunity.content.image.data),
                )
                if opportunity.content.image is not None
                else None
            ),
            tuple(
                StoryMedia(frame.mime_type, bytes(frame.data))
                for frame in opportunity.content.video_frames
            ),
        )
        copied_opportunity = StoryOpportunity(
            opportunity.target_id,
            opportunity.owner_id,
            opportunity.target_handle,
            opportunity.tier,
            opportunity.media_id,
            opportunity.taken_at,
            opportunity.fingerprint,
            content,
        )
        copied_decision = EngagementDecision.model_validate(
            decision.model_dump(mode="python")
        )
        copied_context = ExecutionContext(
            context.user_id, context.ig_account_id, context.now, context.browser
        )
        return copied_opportunity, copied_decision, copied_context
