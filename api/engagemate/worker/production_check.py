"""Explicit, scoped production checks using saved settings and normal receipts."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

from engagemate.domain.models import Action, JobStatus
from engagemate.worker.canary import CanaryError, CanaryScope, _run_scoped_job
from engagemate.worker.follow_store import FollowStoreScope, PendingLikeFollowScope, pending_like_message


async def run_reply(scope: CanaryScope, *, timeout_seconds: int = 300):
    """Run one fresh exact job with at most one new-prospect Story reply."""
    from engagemate.config import Settings
    from engagemate.db.pool import create_pool

    summary = await _run_scoped_job(
        scope, timeout_seconds=timeout_seconds, production_reply_check=True,
    )
    result = {"summary": asdict(summary) if summary is not None else None,
              "selected_action": "reply", "receipt": None, "verified": False}
    if summary is not None and summary.status is JobStatus.COMPLETED:
        pool = create_pool(Settings().database_url, min_size=1, max_size=2)
        try:
            result["receipt"] = await asyncio.to_thread(_selected_reply_receipt, pool, scope)
            result["verified"] = result["receipt"] is not None
        finally:
            pool.close()
    return result


def _selected_reply_receipt(pool, scope: CanaryScope):
    """Audit the completed selected reply, its native receipt and billing."""
    with pool.connection() as connection:
        rows = connection.execute(
            """SELECT r.id AS run_id, tc.id AS tool_call_id, e.id AS engagement_id,
                      tc.target_id, tc.opportunity_id, tc.story_fingerprint, e.executed_at,
                      tc.proposed_text, tc.generated_text_hash
               FROM jobs j JOIN hermes_execution_runs r
                 ON r.job_id=j.id AND r.user_id=j.user_id AND r.ig_account_id=j.ig_account_id
               JOIN hermes_tool_calls tc ON tc.run_id=r.id
               JOIN story_engagements e ON e.user_id=r.user_id AND e.ig_account_id=r.ig_account_id
                 AND e.target_id=tc.target_id AND e.story_fingerprint=tc.story_fingerprint
                 AND e.action=tc.tool_name
               JOIN targets t ON t.id=e.target_id AND t.user_id=e.user_id AND t.ig_account_id=e.ig_account_id
               JOIN run_summaries s ON s.job_id=j.id AND s.user_id=j.user_id AND s.ig_account_id=j.ig_account_id
               JOIN ig_accounts a ON a.id=j.ig_account_id AND a.user_id=j.user_id
               WHERE j.id=%(job_id)s AND j.user_id=%(user_id)s AND j.ig_account_id=%(account_id)s
                 AND j.status='completed' AND j.attempts=1 AND j.claimed_by=%(worker_id)s
                 AND r.status='completed' AND r.completed_at IS NOT NULL
                 AND r.config_snapshot->'production_check'->>'job_id'=%(job_text)s
                 AND r.config_snapshot->'production_check'->>'worker_id'=%(worker_id)s
                 AND r.config_snapshot->'production_check'->>'execution_mode'='autonomous'
                 AND r.config_snapshot->'production_check'->'reply_only'='true'::jsonb
                 AND r.config_snapshot->'production_check'->'new_prospect_only'='true'::jsonb
                 AND tc.tool_name='reply' AND tc.status='confirmed'
                 AND tc.safe_result->>'status'='confirmed' AND tc.opportunity_id IS NOT NULL
                 AND tc.safe_result->>'evidence' ~
                   '^action=reply,stage=[a-z_]+,request=forwarded,receipt=accepted,http=200,health=[a-z_]+(,cleanup=failed)?$'
                 AND tc.proposed_text IS NOT NULL
                 AND tc.generated_text_hash ~ '^[0-9a-f]{64}$'
                 AND tc.started_at>=r.started_at AND tc.completed_at<=r.completed_at
                 AND e.status='sent' AND e.generated_text IS NOT NULL
                 AND e.generated_text=tc.proposed_text
                 AND e.executed_at BETWEEN tc.started_at AND tc.completed_at
                 AND s.completed_at IS NOT NULL AND s.likes_sent=0
                 AND s.replies_sent=1 AND s.dms_sent=0 AND s.failed=0
                 AND (SELECT count(*) FROM run_summaries WHERE job_id=j.id)=1
                 AND (SELECT count(*) FROM hermes_tool_calls WHERE run_id=r.id)=1
                 AND a.health_status='active' AND a.paused_at IS NULL
                 AND (SELECT count(*) FROM engagement_usage_events WHERE engagement_id=e.id)=1
                 AND EXISTS (SELECT 1 FROM engagement_usage_events u
                   WHERE u.engagement_id=e.id AND u.user_id=e.user_id AND u.ig_account_id=e.ig_account_id
                     AND u.action='reply' AND u.surface='story' AND u.status='billable'
                     AND u.finalized_at IS NOT NULL)""",
            {**asdict(scope), "job_text": str(scope.job_id)},
        ).fetchall()
    if len(rows) != 1:
        return None
    row = dict(rows[0])
    proposed_text = row.pop("proposed_text")
    generated_text_hash = row.pop("generated_text_hash")
    if isinstance(proposed_text, bytes):
        proposed_text = proposed_text.decode("utf-8")
    if isinstance(generated_text_hash, bytes):
        generated_text_hash = generated_text_hash.decode("ascii")
    if not isinstance(proposed_text, str) or (
        hashlib.sha256(proposed_text.encode("utf-8")).hexdigest() != generated_text_hash
    ):
        return None
    return row


def _selected_story_receipt(pool, scope: CanaryScope, action: Action):
    """Audit the completed selected write, its summary and billing without writes."""
    with pool.connection() as connection:
        rows = connection.execute(
            """SELECT r.id AS run_id, tc.id AS tool_call_id, e.id AS engagement_id,
                      tc.target_id, tc.opportunity_id, tc.story_fingerprint, e.executed_at
               FROM jobs j JOIN hermes_execution_runs r
                 ON r.job_id=j.id AND r.user_id=j.user_id AND r.ig_account_id=j.ig_account_id
               JOIN hermes_tool_calls tc ON tc.run_id=r.id
               JOIN story_engagements e ON e.user_id=r.user_id AND e.ig_account_id=r.ig_account_id
                 AND e.target_id=tc.target_id AND e.story_fingerprint=tc.story_fingerprint AND e.action=tc.tool_name
               JOIN targets t ON t.id=e.target_id AND t.user_id=e.user_id AND t.ig_account_id=e.ig_account_id
               JOIN run_summaries s ON s.job_id=j.id AND s.user_id=j.user_id AND s.ig_account_id=j.ig_account_id
               JOIN ig_accounts a ON a.id=j.ig_account_id AND a.user_id=j.user_id
               WHERE j.id=%(job_id)s AND j.user_id=%(user_id)s AND j.ig_account_id=%(account_id)s
                 AND j.status='completed' AND j.attempts=1 AND j.claimed_by=%(worker_id)s
                 AND r.status='completed' AND r.completed_at IS NOT NULL
                 AND r.config_snapshot->'production_check'->>'job_id'=%(job_text)s
                 AND r.config_snapshot->'production_check'->>'worker_id'=%(worker_id)s
                 AND r.config_snapshot->'production_check'->>'execution_mode'='autonomous'
                 AND r.config_snapshot->'production_check'->>'selected_action'=%(action)s
                 AND r.config_snapshot->'production_check'->'new_prospect_only'='true'::jsonb
                 AND tc.tool_name=%(action)s AND tc.status='confirmed'
                 AND tc.safe_result->>'status'='confirmed' AND tc.opportunity_id IS NOT NULL
                 AND tc.safe_result->>'evidence' ~ ('^action=' || %(action)s ||
                   ',stage=[a-z_]+,request=forwarded,receipt=accepted,http=200,health=[a-z_]+(,cleanup=failed)?$')
                 AND tc.proposed_text IS NULL AND tc.generated_text_hash IS NULL
                 AND tc.started_at>=r.started_at AND tc.completed_at<=r.completed_at
                 AND e.status='sent' AND e.generated_text IS NULL
                 AND e.executed_at BETWEEN tc.started_at AND tc.completed_at
                 AND s.completed_at IS NOT NULL AND s.likes_sent=%(likes)s
                 AND s.replies_sent=0 AND s.dms_sent=0 AND s.failed=0
                 AND (SELECT count(*) FROM run_summaries WHERE job_id=j.id)=1
                 AND (SELECT count(*) FROM hermes_tool_calls WHERE run_id=r.id)=1
                 AND a.health_status='active' AND a.paused_at IS NULL
                 AND (SELECT count(*) FROM engagement_usage_events WHERE engagement_id=e.id)=%(likes)s
                 AND (%(likes)s=0 OR EXISTS (SELECT 1 FROM engagement_usage_events u
                   WHERE u.engagement_id=e.id AND u.user_id=e.user_id AND u.ig_account_id=e.ig_account_id
                     AND u.action='like' AND u.surface='story' AND u.status='billable'
                     AND u.finalized_at IS NOT NULL))""",
            {**asdict(scope), "job_text": str(scope.job_id), "action": action.value,
             "likes": int(action is Action.LIKE)},
        ).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


async def run_story(scope: CanaryScope, action: Action, *, timeout_seconds: int = 300):
    """Run one fresh exact VIEW or LIKE check; success requires its saved receipt."""
    from engagemate.config import Settings
    from engagemate.db.pool import create_pool
    if type(scope) is not CanaryScope or type(action) is not Action or action not in (Action.VIEW, Action.LIKE):
        raise CanaryError("invalid_production_story_action")
    identity = {}
    summary = await _run_scoped_job(
        scope, timeout_seconds=timeout_seconds, production_story_action=action, story_report=identity,
    )
    result = {"summary": asdict(summary) if summary is not None else None,
              "selected_action": action.value, "receipt": None, "verified": False, "story_identity": None}
    if summary is not None and summary.status is JobStatus.COMPLETED:
        pool = create_pool(Settings().database_url, min_size=1, max_size=2)
        try:
            result["receipt"] = await asyncio.to_thread(_selected_story_receipt, pool, scope, action)
            result["verified"] = result["receipt"] is not None
            if result["verified"] and all(identity.get(key) == result["receipt"][key]
                                           for key in ("target_id", "opportunity_id", "story_fingerprint")):
                result["story_identity"] = identity
        finally:
            pool.close()
    return result


async def discard_job(scope: CanaryScope):
    from engagemate.config import Settings
    from engagemate.db.jobs import JobRepository
    from engagemate.db.pool import create_pool
    if type(scope) is not CanaryScope:
        raise CanaryError("invalid_job_scope")
    pool = create_pool(Settings().database_url, min_size=1, max_size=2)
    try:
        jobs = JobRepository(pool)
        job = await asyncio.to_thread(jobs.claim_exact, scope.job_id, scope.user_id,
                                      scope.account_id, scope.worker_id)
        if job is None:
            raise CanaryError("fresh_job_unavailable_for_discard")
        if not await asyncio.to_thread(jobs.finish, job.id, scope.worker_id,
                                      JobStatus.CANCELLED, "operator_check_not_started"):
            raise CanaryError("discard_lease_lost")
        return {"job_id": str(job.id), "status": "cancelled"}
    finally:
        pool.close()


def _require_new_reply(pool, scope, now):
    with pool.connection() as connection:
        row = connection.execute(
            """SELECT e.id FROM story_engagements e
               JOIN targets t ON t.id=e.target_id AND t.user_id=e.user_id AND t.ig_account_id=e.ig_account_id
               JOIN hermes_tool_calls tc ON tc.target_id=e.target_id
                 AND tc.story_fingerprint=e.story_fingerprint AND tc.tool_name=e.action
               JOIN hermes_execution_runs r ON r.id=tc.run_id AND r.user_id=e.user_id AND r.ig_account_id=e.ig_account_id
               JOIN jobs j ON j.id=r.job_id AND j.user_id=r.user_id AND j.ig_account_id=r.ig_account_id
               WHERE e.id=%s AND e.user_id=%s AND e.ig_account_id=%s AND e.action='reply'
                 AND e.status='sent' AND tc.status='confirmed' AND r.status='completed' AND j.status='completed'
                 AND e.executed_at BETWEEN %s-interval '30 minutes' AND %s
                 AND NOT EXISTS (SELECT 1 FROM relationship_follows f
                   WHERE f.user_id=e.user_id AND f.ig_account_id=e.ig_account_id AND f.owner_id=t.ig_user_id)
               LIMIT 1""",
            (scope.prerequisite_engagement_id, scope.user_id, scope.account_id, now, now),
        ).fetchone()
    if row is None:
        raise CanaryError("fresh_confirmed_new_reply_required")


def _require_pending_like(pool, scope, now):
    """Select an existing pending first follow and its unclaimed reservation."""
    with pool.connection() as connection:
        message_id = pending_like_message(connection,scope,now)
    if message_id is None:
        raise CanaryError("confirmed_like_pending_first_follow_required")
    return message_id


def _new_message(pool, scope, relationship_id, since, *, expected_message_id=None):
    pending_like = type(scope) is PendingLikeFollowScope
    reservation_since = since-timedelta(hours=24) if pending_like else since
    with pool.connection() as connection:
        row = connection.execute(
            """SELECT m.id FROM follow_reel_messages m JOIN relationship_follows f
                 ON f.id=m.relationship_follow_id AND f.user_id=m.user_id AND f.ig_account_id=m.ig_account_id
               WHERE f.id=%s AND f.user_id=%s AND f.ig_account_id=%s
                 AND f.prerequisite_engagement_id=%s AND f.status='following'
                 AND f.follow_attempted_at>=%s AND f.confirmed_follow_at>=%s
                 AND m.status='pending' AND m.attempted_at IS NULL AND m.claim_count=0
                 AND m.created_at>=%s AND m.reserved_at>=%s
                 AND (%s::uuid IS NULL OR m.id=%s)""",
            (relationship_id, scope.user_id, scope.account_id, scope.prerequisite_engagement_id,
             since, since, reservation_since, reservation_since,
             expected_message_id, expected_message_id),
        ).fetchone()
    if row is None:
        raise CanaryError("new_unattempted_reel_reservation_required")
    return row["id"]


async def run_follow_reel(scope: FollowStoreScope | PendingLikeFollowScope, *, worker_id: str):
    """Follow once from an exact confirmed prerequisite, then send once."""
    from playwright.async_api import async_playwright
    from deploy.healthcheck import validate_worker_environment
    from engagemate.config import Settings
    from engagemate.db.pool import create_pool
    from engagemate.worker.follow_maintenance import FollowMaintenance
    from engagemate.worker.follow_reel_maintenance import ReelMessageMaintenance
    from engagemate.worker.follow_reel_runtime import LiveFollowReelPerformer
    from engagemate.worker.follow_reel_store import PostgresFollowReelStore, ReelMessageStoreScope
    from engagemate.worker.follow_runtime import LiveFollowPerformer
    from engagemate.worker.follow_store import PostgresFollowStore

    if type(scope) not in {FollowStoreScope, PendingLikeFollowScope}:
        raise CanaryError("invalid_follow_reel_scope")
    CanaryScope(scope.user_id, scope.account_id, scope.prerequisite_engagement_id, worker_id)
    validate_worker_environment(os.environ)
    settings = Settings()
    if settings.instagram_execution_mode != "autonomous":
        raise CanaryError("production_check_requires_saved_autonomous_mode")
    pool = create_pool(settings.database_url, min_size=1, max_size=4)
    try:
        started = datetime.now(UTC)
        pending_like = type(scope) is PendingLikeFollowScope
        message_id = await asyncio.to_thread(_require_pending_like, pool, scope, started) if pending_like else None
        if not pending_like:
            await asyncio.to_thread(_require_new_reply, pool, scope, started)
        async with async_playwright() as playwright:
            follow = await FollowMaintenance(
                PostgresFollowStore(pool, scope=scope), LiveFollowPerformer(pool, settings, playwright),
                execution_mode=settings.instagram_execution_mode,
            ).run_once(worker_id)
            result = {"follow": asdict(follow) if follow is not None else None, "reel": None}
            if pending_like:
                result["prerequisite_action"] = "like"
            if follow is None or follow.status is not JobStatus.COMPLETED or follow.engaged != 1 or follow.stop_reason != "follow_confirmed":
                return result
            message_id = await asyncio.to_thread(
                _new_message, pool, scope, follow.job_id, started, expected_message_id=message_id,
            )
            reel = await ReelMessageMaintenance(
                PostgresFollowReelStore(pool, scope=ReelMessageStoreScope(
                    scope.user_id, scope.account_id, message_id=message_id,
                    first_attempt_only=pending_like,
                )), LiveFollowReelPerformer(pool, settings, playwright),
                execution_mode=settings.instagram_execution_mode,
            ).run_once(worker_id)
            result["reel"] = asdict(reel) if reel is not None else None
            return result
    finally:
        pool.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("reply", "view", "like", "follow-reel", "follow-reel-like", "discard"):
        command = subcommands.add_parser(name)
        command.add_argument("--user-id", required=True, type=UUID)
        command.add_argument("--account-id", required=True, type=UUID)
        command.add_argument("--worker-id", required=True)
        if name not in {"follow-reel", "follow-reel-like"}:
            command.add_argument("--job-id", required=True, type=UUID)
        else:
            command.add_argument("--engagement-id", required=True, type=UUID)
            if name == "follow-reel-like":
                command.add_argument("--relationship-id", required=True, type=UUID)
        if name in ("reply", "view", "like"):
            command.add_argument("--timeout-seconds", default=300, type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "reply":
            result = asyncio.run(run_reply(CanaryScope(
                args.user_id, args.account_id, args.job_id, args.worker_id, reply_only=True,
            ), timeout_seconds=args.timeout_seconds))
            ok = result["verified"] is True
        elif args.command in ("view", "like"):
            result = asyncio.run(run_story(CanaryScope(
                args.user_id, args.account_id, args.job_id, args.worker_id,
            ), Action(args.command), timeout_seconds=args.timeout_seconds))
            ok = result["verified"] is True
        elif args.command == "discard":
            result = asyncio.run(discard_job(CanaryScope(
                args.user_id, args.account_id, args.job_id, args.worker_id,
            )))
            ok = True
        else:
            selected = PendingLikeFollowScope(args.user_id,args.account_id,args.engagement_id,args.relationship_id) if args.command == "follow-reel-like" else FollowStoreScope(args.user_id,args.account_id,args.engagement_id)
            result = asyncio.run(run_follow_reel(selected, worker_id=args.worker_id))
            ok = all(result.get(lane) and result[lane]["status"] == JobStatus.COMPLETED
                     and result[lane]["engaged"] == 1 for lane in ("follow", "reel"))
    except CanaryError as error:
        print(f"production check refused: {error}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - browser and provider errors may contain credentials
        print("production check failed; inspect the exact job and action ledgers", file=sys.stderr)
        return 2
    print(json.dumps(result, default=str, sort_keys=True))
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
