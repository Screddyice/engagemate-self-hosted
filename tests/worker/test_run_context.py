"""Task 12 — tenant-verified context loading."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from engagemate.browser.fingerprint import (
    FingerprintFactory,
    FingerprintGeoRegistry,
)
from engagemate.domain.budgets import ActionBudget
from engagemate.domain.models import ClaimedJob, JobStatus
from engagemate.proxy.base import Geo, ProxyEndpoint
from engagemate.db.proxies import ProxyBindingError
from engagemate.worker.run_context import (
    AccountRow,
    HistoryRow,
    RunContextError,
    RunContextLoader,
    RunContextRows,
    SessionRow,
    TargetRow,
    UsageRow,
)

USER = UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT = UUID("00000000-0000-0000-0000-000000000002")
TARGET = UUID("00000000-0000-0000-0000-000000000003")
OTHER_USER = UUID("00000000-0000-0000-0000-0000000000ff")
OTHER_ACCOUNT = UUID("00000000-0000-0000-0000-0000000000fe")
JOB = UUID("00000000-0000-0000-0000-000000000005")
NOW = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
GEO = Geo("US", "California", "San Francisco")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
GEO_MAP = (
    '[{"country": "US", "region": "California", "city": "San Francisco",'
    ' "timezone": "America/Los_Angeles", "locale": "en-US"}]'
)


def claimed_job(user_id: UUID = USER, account_id: UUID = ACCOUNT) -> ClaimedJob:
    return ClaimedJob(
        id=JOB,
        user_id=user_id,
        ig_account_id=account_id,
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


def view_claimed_job(user_id: UUID = USER, account_id: UUID = ACCOUNT) -> ClaimedJob:
    job = claimed_job(user_id, account_id)
    return ClaimedJob(
        id=job.id,
        user_id=job.user_id,
        ig_account_id=job.ig_account_id,
        schedule_key="views:2026-07-31:12",
        scheduled_for=job.scheduled_for,
        status=job.status,
        claimed_by=job.claimed_by,
        lease_expires_at=job.lease_expires_at,
        heartbeat_at=job.heartbeat_at,
        attempts=job.attempts,
        last_error=job.last_error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def account_row(**overrides) -> AccountRow:
    values = {
        "user_id": USER,
        "ig_account_id": ACCOUNT,
        "username": "account.one",
        "health": "active",
        "paused_at": None,
        "warmup_started_at": NOW - timedelta(days=60),
        "last_restricted_at": None,
        "geo_country": "US",
        "geo_region": "California",
        "geo_city": "San Francisco",
        "persona_text": "A friendly founder who replies briefly.",
        "plan": "pro",
        "daily_like_limit": 20,
        "daily_reply_limit": 5,
        "daily_dm_limit": 2,
        "daily_view_limit": 50,
        "banned_phrases": ("buy now",),
        "expected_language": "en",
        "config_version": 7,
        "config_snapshot": {"version": 7, "profile": {"tone": "warm"}},
    }
    values.update(overrides)
    return AccountRow(**values)


def session_row(**overrides) -> SessionRow:
    values = {
        "user_id": USER,
        "ig_account_id": ACCOUNT,
        "ciphertext": "token",
        "captured_user_agent": USER_AGENT,
        "captured_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=10),
    }
    values.update(overrides)
    return SessionRow(**values)


def rows(**overrides) -> RunContextRows:
    values = {
        "account": account_row(),
        "session": session_row(),
        "targets": (
            TargetRow(USER, ACCOUNT, TARGET, "owner-1", "target.one", "vip", True),
        ),
        "usage": UsageRow(USER, ACCOUNT, 0, 0, 0),
        "history": HistoryRow(USER, ACCOUNT, "no recent contact", ()),
    }
    values.update(overrides)
    return RunContextRows(**values)


class FakeReader:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def read(self, user_id, ig_account_id):
        self.calls.append((user_id, ig_account_id))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeCipher:
    """Stands in for both the per-account factory and the cipher it returns."""

    def __init__(self):
        self.calls = []
        self.accounts = []

    def for_account(self, ig_account_id):
        self.accounts.append(ig_account_id)
        return self

    def decrypt(self, token):
        self.calls.append(token)
        return {"cookies": [{"name": "sessionid", "value": "x"}]}


class FakeBindings:
    def __init__(self, result=None):
        self._result = result
        self.calls = []
        self.job_ids = []

    def ensure_binding(self, user_id, ig_account_id, geo, *, job_id=None):
        self.calls.append((user_id, ig_account_id, geo))
        self.job_ids.append(job_id)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result if self._result is not None else endpoint()


def endpoint() -> ProxyEndpoint:
    return ProxyEndpoint(
        provider="iproyal",
        provider_reference="lease-1",
        proxy_type="isp",
        geo=GEO,
        host="proxy.example.com",
        port=8080,
        username="user",
        password="secret",
    )


def build_loader(reader=None, cipher=None, bindings=None):
    cipher = cipher if cipher is not None else FakeCipher()
    bindings = bindings if bindings is not None else FakeBindings()
    factory = FingerprintFactory(
        "s" * 64, FingerprintGeoRegistry.from_json(GEO_MAP)
    )
    loader = RunContextLoader(
        reader=reader if reader is not None else FakeReader(rows()),
        cipher_for=cipher.for_account,
        fingerprints=factory,
        proxy_bindings=bindings,
        clock=lambda: NOW,
    )
    return loader, cipher, bindings


async def test_load_returns_a_tenant_scoped_context():
    loader, cipher, bindings = build_loader()

    context = await loader.load(claimed_job())

    assert context.user_id == USER
    assert context.ig_account_id == ACCOUNT
    assert context.username == "account.one"
    assert context.geo == GEO
    assert context.endpoint.server == "http://proxy.example.com:8080"
    assert context.fingerprint.user_agent == USER_AGENT
    assert context.targets[0].id == TARGET
    assert context.budget == ActionBudget(
        20, 5, 0, plan_total=20, views=50, private_messages=5
    )
    assert context.config_version == 7
    assert context.config_snapshot == {
        "version": 7,
        "profile": {"tone": "warm"},
    }
    assert cipher.calls == ["token"]
    assert bindings.job_ids == [JOB]


async def test_view_lane_cannot_spend_engagement_budget():
    loader, _cipher, _bindings = build_loader()

    context = await loader.load(view_claimed_job())

    assert context.view_only is True
    assert context.budget == ActionBudget(
        0, 0, 0, plan_total=0, views=13, private_messages=0
    )


async def test_session_tenant_mismatch_aborts_before_decrypting():
    reader = FakeReader(rows(session=session_row(user_id=OTHER_USER)))
    loader, cipher, bindings = build_loader(reader=reader)

    with pytest.raises(RunContextError) as error:
        await loader.load(claimed_job())

    assert error.value.reason == "tenant_mismatch"
    assert cipher.calls == []
    assert bindings.calls == []


async def test_account_tenant_mismatch_aborts_before_decrypting():
    reader = FakeReader(rows(account=account_row(ig_account_id=OTHER_ACCOUNT)))
    loader, cipher, bindings = build_loader(reader=reader)

    with pytest.raises(RunContextError) as error:
        await loader.load(claimed_job())

    assert error.value.reason == "tenant_mismatch"
    assert cipher.calls == []


async def test_target_tenant_mismatch_aborts_before_decrypting():
    reader = FakeReader(
        rows(
            targets=(
                TargetRow(
                    OTHER_USER, ACCOUNT, TARGET, "owner-1", "target.one", "vip", True
                ),
            )
        )
    )
    loader, cipher, _ = build_loader(reader=reader)

    with pytest.raises(RunContextError) as error:
        await loader.load(claimed_job())

    assert error.value.reason == "tenant_mismatch"
    assert cipher.calls == []


async def test_history_tenant_mismatch_aborts_before_decrypting():
    reader = FakeReader(
        rows(history=HistoryRow(USER, OTHER_ACCOUNT, "no recent contact", ()))
    )
    loader, cipher, _ = build_loader(reader=reader)

    with pytest.raises(RunContextError) as error:
        await loader.load(claimed_job())

    assert error.value.reason == "tenant_mismatch"
    assert cipher.calls == []


async def test_usage_tenant_mismatch_aborts_before_decrypting():
    reader = FakeReader(rows(usage=UsageRow(OTHER_USER, ACCOUNT, 0, 0, 0)))
    loader, cipher, _ = build_loader(reader=reader)

    with pytest.raises(RunContextError) as error:
        await loader.load(claimed_job())

    assert error.value.reason == "tenant_mismatch"
    assert cipher.calls == []


async def test_missing_account_fails_closed():
    reader = FakeReader(RunContextError("account_missing"))
    loader, cipher, _ = build_loader(reader=reader)

    with pytest.raises(RunContextError) as error:
        await loader.load(claimed_job())

    assert error.value.reason == "account_missing"
    assert cipher.calls == []


async def test_expired_session_pauses_the_account():
    reader = FakeReader(rows(session=session_row(expires_at=NOW - timedelta(days=1))))
    loader, cipher, _ = build_loader(reader=reader)

    with pytest.raises(RunContextError) as error:
        await loader.load(claimed_job())

    assert error.value.reason == "session_expired"
    assert error.value.pause_account is True
    assert cipher.calls == []


async def test_stale_session_capture_pauses_the_account():
    reader = FakeReader(
        rows(session=session_row(captured_at=NOW - timedelta(days=400)))
    )
    loader, cipher, _ = build_loader(reader=reader)

    with pytest.raises(RunContextError) as error:
        await loader.load(claimed_job())

    assert error.value.reason == "session_expired"
    assert error.value.pause_account is True


async def test_paused_account_stops_the_run():
    reader = FakeReader(rows(account=account_row(paused_at=NOW)))
    loader, cipher, _ = build_loader(reader=reader)

    with pytest.raises(RunContextError) as error:
        await loader.load(claimed_job())

    assert error.value.reason == "account_paused"
    assert cipher.calls == []


async def test_unhealthy_account_stops_the_run():
    reader = FakeReader(rows(account=account_row(health="banned")))
    loader, cipher, _ = build_loader(reader=reader)

    with pytest.raises(RunContextError) as error:
        await loader.load(claimed_job())

    assert error.value.reason == "account_unhealthy"
    assert cipher.calls == []


async def test_proxy_binding_failure_pauses_the_account():
    bindings = FakeBindings(RuntimeError("proxy health check failed"))
    loader, cipher, _ = build_loader(bindings=bindings)

    with pytest.raises(RunContextError) as error:
        await loader.load(claimed_job())

    assert error.value.reason == "proxy_unhealthy"
    assert error.value.pause_account is True


async def test_transient_proxy_binding_failure_does_not_pause_the_account():
    bindings = FakeBindings(
        ProxyBindingError(
            "proxy health check temporarily unavailable",
            transient=True,
            reason="proxy_access_denied",
        )
    )
    loader, cipher, _ = build_loader(bindings=bindings)

    with pytest.raises(RunContextError) as error:
        await loader.load(claimed_job())

    assert error.value.reason == "proxy_transient"
    assert error.value.pause_account is False
    assert cipher.calls == []


async def test_disabled_targets_are_excluded():
    reader = FakeReader(
        rows(
            targets=(
                TargetRow(USER, ACCOUNT, TARGET, "owner-1", "target.one", "vip", True),
                TargetRow(
                    USER,
                    ACCOUNT,
                    UUID("00000000-0000-0000-0000-0000000000aa"),
                    "owner-2",
                    "target.two",
                    "broad",
                    False,
                ),
            )
        )
    )
    loader, _, _ = build_loader(reader=reader)

    context = await loader.load(claimed_job())

    assert len(context.targets) == 1
    assert context.targets[0].handle == "target.one"


async def test_budget_reflects_recorded_usage():
    reader = FakeReader(rows(usage=UsageRow(USER, ACCOUNT, 4, 1, 0)))
    loader, _, _ = build_loader(reader=reader)

    context = await loader.load(claimed_job())

    # One reply is already sent, so the shared message ceiling is 5 - 1.
    assert context.budget == ActionBudget(
        16, 4, 0, plan_total=15, views=50, private_messages=4
    )


@pytest.mark.parametrize('enabled,following,used_dms,likes,replies,expected', [
    (True, True, 0, 8, 2, 1),
    (False, True, 0, 8, 2, 2),
    (True, False, 0, 8, 2, 2),
    (True, True, 1, 8, 2, 1),
    (True, True, 0, 0, 2, 1),
    (True, True, 0, 0, 1, 1),
    (True, True, 0, 8, 1, 0),
    (True, True, 0, 8, 0, 0),
])
async def test_opted_in_follow_reels_keep_one_shared_slot(enabled,following,used_dms,likes,replies,expected):
    account = account_row(follow_after_engagement=following, follow_reel_message_enabled=enabled,
                          daily_like_limit=likes,daily_reply_limit=replies,daily_dm_limit=0)
    reader = FakeReader(rows(account=account,usage=UsageRow(USER,ACCOUNT,0,0,used_dms)))
    loader, _, _ = build_loader(reader=reader)
    context = await loader.load(claimed_job())
    assert min(context.budget.replies, context.budget.private_messages) == expected
    assert context.budget.private_messages == expected
    assert context.budget.likes == likes
    assert expected + used_dms <= max(replies,used_dms)


async def test_warmup_bracket_limits_a_new_account():
    reader = FakeReader(
        rows(account=account_row(warmup_started_at=NOW - timedelta(days=1)))
    )
    loader, _, _ = build_loader(reader=reader)

    context = await loader.load(claimed_job())

    assert context.budget == ActionBudget(3, 0, 0, plan_total=20, views=30)


async def test_context_repr_never_exposes_secrets():
    loader, _, _ = build_loader()

    context = await loader.load(claimed_job())

    text = repr(context)
    assert "secret" not in text
    assert "sessionid" not in text
    assert "cookies=<redacted>" in text


async def test_pacing_seed_is_stable_and_account_scoped():
    loader, _, _ = build_loader()
    other_loader, _, _ = build_loader()

    first = await loader.load(claimed_job())
    second = await other_loader.load(claimed_job())

    assert first.pacing_seed == second.pacing_seed
    assert first.pacing_seed != first.fingerprint.canvas_seed


async def test_reader_is_called_once_with_the_job_tenant():
    reader = FakeReader(rows())
    loader, _, _ = build_loader(reader=reader)

    await loader.load(claimed_job())

    assert reader.calls == [(USER, ACCOUNT)]


async def test_the_cipher_is_bound_to_the_jobs_account():
    """AES-GCM binds the envelope to its account, so the cipher must too."""

    loader, cipher, _ = build_loader()

    await loader.load(claimed_job())

    assert cipher.accounts == [ACCOUNT]


async def test_no_cipher_is_built_for_a_mismatched_tenant():
    reader = FakeReader(rows(session=session_row(user_id=OTHER_USER)))
    loader, cipher, _ = build_loader(reader=reader)

    with pytest.raises(RunContextError):
        await loader.load(claimed_job())

    assert cipher.accounts == []
