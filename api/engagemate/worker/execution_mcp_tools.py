"""Five bounded Instagram tools available only to the dedicated Hermes profile."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from functools import wraps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from engagemate.domain.budgets import BudgetError
from engagemate.domain.models import Action
from engagemate.db.executions import STORY_SKIP_REASONS
from engagemate.intelligence.schemas import EngagementDecision
from engagemate.worker.active_run import ActiveRun, ActiveRunRegistry
from engagemate.worker.executor import ExecutionResult, ExecutionResultStatus
from engagemate.worker.story_types import StoryMedia, StoryOpportunity

DiscoveryLimit = Annotated[int, Field(ge=1, le=25)]
IdempotencyKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")]
GeneratedText = Annotated[str, Field(min_length=1, max_length=200)]
StoryDetail = Annotated[str, Field(min_length=3, max_length=100)]
CompletionReason = Literal[
    "budget_exhausted",
    "no_opportunities",
    "safety_stop",
    "account_unhealthy",
    "completed",
    "source_exhausted",
    "scan_limit",
    "page_limit",
    "time_limit",
]
ActionName = Literal["view", "like", "reply", "dm"]
_RESTRICTION_REASONS = frozenset(
    {
        "authentication_required",
        "checkpoint_required",
        "rate_limited",
    }
)
_POLICY_DIAGNOSTIC_REASONS = _RESTRICTION_REASONS | {
    "profile_unreadable", "profile_ambiguous", "response_unreadable",
    "response_failed", "response_too_large", "invalid_handle", "lease_lost",
}
logger = logging.getLogger(__name__)


def _boundary_idempotency_key(
    opportunity_id: str, action: ActionName
) -> str | None:
    """Derive a stable key when a model omits the optional protocol field.

    Hermes' model boundary occasionally emits a valid opportunity and action
    without the required ``idempotency_key`` even though the live MCP schema
    advertises it. Refusing the call at Pydantic validation drops a safe
    action before EngageMate can record it. The opaque opportunity UUID is
    unique for each discovery result, so a key derived from it is stable for
    retries of that exact opportunity and cannot collide with another Story.
    Invalid opportunity IDs still fail closed in ``_mutate_internal``.
    """
    try:
        opaque_id = UUID(opportunity_id)
    except (TypeError, ValueError):
        return None
    return f"auto_{action}_{opaque_id.hex}"


def _provided_or_derived_idempotency_key(
    opportunity_id: str, action: ActionName, value: str | None
) -> str | None:
    return value or _boundary_idempotency_key(opportunity_id, action)


def _view_idempotency_key(opportunity_id: UUID) -> str:
    """Build the stable key used by the deterministic view drain."""

    return f"auto_view_{opportunity_id.hex}"


def _completion_response_counters(counters: dict[str, int]) -> dict[str, int]:
    """Expose completion counts without tripping Hermes' text error heuristic.

    The Hermes gateway still classifies a successful JSON tool result as an
    error when it contains the literal ``\"failed\"`` key. Keep the ledger's
    canonical counter on ``ActiveRun`` while using an unambiguous display name
    in the MCP response.
    """
    return {
        **{key: value for key, value in counters.items() if key != "failed"},
        "action_errors": int(counters.get("failed", 0)),
    }


def _log_follower_policy_failure(stage: str, error: Exception) -> None:
    error_class = type(error).__name__
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", error_class):
        error_class = "Exception"
    reason = vars(error).get("reason")
    safe_reason = reason if type(reason) is str and reason in _POLICY_DIAGNOSTIC_REASONS else "unknown"
    logger.warning(
        "follower policy unavailable stage=%s exception=%s reason=%s",
        stage, error_class, safe_reason,
        extra={"policy_stage": stage, "exception_class": error_class, "canonical_reason": safe_reason},
    )


def register_execution_tools(server: MCPServer, registry: ActiveRunRegistry) -> None:
    # Discovery and actions share one browser and one in-memory action budget.
    # Hold this lock across awaited policy and pacing calls as well as execution.
    tool_lock = asyncio.Lock()

    def serialized(function):
        @wraps(function)
        async def guarded(*args, **kwargs):
            bound_run = registry.require()
            if bound_run.stopping:
                return _denied("run_stopping")
            task = asyncio.current_task()
            bound_run.tool_tasks.add(task)
            try:
                async with tool_lock:
                    if registry.require() is not bound_run:
                        return _denied("run_changed")
                    if bound_run.stopping:
                        return _denied("run_stopping")
                    return await function(*args, **kwargs)
            except asyncio.CancelledError:
                if bound_run.stopping:
                    # Finish run-owned cancellation without closing the SDK session.
                    return _denied("run_stopping")
                raise
            finally:
                bound_run.tool_tasks.discard(task)

        return guarded

    @server.tool(name="instagram.discover")
    @serialized
    async def discover(
        limit: DiscoveryLimit = 10,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        run = registry.require()
        if run.completed:
            return _discovery_result(_denied("run_completed"), [])
        follower_mode = bool(run.config_snapshot.get("follower_sources"))
        if follower_mode:
            opportunities = await run.discovery.find_followers(run, limit)
        else:
            opportunities = await run.discovery.find(
                run.discovery_context,
                run.context.targets,
                run.budget,
            )
        selected = opportunities[:limit]
        payload = []
        images: list[ImageContent] = []
        for opportunity in selected:
            opaque_id = uuid4()
            run.opportunities[opaque_id] = opportunity
            item = _bounded_opportunity(opaque_id, opportunity, run)
            for frame_index, media in enumerate(_bounded_media(opportunity)):
                # Content index 0 is the JSON metadata block. References stay
                # aligned across multiple opportunities and video frames.
                item["story"]["media"].append(
                    {
                        "content_index": len(images) + 1,
                        "mime_type": media.mime_type,
                    }
                )
                images.append(
                    ImageContent(
                        data=base64.b64encode(media.data).decode("ascii"),
                        mime_type=media.mime_type,
                        meta={
                            "opportunity_id": str(opaque_id),
                            "frame_index": frame_index,
                        },
                    )
                )
            payload.append(item)
        result = {
            "opportunities": payload,
            "remaining_budget": _budget_dict(run),
        }
        if follower_mode:
            report = run.discovery.follower_report()
            await run.tool_repository.record_discovery(
                run_id=run.run_id,
                user_id=run.context.user_id,
                ig_account_id=run.context.ig_account_id,
                report=report,
            )
            result["discovery"] = report
        return _discovery_result(result, images)

    @server.tool(name="instagram.view")
    @serialized
    async def view(
        opportunity_id: str, idempotency_key: IdempotencyKey | None = None
    ) -> dict[str, Any]:
        return await _mutate(
            registry,
            opportunity_id,
            _provided_or_derived_idempotency_key(opportunity_id, "view", idempotency_key),
            "view",
            None,
        )

    @server.tool(name="instagram.like")
    @serialized
    async def like(
        opportunity_id: str, idempotency_key: IdempotencyKey | None = None
    ) -> dict[str, Any]:
        return await _mutate(
            registry,
            opportunity_id,
            _provided_or_derived_idempotency_key(opportunity_id, "like", idempotency_key),
            "like",
            None,
        )

    @server.tool(name="instagram.reply")
    @serialized
    async def reply(
        opportunity_id: str,
        generated_text: GeneratedText,
        story_detail: StoryDetail,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        return await _mutate(
            registry,
            opportunity_id,
            _provided_or_derived_idempotency_key(opportunity_id, "reply", idempotency_key),
            "reply",
            generated_text,
            story_detail,
        )

    @server.tool(name="instagram.complete_run")
    @serialized
    async def complete_run(reason: CompletionReason) -> dict[str, Any]:
        run = registry.require()
        if run.completed:
            return _denied("run_completed")
        repository = run.tool_repository
        if repository is None:
            return _denied("policy_repository_unavailable")
        if run.view_only and reason not in {"safety_stop", "account_unhealthy"}:
            await _drain_view_lane(registry, run)
        counters = await repository.complete_run(
            run_id=run.run_id,
            reason=reason,
            completed_at=datetime.now(UTC),
        )
        run.completed = True
        run.completion_reason = reason
        run.completion_counters = dict(counters)
        return {
            "status": "completed",
            "reason": reason,
            "counters": _completion_response_counters(counters),
        }


async def _drain_view_lane(registry: ActiveRunRegistry, run: ActiveRun) -> None:
    """Spend view capacity on discovered Stories the model left untouched.

    View jobs are model-assisted for discovery but deterministic for allocation:
    every unclaimed opportunity is attempted once through the same mutation
    ledger, pacing, lease, identity, and receipt path as a model call. An
    uncertain result stays held in the ledger and is never replayed.
    """

    for opportunity_id in tuple(run.opportunities):
        if run.budget.remaining_for(Action.VIEW) <= 0:
            break
        if opportunity_id in run.claimed_opportunities:
            continue
        await _mutate(
            registry,
            str(opportunity_id),
            _view_idempotency_key(opportunity_id),
            "view",
            None,
        )


async def _mutate(
    registry: ActiveRunRegistry,
    opportunity_id: str,
    idempotency_key: str | None,
    action: ActionName,
    generated_text: str | None,
    story_detail: str | None = None,
) -> dict[str, Any]:
    """Return an MCP-safe copy while keeping the ledger result canonical."""
    if idempotency_key is None:
        return _denied("invalid_idempotency_key")
    result = await _mutate_internal(
        registry, opportunity_id, idempotency_key, action, generated_text, story_detail
    )
    return _gateway_safe_result(result)


async def _mutate_internal(
    registry: ActiveRunRegistry,
    opportunity_id: str,
    idempotency_key: str,
    action: ActionName,
    generated_text: str | None,
    story_detail: str | None = None,
) -> dict[str, Any]:
    if action == "dm":
        return _denied("standalone_dm_retired")
    run = registry.require()
    if run.completed:
        return _denied("run_completed")
    if run.view_only and action != "view":
        return _denied("view_only_lane")
    try:
        opaque_id = UUID(opportunity_id)
    except (TypeError, ValueError):
        return _denied("unknown_opportunity")
    opportunity = run.opportunities.get(opaque_id)
    if opportunity is None:
        return _denied("unknown_opportunity")

    decision = _decision(action, generated_text)
    gate_reason: str | None = None
    if action in {"reply", "dm"}:
        gate = run.text_gate.evaluate(decision, run.context.history.sent_messages)
        if not gate.allowed:
            gate_reason = f"text_gate_{gate.reason.value}"
        elif action == "reply" and not _story_reply_grounded(
            opportunity, generated_text, story_detail
        ):
            gate_reason = "story_reply_ungrounded"
    try:
        next_budget = run.budget.consume(Action(action))
    except BudgetError:
        return _denied("budget_exhausted")
    try:
        lease_live = await run.lease_is_live()
    except Exception:  # noqa: BLE001 - fail closed and preserve session cleanup
        lease_live = False
    if not lease_live:
        return _denied("lease_lost")

    follower_reason = await _follower_action_stop_reason(run, opportunity, action)
    if follower_reason is not None:
        return _denied(follower_reason)

    deadline = run.check_deadline
    if deadline is not None and not deadline.can_admit(
        Action(action), f"{run.context.pacing_seed}:{opportunity.fingerprint}"
    ):
        return _denied("production_check_time_limit")

    repository = run.tool_repository
    if repository is None:
        return _denied("policy_repository_unavailable")
    now = datetime.now(UTC)
    policy = await repository.begin_mutation(
        run_id=run.run_id,
        job_id=run.job.id,
        user_id=run.context.user_id,
        ig_account_id=run.context.ig_account_id,
        worker_id=run.job.claimed_by,
        opportunity_id=opaque_id,
        target_id=opportunity.target_id,
        story_fingerprint=opportunity.fingerprint,
        action=action,
        idempotency_key=idempotency_key,
        generated_text_hash=(
            hashlib.sha256(generated_text.encode("utf-8")).hexdigest()
            if generated_text is not None
            else None
        ),
        # Read only by the canary operator scripts, so a human approving a
        # live message sees the message. It never reaches safe_result.
        proposed_text=generated_text,
        now=now,
        execution_mode=run.execution_mode,
    )
    if not policy.get("allowed"):
        replay = policy.get("result") if policy.get("replay") else None
        return replay or _denied(str(policy.get("reason") or "policy_denied"))

    # A durable mutation claim owns this Story for the rest of the run. This
    # prevents the deterministic view drain from applying a second action to
    # the same Story after Hermes has already tried it.
    run.claimed_opportunities.add(opaque_id)

    if gate_reason is not None:
        safe = {"status": "denied", "reason": gate_reason}
        await repository.finish_mutation(
            run_id=run.run_id,
            idempotency_key=idempotency_key,
            status="denied",
            failure_class=gate_reason,
            safe_result=safe,
            completed_at=datetime.now(UTC),
        )
        return safe

    previous_budget = run.budget
    if deadline is not None and not deadline.can_admit(
        Action(action), f"{run.context.pacing_seed}:{opportunity.fingerprint}"
    ):
        safe = _denied("production_check_time_limit")
        await repository.finish_mutation(
            run_id=run.run_id, idempotency_key=idempotency_key,
            status="denied", failure_class="production_check_time_limit",
            safe_result=safe, completed_at=datetime.now(UTC),
        )
        return safe
    run.budget = next_budget
    async def deny(reason, *, restore=False):
        safe = _denied(reason)
        await repository.finish_mutation(
            run_id=run.run_id, idempotency_key=idempotency_key,
            status="denied", failure_class=reason, safe_result=safe,
            completed_at=datetime.now(UTC),
        )
        if restore:
            run.budget = previous_budget
        return safe

    execution_entered = False
    try:
        await run.pace(Action(action), opportunity)
        try:
            lease_live = await run.lease_is_live()
        except Exception:  # noqa: BLE001 - fail closed and preserve session cleanup
            lease_live = False
        if not lease_live:
            # Pacing and policy checks happen before the executor owns the
            # mutation. Keep the allocation available when the job lease
            # disappears during either check.
            return await deny("lease_lost", restore=True)
        follower_reason = await _follower_action_stop_reason(run, opportunity, action)
        if follower_reason is not None:
            return await deny(follower_reason, restore=True)
        if deadline is not None and not deadline.can_execute():
            return await deny("production_check_time_limit", restore=True)
        # The transition commits before entering Executor. Cancellation during
        # this await is conservative because the database thread may commit.
        # Set this before the await. A cancellation can arrive after the
        # repository commits the transition but before it returns. In that
        # case the action is already owned by the executor and its budget must
        # stay reserved.
        execution_entered = True
        armed = await repository.mark_execution_started(
            run_id=run.run_id, idempotency_key=idempotency_key,
            worker_id=run.job.claimed_by, user_id=run.context.user_id,
            account_id=run.context.ig_account_id, completed_at=datetime.now(UTC),
        )
        if not armed:
            # A false result means the durable transition did not claim this
            # mutation. It is safe to make the allocation available again.
            execution_entered = False
            return await deny("execution_claim_unavailable", restore=True)
        if deadline is not None and not deadline.can_execute():
            # mark_execution_started succeeded, so the executor owns the
            # mutation even though the final deadline check stopped it.
            return await deny("production_check_time_limit")
        result = await run.executor.execute(
            opportunity,
            decision,
            run.execution_context,
        )
    except asyncio.CancelledError as exc:
        result = getattr(exc, "_engagemate_story_result", None)
        if (
            type(result) is ExecutionResult
            and result.status is ExecutionResultStatus.SENT
            and isinstance(result.engagement_id, UUID)
            and result.failure_code is None
        ):
            safe = {"status": "confirmed"}
            if result.action_evidence:
                safe["evidence"] = result.action_evidence
        elif execution_entered:
            safe = {"status": "uncertain", "reason": "execution_cancelled"}
        else:
            safe = {"status": "denied", "reason": "cancelled_before_execution", "phase": "reserved"}
            run.budget = previous_budget
        await _finish_cancelled(repository, run, idempotency_key, safe)
        raise
    except Exception:  # noqa: BLE001 - fail closed and preserve session cleanup
        safe = {"status": "uncertain", "reason": "executor_exception"}
        if not execution_entered:
            safe = {"status": "denied", "reason": "pre_execution_failed", "phase": "reserved"}
            run.budget = previous_budget
    else:
        safe = _safe_execution_result(result.status, result.failure_code)
        if safe["status"] in {"confirmed", "uncertain", "failed"} and result.action_evidence:
            safe["evidence"] = result.action_evidence
        if safe["status"] == "denied" and safe.get("reason") in (
            STORY_SKIP_REASONS | {"duplicate_story", "reply_follow_unconfirmed"}
        ):
            # The selected engagement was never forwarded. Keep its allocation
            # for another Story; the durable claim still prevents a replay.
            run.budget = previous_budget
    if safe["status"] == "confirmed":
        await _finish_cancelled(repository, run, idempotency_key, safe, propagate=True)
    else:
        await repository.finish_mutation(
            run_id=run.run_id, idempotency_key=idempotency_key,
            status=safe["status"], failure_class=safe.get("reason"),
            safe_result=safe, completed_at=datetime.now(UTC),
        )
    return safe


async def _finish_cancelled(repository, run, idempotency_key, safe, *, propagate=False):
    async def finish():
        try:
            async with asyncio.timeout(5):
                await repository.finish_mutation(
                    run_id=run.run_id, idempotency_key=idempotency_key,
                    status=safe["status"], failure_class=safe.get("reason"),
                    safe_result=safe, completed_at=datetime.now(UTC),
                )
        except Exception:
            # The durable phase/receipt remains held if the tool ledger fails.
            pass

    task = asyncio.create_task(finish())
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    if interrupted and propagate:
        raise asyncio.CancelledError


async def _follower_action_stop_reason(
    run: ActiveRun,
    opportunity: StoryOpportunity,
    action: ActionName,
) -> str | None:
    if not run.config_snapshot.get("follower_sources"):
        return None
    if run.execution_mode == "disabled":
        return "execution_disabled"
    stage = "policy_precheck"
    try:
        reason = await run.tool_repository.discovery_stop_reason(run, action=action)
        if reason is not None:
            return reason
        stage = "prospect_recheck"
        try:
            reason = await run.discovery.recheck_prospect(run, opportunity)
        except Exception as error:  # noqa: BLE001 - browser failures expose only canonical restrictions
            reason = getattr(error, "reason", None)
            if type(reason) is not str or reason not in _RESTRICTION_REASONS:
                _log_follower_policy_failure(stage, error)
                return "policy_unavailable"
        if type(reason) is str and reason in _RESTRICTION_REASONS:
            stage = "restriction_record"
            report = dict(run.discovery.follower_report())
            report.update(stop_reason=reason, can_continue=False)
            # This existing transaction pauses the owned account and records its
            # health signal without creating an action claim or engagement row.
            await run.tool_repository.record_discovery(
                run_id=run.run_id,
                user_id=run.context.user_id,
                ig_account_id=run.context.ig_account_id,
                report=report,
            )
            return reason
        if reason is not None:
            return reason
        # Profile inspection can await navigation. Consent and lease ownership
        # may change while that read is in flight.
        stage = "policy_postcheck"
        reason = await run.tool_repository.discovery_stop_reason(run, action=action)
        if reason is not None:
            return reason
        stage = "lease_check"
        return None if await run.lease_is_live() else "lease_lost"
    except Exception as error:  # noqa: BLE001 - fail closed and preserve session cleanup
        _log_follower_policy_failure(stage, error)
        return "policy_unavailable"


def _decision(action: ActionName, generated_text: str | None) -> EngagementDecision:
    return EngagementDecision(
        engage=True,
        action=action,
        generated_text=generated_text,
        confidence=1.0,
        rationale="Hermes selected this bounded execution tool",
        model_used="engagemate-agent",
    )


def _safe_execution_result(
    status: ExecutionResultStatus, failure: str | None
) -> dict[str, str]:
    if status is ExecutionResultStatus.SENT:
        return {"status": "confirmed"}
    if status is ExecutionResultStatus.SKIPPED:
        return {"status": "denied", "reason": failure or "duplicate_story"}
    if failure in {"action_failed", "repository_failed"}:
        return {"status": "uncertain", "reason": failure}
    return {"status": "failed", "reason": failure or status.value}


def _gateway_safe_result(value: Any) -> Any:
    """Keep Hermes' failure-token heuristic out of successful MCP tool calls."""
    if isinstance(value, str):
        return value.replace("failed", "error")
    if isinstance(value, dict):
        return {
            (key.replace("failed", "error") if isinstance(key, str) else key):
            _gateway_safe_result(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_gateway_safe_result(item) for item in value]
    return value


def _bounded_opportunity(
    opaque_id: UUID,
    opportunity: StoryOpportunity,
    run: ActiveRun,
) -> dict[str, Any]:
    caption = (opportunity.content.caption_text or "")[:4_000]
    sticker = (opportunity.content.sticker_text or "")[:1_000]
    return {
        "opportunity_id": str(opaque_id),
        "target": {
            "username": opportunity.target_handle,
            "tier": opportunity.tier,
        },
        "taken_at": opportunity.taken_at.isoformat(),
        "story": {
            "media_type": opportunity.content.media_type.value,
            "caption": caption,
            "sticker": sticker,
            "media": [],
        },
        "prior_contact": run.context.history.recent_interaction_summary[:1_000],
        "allowed_actions": _allowed_actions(run),
    }


def _discovery_result(
    metadata: dict[str, Any],
    images: list[ImageContent],
) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(text=json.dumps(metadata)), *images],
        structured_content=metadata,
    )


def _bounded_media(opportunity: StoryOpportunity) -> list[StoryMedia]:
    media: list[StoryMedia] = []
    if opportunity.content.image is not None:
        media.append(opportunity.content.image)
    media.extend(opportunity.content.video_frames[:4])
    return [
        item
        for item in media
        if item.mime_type in {"image/jpeg", "image/png", "image/webp", "image/gif"}
        and 0 < len(item.data) <= 512 * 1024
    ]


_GENERIC_REPLY_WORDS = frozenset({
    "amazing", "awesome", "context", "cool", "curious", "energy", "great",
    "hello", "here", "interesting", "love", "moment", "nice", "prompted",
    "really", "share", "story", "this", "what", "where", "wonderful",
})


def _specific_words(value: str) -> set[str]:
    return {
        word for word in re.findall(r"[a-z0-9]+", value.casefold())
        if len(word) >= 4 and word not in _GENERIC_REPLY_WORDS
    }


def _story_reply_grounded(
    opportunity: StoryOpportunity, text: str | None, detail: str | None
) -> bool:
    """Require a concrete detail shared by the observation and proposed reply."""
    if not text or not detail:
        return False
    normalized = " ".join(re.findall(r"[a-z0-9]+", text.casefold()))
    if any(phrase in normalized for phrase in (
        "what s the context here", "whats the context here",
        "curious what prompted this moment", "love the energy here",
    )):
        return False
    detail_words = _specific_words(detail)
    if not detail_words or not detail_words.intersection(_specific_words(text)):
        return False
    evidence = " ".join(
        filter(None, (opportunity.content.caption_text, opportunity.content.sticker_text))
    )
    if detail_words.intersection(_specific_words(evidence)):
        return True
    # A visual detail is permitted only if an actual image/frame reached discover.
    return bool(_bounded_media(opportunity))


def _allowed_actions(run: ActiveRun) -> list[str]:
    # remaining_for folds in the plan total and the shared private-message
    # ceiling, so an action offered here is one the budget will actually let
    # the agent spend rather than one that raises on consume.
    if getattr(run, "view_only", False):
        return ["view"] if run.budget.remaining_for(Action.VIEW) > 0 else []
    return [
        action.value
        for action in (Action.VIEW, Action.LIKE, Action.REPLY)
        if run.budget.remaining_for(action) > 0
    ]


def _budget_dict(run: ActiveRun) -> dict[str, int]:
    return {
        "views": run.budget.remaining_for(Action.VIEW),
        "likes": run.budget.remaining_for(Action.LIKE),
        "replies": run.budget.remaining_for(Action.REPLY),
        "dms": run.budget.remaining_for(Action.DM),
        "private_messages": run.budget.private_messages,
        "engagements": run.budget.engagements,
    }


def _denied(reason: str) -> dict[str, str]:
    return {"status": "denied", "reason": reason}
