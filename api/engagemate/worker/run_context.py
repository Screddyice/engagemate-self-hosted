"""Assemble one tenant-verified run context before any secret is decrypted.

Every row is read in a single read-only transaction and checked against the
claimed job's tenant. Session plaintext is produced last, so a mismatched or
unrunnable account never reaches the cipher.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID

from engagemate.browser.context import DecryptedCookieJar
from engagemate.db.proxies import ProxyBindingError
from engagemate.browser.fingerprint import (
    CapturedBrowserSession,
    Fingerprint,
    FingerprintAccount,
    FingerprintFactory,
)
from engagemate.domain.budgets import (
    AccountBudgetState,
    ActionBudget,
    AgentConfigLimits,
    BudgetPolicy,
    DailyActionUsage,
)
from engagemate.domain.models import VIEW_SLOT_COUNT, ClaimedJob, is_view_job
from engagemate.domain.session_policy import INSTAGRAM_SESSION_MAX_AGE
from engagemate.intelligence.schemas import IntelligenceHistory, Persona
from engagemate.proxy.base import Geo, ProxyEndpoint
from engagemate.worker.story_types import DiscoveryTarget

_RUNNABLE_HEALTH = frozenset({"active", "degraded"})


class RunContextError(RuntimeError):
    """A bounded, non-secret reason why a run cannot proceed."""

    def __init__(self, reason: str, *, pause_account: bool = False) -> None:
        self.reason = reason
        self.pause_account = pause_account
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class AccountRow:
    """Account, geography, persona, and plan limits for one tenant."""

    user_id: UUID
    ig_account_id: UUID
    username: str
    health: str
    paused_at: datetime | None
    warmup_started_at: datetime | None
    last_restricted_at: datetime | None
    geo_country: str
    geo_region: str
    geo_city: str
    persona_text: str
    plan: str
    daily_like_limit: int
    daily_reply_limit: int
    daily_dm_limit: int
    daily_view_limit: int
    banned_phrases: tuple[str, ...]
    expected_language: str
    config_version: int
    config_snapshot: Mapping[str, object]
    follow_after_engagement: bool = False
    follow_reel_message_enabled: bool = False
    daily_reel_message_limit: int | None = None
    warmup_exempt: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class SessionRow:
    """The encrypted session envelope and the browser inputs captured with it."""

    user_id: UUID
    ig_account_id: UUID
    ciphertext: str
    captured_user_agent: str
    captured_at: datetime
    expires_at: datetime | None

    def __repr__(self) -> str:
        return (
            "SessionRow("
            f"user_id={self.user_id!r}, "
            f"ig_account_id={self.ig_account_id!r}, "
            f"captured_at={self.captured_at!r}, "
            f"expires_at={self.expires_at!r}, ciphertext=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class TargetRow:
    """One configured Instagram target."""

    user_id: UUID
    ig_account_id: UUID
    id: UUID
    owner_id: str
    handle: str
    tier: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class UsageRow:
    """Today's recorded action quantities for one account."""

    user_id: UUID
    ig_account_id: UUID
    likes: int
    replies: int
    dms: int
    views: int = 0
    reel_messages: int = 0


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """Bounded interaction history used for routing and duplicate context."""

    user_id: UUID
    ig_account_id: UUID
    recent_interaction_summary: str
    sent_messages: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RunContextRows:
    """Every row required for one run, read in a single transaction."""

    account: AccountRow
    session: SessionRow
    targets: tuple[TargetRow, ...]
    usage: UsageRow
    history: HistoryRow

    def __repr__(self) -> str:
        return f"RunContextRows(account={self.account.ig_account_id!r}, rows=<loaded>)"


@dataclass(frozen=True, slots=True, repr=False)
class RunContext:
    """The account-bound values one worker run is allowed to touch."""

    user_id: UUID
    ig_account_id: UUID
    username: str
    geo: Geo
    endpoint: ProxyEndpoint
    fingerprint: Fingerprint
    cookies: DecryptedCookieJar
    targets: tuple[DiscoveryTarget, ...]
    budget: ActionBudget
    persona: Persona
    history: IntelligenceHistory
    banned_phrases: tuple[str, ...]
    expected_language: str
    pacing_seed: str
    config_version: int
    config_snapshot: Mapping[str, object]
    view_only: bool = False

    def __repr__(self) -> str:
        return (
            "RunContext("
            f"user_id={self.user_id!r}, "
            f"ig_account_id={self.ig_account_id!r}, "
            f"username={self.username!r}, "
            f"geo={self.geo!r}, "
            f"endpoint={self.endpoint!r}, "
            f"targets={len(self.targets)}, "
            f"budget={self.budget!r}, "
            "persona=<redacted>, history=<redacted>, cookies=<redacted>)"
        )


class RunContextReader(Protocol):
    """One read-only transaction returning every row for a tenant."""

    async def read(self, user_id: UUID, ig_account_id: UUID) -> RunContextRows: ...


class SessionCipherPort(Protocol):
    def decrypt(self, token: str) -> Mapping[str, Any]: ...


class SessionCipherFactory(Protocol):
    """Build the cipher for one account.

    Session envelopes are AES-256-GCM with the account ID as additional
    authenticated data, so a cipher is only valid for the account it was built
    for. Sharing one cipher across accounts would defeat that binding.
    """

    def __call__(self, ig_account_id: UUID) -> SessionCipherPort: ...


class ProxyBindingPort(Protocol):
    def ensure_binding(
        self, user_id: UUID, ig_account_id: UUID, geo: Geo, *, job_id: UUID | None = None
    ) -> ProxyEndpoint: ...


class RunContextLoader:
    """Verify tenancy and runnability, then produce one decrypted context."""

    def __init__(
        self,
        *,
        reader: RunContextReader,
        cipher_for: SessionCipherFactory,
        fingerprints: FingerprintFactory,
        proxy_bindings: ProxyBindingPort,
        clock: Callable[[], datetime] | None = None,
        max_session_age: timedelta = INSTAGRAM_SESSION_MAX_AGE,
    ) -> None:
        if type(max_session_age) is not timedelta or max_session_age <= timedelta(0):
            raise ValueError("max_session_age must be positive")
        self._reader = reader
        self._cipher_for = cipher_for
        self._fingerprints = fingerprints
        self._proxy_bindings = proxy_bindings
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_session_age = max_session_age

    def __repr__(self) -> str:
        return "RunContextLoader(reader=<configured>, cipher=<configured>)"

    async def load(self, job: ClaimedJob) -> RunContext:
        """Return the run context for a claimed job, or fail closed."""

        if type(job.user_id) is not UUID or type(job.ig_account_id) is not UUID:
            raise RunContextError("invalid_job_tenant")

        rows = await self._read(job)
        self._require_matching_tenant(rows, job)
        self._require_runnable_account(rows.account)
        now = self._require_now()
        self._require_live_session(rows.session, now)

        geo = self._geo(rows.account)
        endpoint = self._bind_proxy(job, geo)
        fingerprint = self._fingerprint(job.ig_account_id, geo, rows.session)
        budget = self._budget(rows.account, rows.usage, now)
        view_only = is_view_job(job.schedule_key)
        if view_only:
            # View slots have their own daily ceiling and must never consume
            # the engagement lane, even if Hermes asks for another action.
            budget = replace(
                budget,
                likes=0,
                replies=0,
                dms=0,
                plan_total=0,
                private_messages=0,
                reel_messages=0,
                views=min(
                    budget.views,
                    (rows.account.daily_view_limit + VIEW_SLOT_COUNT - 1)
                    // VIEW_SLOT_COUNT,
                ),
            )
        persona, history = self._intelligence_inputs(rows)
        targets = self._targets(rows.targets)

        # Plaintext is produced last so no earlier failure can leak it.
        cookies = self._decrypt(rows.session, job.ig_account_id)

        return RunContext(
            user_id=job.user_id,
            ig_account_id=job.ig_account_id,
            username=rows.account.username,
            geo=geo,
            endpoint=endpoint,
            fingerprint=fingerprint,
            cookies=cookies,
            targets=targets,
            budget=budget,
            persona=persona,
            history=history,
            banned_phrases=rows.account.banned_phrases,
            expected_language=rows.account.expected_language,
            pacing_seed=_pacing_seed(job.ig_account_id, fingerprint),
            config_version=rows.account.config_version,
            config_snapshot=dict(rows.account.config_snapshot),
            view_only=view_only,
        )

    async def _read(self, job: ClaimedJob) -> RunContextRows:
        try:
            rows = await self._reader.read(job.user_id, job.ig_account_id)
        except RunContextError:
            raise
        # The reader is a narrow database port with no shared error base.
        except Exception:  # noqa: BLE001
            raise RunContextError("context_read_failed") from None
        if type(rows) is not RunContextRows:
            raise RunContextError("context_read_failed")
        return rows

    def _require_matching_tenant(self, rows: RunContextRows, job: ClaimedJob) -> None:
        owners: list[tuple[Any, Any]] = [
            (rows.account.user_id, rows.account.ig_account_id),
            (rows.session.user_id, rows.session.ig_account_id),
            (rows.usage.user_id, rows.usage.ig_account_id),
            (rows.history.user_id, rows.history.ig_account_id),
        ]
        if type(rows.targets) not in (tuple, list):
            raise RunContextError("tenant_mismatch")
        owners.extend(
            (target.user_id, target.ig_account_id) for target in rows.targets
        )
        for user_id, ig_account_id in owners:
            if user_id != job.user_id or ig_account_id != job.ig_account_id:
                raise RunContextError("tenant_mismatch")

    def _require_runnable_account(self, account: AccountRow) -> None:
        if account.paused_at is not None:
            raise RunContextError("account_paused")
        if account.health not in _RUNNABLE_HEALTH:
            raise RunContextError("account_unhealthy")

    def _require_now(self) -> datetime:
        try:
            now = self._clock()
        # An injected clock is a narrow dependency with no shared error base.
        except Exception:  # noqa: BLE001
            raise RunContextError("invalid_clock") from None
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise RunContextError("invalid_clock")
        return now.astimezone(timezone.utc)

    def _require_live_session(self, session: SessionRow, now: datetime) -> None:
        captured_at = _as_utc(session.captured_at)
        if captured_at is None:
            raise RunContextError("session_expired", pause_account=True)
        if now - captured_at > self._max_session_age:
            raise RunContextError("session_expired", pause_account=True)
        if session.expires_at is not None:
            expires_at = _as_utc(session.expires_at)
            if expires_at is None or expires_at <= now:
                raise RunContextError("session_expired", pause_account=True)

    def _geo(self, account: AccountRow) -> Geo:
        try:
            return Geo(account.geo_country, account.geo_region, account.geo_city)
        except ValueError:
            raise RunContextError("invalid_account_geography") from None

    def _bind_proxy(self, job: ClaimedJob, geo: Geo) -> ProxyEndpoint:
        try:
            endpoint = self._proxy_bindings.ensure_binding(
                job.user_id, job.ig_account_id, geo, job_id=job.id
            )
        except ProxyBindingError as error:
            # Temporary provider or CONNECT failures should be retried by the
            # scheduler without taking a healthy account offline. Permanent
            # binding failures still fail closed and pause the account.
            if error.transient:
                raise RunContextError("proxy_transient", pause_account=False) from None
            raise RunContextError("proxy_unhealthy", pause_account=True) from None
        # Provider and repository errors can carry proxy credentials.
        except Exception:  # noqa: BLE001
            raise RunContextError("proxy_unhealthy", pause_account=True) from None
        if type(endpoint) is not ProxyEndpoint:
            raise RunContextError("proxy_unhealthy", pause_account=True)
        if endpoint.geo != geo:
            raise RunContextError("proxy_geo_mismatch", pause_account=True)
        return endpoint

    def _fingerprint(
        self, ig_account_id: UUID, geo: Geo, session: SessionRow
    ) -> Fingerprint:
        try:
            return self._fingerprints.for_account(
                FingerprintAccount(ig_account_id, geo),
                CapturedBrowserSession(session.captured_user_agent),
            )
        except (ValueError, KeyError):
            raise RunContextError("fingerprint_unavailable") from None

    def _budget(
        self, account: AccountRow, usage: UsageRow, now: datetime
    ) -> ActionBudget:
        try:
            budget = BudgetPolicy.remaining(
                AccountBudgetState(
                    account.warmup_started_at, account.last_restricted_at
                ),
                AgentConfigLimits(
                    account.plan,
                    account.daily_like_limit,
                    account.daily_reply_limit,
                    account.daily_dm_limit,
                    account.daily_view_limit,
                    daily_reel_message_limit=(account.daily_reel_message_limit
                        if account.follow_after_engagement and account.follow_reel_message_enabled
                        else None),
                    warmup_exempt=account.warmup_exempt,
                ),
                DailyActionUsage(
                    usage.likes, usage.replies, usage.dms, usage.views,
                    reel_messages=usage.reel_messages,
                ),
                now,
            )
            # A full Story run must leave room for its first follow-up reel.
            # Existing DM reservations already reduce the shared budget, so
            # reserve only before any message slot has been used by that lane.
            if (account.daily_reel_message_limit is None
                    and account.follow_after_engagement and account.follow_reel_message_enabled
                    and usage.dms == 0 and (budget.likes > 0 or budget.replies > 1)
                    and budget.replies > 0 and budget.private_messages > 0):
                budget = replace(budget, replies=budget.replies - 1,
                                 private_messages=budget.private_messages - 1)
            return budget
        except Exception:  # noqa: BLE001 - budget errors share no public base
            raise RunContextError("invalid_budget_configuration") from None

    def _intelligence_inputs(
        self, rows: RunContextRows
    ) -> tuple[Persona, IntelligenceHistory]:
        try:
            persona = Persona(rows.account.persona_text)
            history = IntelligenceHistory(
                rows.history.recent_interaction_summary,
                rows.history.sent_messages,
            )
        except ValueError:
            raise RunContextError("invalid_intelligence_inputs") from None
        return persona, history

    def _targets(self, rows: tuple[TargetRow, ...]) -> tuple[DiscoveryTarget, ...]:
        return tuple(
            DiscoveryTarget(
                id=row.id,
                user_id=row.user_id,
                ig_account_id=row.ig_account_id,
                owner_id=row.owner_id,
                handle=row.handle,
                tier=row.tier,
                enabled=True,
            )
            for row in rows
            if row.enabled
        )

    def _decrypt(
        self, session: SessionRow, ig_account_id: UUID
    ) -> DecryptedCookieJar:
        payload: Any = None
        try:
            payload = self._cipher_for(ig_account_id).decrypt(session.ciphertext)
            if type(payload) is not dict:
                raise RunContextError("session_undecryptable", pause_account=True)
            return DecryptedCookieJar.from_payload(payload)
        except RunContextError:
            raise
        # Cipher and payload failures must never surface ciphertext or cookies.
        except Exception:  # noqa: BLE001
            raise RunContextError("session_undecryptable", pause_account=True) from None
        finally:
            payload = None


def _pacing_seed(ig_account_id: UUID, fingerprint: Fingerprint) -> str:
    """Derive a private pacing seed that is never reused as a render seed."""

    return hashlib.sha256(
        b"engagemate:pacing-seed:v1\0"
        + str(ig_account_id).encode("ascii")
        + b"\0"
        + fingerprint.canvas_seed.encode("ascii")
    ).hexdigest()


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
