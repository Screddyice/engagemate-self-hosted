"""Bounded, resumable traversal of an approved inspiration-account audience."""

from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from engagemate.worker.discovery import DiscoveryError


def prospect_reason(profile: dict[str, Any], owner_id: str, excluded: set[str]) -> str:
    """Only explicit, identity-matched negative relationships admit a prospect."""
    if profile.get("owner_id") != owner_id:
        return "identity_mismatch"
    handle = profile.get("handle")
    if not isinstance(handle, str) or handle.lower() in excluded:
        return "excluded"
    if profile.get("following") is True or profile.get("followed_by") is True:
        return "excluded"
    if profile.get("private") is True:
        return "excluded"
    if any(
        profile.get(key) is not False for key in ("following", "followed_by", "private")
    ):
        return "unknown"
    if profile.get("has_story") is False:
        return "no_story"
    return "available" if profile.get("has_story") is True else "unknown"


# One prospect's Story can disappear or fail to load between the profile check
# and the Story read. That prospect is skipped. This many failures in a row,
# with no Instagram restriction detected, still stops discovery: at that point
# the browser session is the likelier fault.
MAX_CONSECUTIVE_STORY_FAILURES = 3
# Hermes enforces a bounded MCP tool-call deadline in production. Keep a
# normal follower scan comfortably inside that deadline so a slow Instagram
# page cannot hold the worker lane long enough to starve relationship and Reel
# maintenance. The scan remains resumable through the persisted source cursor.
DEFAULT_MAX_SECONDS = 300.0


class FollowerDiscovery:
    """Keep looking past empty profiles; never infer completion from an empty page.

    The repository commits a fetched page and its cursor together. This object
    owns per-run ceilings, pacing and delivered-story dedupe. Repeated discover
    calls drain the same queue rather than start again at the first source.
    """

    def __init__(
        self,
        *,
        browser: Any,
        store: Any,
        stories: Any,
        max_checks: int = 150,
        max_pages: int = 15,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        delay_seconds: float = 5,
        sleep: Any = asyncio.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.browser, self.store, self.stories = browser, store, stories
        self.max_checks, self.max_pages = max_checks, max_pages
        self.max_seconds, self.delay_seconds = max_seconds, delay_seconds
        self.sleep, self.monotonic = sleep, monotonic
        self.started = monotonic()
        self.counters: Counter[str] = Counter()
        self.pending: deque = deque()
        self.delivered: set[str] = set()
        self.checked: set[str] = set()
        self.source_offset = 0
        self.story_failures = 0
        self.stop_reason: str | None = None
        self.lock = asyncio.Lock()

    async def find(self, run: Any, limit: int) -> list:
        async with self.lock:
            try:
                deadline = getattr(run, "check_deadline", None)
                if deadline is None:
                    return await self._find(run, limit)
                deadline.record("discovery_started")
                try:
                    remaining = deadline.discovery_remaining()
                    if remaining <= 0:
                        self.stop_reason = "time_limit"
                        return []
                    async with asyncio.timeout(remaining):
                        return await self._find(run, limit)
                except TimeoutError:
                    self.stop_reason = "time_limit"
                    return []
                finally:
                    deadline.record("discovery_finished")
            except Exception:  # noqa: BLE001 - fail closed and preserve session cleanup
                # A storage failure must produce a terminal report rather than
                # leave the agent retrying a half-finished discovery phase.
                self.stop_reason = "discovery_unavailable"
                return []

    async def _find(self, run: Any, limit: int) -> list:
        sources = run.config_snapshot.get("follower_sources", [])
        if not isinstance(sources, list) or not sources or len(sources) > 5:
            self.stop_reason = "source_approval_required"
            return []
        try:
            ids = [UUID(source["id"]) for source in sources]
            if len(set(ids)) != len(ids) or any(
                not isinstance(source.get("username"), str) or not source["username"]
                for source in sources
            ):
                raise ValueError("invalid follower sources")
        except (KeyError, TypeError, ValueError, AttributeError):
            self.stop_reason = "source_approval_required"
            return []
        excluded = {
            run.context.username.lower(),
            *(s["username"].lower() for s in sources),
        }
        capacity = min(limit, run.budget.views + run.budget.engagements)
        if capacity <= 0:
            self.stop_reason = "budget_exhausted"
            return []
        if self.stop_reason is not None:
            return []
        # Start every approved source before consuming the first source's
        # queue. Without this bounded warm-up, a large first source can keep
        # the other approved sources uninitialized indefinitely: candidates
        # that already have a no-Story or relationship cooldown remain due
        # often enough to prevent the refill path from running at all.
        await self._prime_sources(run, sources)
        if self.stop_reason is not None:
            return []
        selected = []
        while len(selected) < capacity:
            if not await self._allowed(run):
                break
            if self.pending:
                opportunity = self.pending.popleft()
                if opportunity.fingerprint not in self.delivered:
                    self.delivered.add(opportunity.fingerprint)
                    selected.append(opportunity)
                continue
            if self.counters["checked"] >= self.max_checks:
                self.stop_reason = "scan_limit"
                break
            now = datetime.now(UTC)
            candidate = await self.store.next_candidate(run.discovery_context, ids, now)
            if candidate is None:
                if not await self._refill(run, sources):
                    break
                continue
            owner_id = candidate["owner_id"]
            if owner_id in self.checked:
                # A repository must respect the cooldown, but bound a broken
                # implementation too, rather than spin on one candidate.
                self.stop_reason = "candidate_progress_stalled"
                break
            self.checked.add(owner_id)
            self.counters["checked"] += 1
            await self.sleep(self.delay_seconds)
            if not await self._allowed(run):
                break
            try:
                profile = await self.browser.inspect(candidate["handle"])
                reason = prospect_reason(profile, owner_id, excluded)
            except Exception as error:  # noqa: BLE001 - explicit restrictions still stop discovery
                if getattr(error, "reason", None) == "profile_unreadable":
                    # An unreadable recipient is ineligible, not an exhausted audience.
                    # Persist its cooldown so later runs also advance past it.
                    await self.store.mark_checked(
                        run.discovery_context, owner_id, "unknown", now
                    )
                    self.counters["unknown"] += 1
                    continue
                self.stop_reason = getattr(error, "reason", "profile_unreadable")
                break
            await self.store.mark_checked(
                run.discovery_context,
                owner_id,
                "unknown" if reason == "identity_mismatch" else reason,
                now,
            )
            self.counters[reason] += 1
            if reason != "available":
                continue
            target = await self.store.admit_target(
                run.discovery_context, ids, owner_id, candidate["handle"], now
            )
            if target is None:
                self.counters["excluded"] += 1
                continue
            if not await self._allowed(run):
                break
            try:
                opportunities = await self.stories.find(
                    run.discovery_context, (target,), run.budget
                )
            except DiscoveryError as error:
                reason = getattr(error, "reason", "story_unreadable")
                if reason == "story_unavailable":
                    # An explicit expired/deleted Story is a healthy read of
                    # an ineligible prospect, not a broken browser session.
                    self.story_failures = 0
                    self.counters["story_unavailable"] += 1
                    continue
                self.story_failures += 1
                if (
                    reason == "story_unreadable"
                    and self.story_failures < MAX_CONSECUTIVE_STORY_FAILURES
                ):
                    # On 2026-09-17 one unreadable Story ended discovery with
                    # seven opportunities already found, and the run closed as
                    # safety_stop without a single like. The prospect's
                    # cooldown is already saved, so later runs move past it.
                    self.counters["story_unreadable"] += 1
                    continue
                self.stop_reason = reason
                break
            except Exception as error:  # noqa: BLE001 - fail closed and preserve session cleanup
                self.stop_reason = getattr(error, "reason", "story_unreadable")
                break
            self.story_failures = 0
            # Spread the allocation across prospects instead of filling a
            # batch with many Stories from the first prolific account.
            self.pending.extend(
                [o for o in opportunities if o.fingerprint not in self.delivered][:1]
            )
            if not opportunities:
                self.counters["no_unseen_story"] += 1
        self.counters["opportunities"] += len(selected)
        return selected

    async def _allowed(self, run: Any) -> bool:
        deadline = getattr(run, "check_deadline", None)
        if (deadline is not None and deadline.discovery_remaining() <= 0) or (
            self.monotonic() - self.started >= self.max_seconds
        ):
            self.stop_reason = "time_limit"
            return False
        if run.execution_mode == "disabled":
            self.stop_reason = "execution_disabled"
            return False
        try:
            if not await run.lease_is_live():
                self.stop_reason = "lease_lost"
                return False
            reason = await run.tool_repository.discovery_stop_reason(run)
        except Exception:  # noqa: BLE001 - fail closed and preserve session cleanup
            reason = "policy_unavailable"
        if reason:
            self.stop_reason = reason
            return False
        return True

    async def _prime_sources(self, run: Any, sources: list[dict]) -> None:
        for _ in sources:
            if not await self._allowed(run):
                return
            if await self._refill(run, sources, only_uninitialized=True):
                continue
            # No uninitialized source remains. `_refill` deliberately leaves
            # stop_reason unset for this normal completion case.
            return

    async def _refill(
        self,
        run: Any,
        sources: list[dict],
        *,
        only_uninitialized: bool = False,
    ) -> bool:
        for _ in sources:
            if self.counters["pages"] >= self.max_pages:
                self.stop_reason = "page_limit"
                return False
            source = sources[self.source_offset % len(sources)]
            self.source_offset += 1
            source_id = UUID(source["id"])
            state = await self.store.source_state(run.discovery_context, source_id)
            if only_uninitialized and state.get("initialized", True):
                continue
            if state["exhausted"]:
                continue
            await self.sleep(self.delay_seconds)
            if not await self._allowed(run):
                return False
            self.counters["pages"] += 1
            try:
                page = await self.browser.followers(source["username"], state["cursor"])
            except Exception as error:  # noqa: BLE001 - fail closed and preserve session cleanup
                self.stop_reason = getattr(error, "reason", "followers_unreadable")
                return False
            if not page["exhausted"] and page["next_cursor"] == state["cursor"]:
                self.stop_reason = "source_progress_stalled"
                return False
            saved = await self.store.save_page(
                run.discovery_context,
                source_id,
                state["cursor"],
                page["next_cursor"],
                page["exhausted"],
                page["candidates"],
            )
            if not saved:
                self.stop_reason = "source_changed"
                return False
            self.counters["sourced"] += len(page["candidates"])
            if page.get("limited"):
                self.counters["limited_sources"] += 1
            if page["candidates"] or not page["exhausted"]:
                return True
        if not only_uninitialized:
            self.stop_reason = "source_exhausted"
        return False

    def report(self) -> dict:
        return {
            "stop_reason": self.stop_reason,
            "counters": dict(self.counters),
            "can_continue": self.stop_reason is None,
        }
