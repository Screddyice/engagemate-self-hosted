"""Weekly report dispatcher ordering and failure-isolation contracts."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import pytest

from engagemate.reports.aggregate import previous_week
from engagemate.reports.dispatcher import (
    CanaryReportRepository,
    DispatchResult,
    ReportDispatcher,
)
from engagemate.reports.models import AccountWeeklyReport, ManagerWeeklyReport
from engagemate.reports.render import RenderedEmail
from engagemate.reports.repository import DeliveryClaim, DueManager, ReportRepository
from engagemate.reports.resend import ResendError, request_payload_hash
from tests.reports.test_repository import (
    _delivery_row,
    _seed_account,
    _seed_user,
    database_url,
    pool,
)

NOW = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)


def _report(username: str = "managed.account") -> ManagerWeeklyReport:
    return ManagerWeeklyReport(
        period=previous_week(NOW, "UTC"),
        accounts=(
            AccountWeeklyReport(
                username=username,
                follower_start=100,
                follower_end=105,
                engagement_rate_bps_start=200,
                engagement_rate_bps_end=225,
                opportunities_reviewed=2,
                likes_sent=1,
                replies_sent=1,
                dms_sent=0,
                views_sent=1,
                skipped=0,
                gated=0,
                failed=0,
            ),
        ),
    )


def _due(
    user_id: uuid.UUID | None = None,
    recipient: str = "due@example.test",
) -> DueManager:
    return DueManager(
        user_id=user_id or uuid.uuid4(),
        recipient_email=recipient,
        timezone="UTC",
        period=previous_week(NOW, "UTC"),
    )


class FakeRepository:
    def __init__(self, due: list[DueManager]) -> None:
        self.due = due
        self.events: list[tuple] = []
        self.claims: dict[uuid.UUID, DeliveryClaim | None] = {}
        self.lock_result = True
        self.raise_mark_failed_for: set[uuid.UUID] = set()
        self.reports = {manager.user_id: _report() for manager in due}

    def list_due_managers(self, now, limit):
        self.events.append(("due", now, limit))
        return self.due

    def build_report(self, user_id, period):
        self.events.append(("report", user_id, period.week_start))
        return self.reports[user_id]

    def claim(self, user_id, week_start, recipient, now):
        self.events.append(("claim", user_id, week_start, recipient, now))
        if user_id in self.claims:
            return self.claims[user_id]
        return DeliveryClaim(
            delivery_id=uuid.uuid5(uuid.NAMESPACE_URL, str(user_id)),
            user_id=user_id,
            week_start=week_start,
            recipient_email=recipient,
            attempt_number=1,
            lease_token=uuid.uuid4(),
        )

    def lock_payload(self, delivery_id, lease_token, payload_hash, now=None):
        self.events.append(
            ("lock", delivery_id, lease_token, payload_hash, now)
        )
        return self.lock_result

    def mark_sent(self, delivery_id, lease_token, provider_id, now):
        self.events.append(
            ("sent", delivery_id, lease_token, provider_id, now)
        )
        return True

    def mark_failed(
        self,
        delivery_id,
        lease_token,
        error_code,
        retryable,
        now,
    ):
        if delivery_id in self.raise_mark_failed_for:
            raise RuntimeError("private repository failure")
        self.events.append(
            (
                "failed",
                delivery_id,
                lease_token,
                error_code,
                retryable,
                now,
            )
        )
        return True


class FakeResend:
    sender = "reports@example.test"

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.error: ResendError | None = None
        self.error_recipients: set[str] = set()

    def send(self, recipient, rendered, idempotency_key):
        self.events.append(("send", recipient, rendered, idempotency_key))
        if self.error is not None or recipient in self.error_recipients:
            raise self.error or ResendError("provider_unavailable", retryable=True)
        return f"provider-{len(self.events)}"


class FakeCanaryRepository:
    def __init__(
        self,
        repository: FakeRepository,
        due: DueManager | None,
    ) -> None:
        self.repository = repository
        self.due = due
        self.events: list[tuple] = []

    def find_due_manager(self, now, user_id, recipient):
        self.events.append(("find_due", now, user_id, recipient))
        if self.due is None:
            return None
        return DueManager(
            user_id=self.due.user_id,
            recipient_email=recipient,
            timezone=self.due.timezone,
            period=self.due.period,
        )

    def claim(self, user_id, week_start, recipient, now):
        self.events.append(("claim", user_id, week_start, recipient, now))
        return self.repository.claim(user_id, week_start, recipient, now)


def _renderer(events: list[tuple], report: ManagerWeeklyReport) -> RenderedEmail:
    events.append(("render", report))
    return RenderedEmail("Subject", "Text", "<p>HTML</p>")


def test_dispatcher_claims_before_render_and_locks_complete_hash_before_send() -> None:
    due = _due()
    repository = FakeRepository([due])
    client = FakeResend()
    render_events: list[tuple] = []

    def render_after_claim(report: ManagerWeeklyReport) -> RenderedEmail:
        assert repository.events[-1][0] == "claim"
        return _renderer(render_events, report)

    dispatcher = ReportDispatcher(
        repository,
        client,
        batch_size=7,
        renderer=render_after_claim,
    )

    result = dispatcher.run(NOW)

    assert result == DispatchResult(due=1, built=1, claimed=1, sent=1, failed=0)
    claim = next(event for event in repository.events if event[0] == "claim")
    lock = next(event for event in repository.events if event[0] == "lock")
    sent = next(event for event in repository.events if event[0] == "sent")
    send = client.events[0]
    assert [event[0] for event in repository.events] == [
        "due",
        "report",
        "claim",
        "lock",
        "sent",
    ]
    assert render_events[0][0] == "render"
    assert claim[3] == due.recipient_email
    assert lock[3] == request_payload_hash(
        client.sender,
        claim[3],
        send[2],
    )
    assert len(lock[3]) == 64
    assert send[1] == claim[3]
    assert send[3] == f"engagemate-weekly/{lock[1]}"
    assert sent[3].startswith("provider-")


def test_dispatcher_uses_claim_captured_recipient_for_hash_and_retry_key() -> None:
    due = _due(recipient="changed@example.test")
    repository = FakeRepository([due])
    delivery_id = uuid.uuid4()
    repository.claims[due.user_id] = DeliveryClaim(
        delivery_id=delivery_id,
        user_id=due.user_id,
        week_start=due.period.week_start,
        recipient_email="captured@example.test",
        attempt_number=2,
        lease_token=uuid.uuid4(),
    )
    client = FakeResend()
    dispatcher = ReportDispatcher(repository, client, batch_size=5)

    first = dispatcher.run(NOW)
    second = dispatcher.run(NOW)

    assert first.sent == second.sent == 1
    sends = client.events
    assert [event[1] for event in sends] == [
        "captured@example.test",
        "captured@example.test",
    ]
    assert [event[3] for event in sends] == [
        f"engagemate-weekly/{delivery_id}",
        f"engagemate-weekly/{delivery_id}",
    ]
    hashes = [event[3] for event in repository.events if event[0] == "lock"]
    assert hashes[0] == hashes[1]


def test_payload_mismatch_stops_before_provider_send() -> None:
    due = _due()
    repository = FakeRepository([due])
    repository.lock_result = False
    client = FakeResend()

    result = ReportDispatcher(repository, client, batch_size=5).run(NOW)

    assert result.failed == 1
    assert client.events == []
    assert not any(event[0] in {"sent", "failed"} for event in repository.events)


def test_unclaimable_expired_delivery_stops_before_render_or_send() -> None:
    due = _due()
    repository = FakeRepository([due])
    repository.claims[due.user_id] = None
    client = FakeResend()
    render_events: list[tuple] = []

    result = ReportDispatcher(
        repository,
        client,
        batch_size=5,
        renderer=lambda report: _renderer(render_events, report),
    ).run(NOW)

    assert result.claimed == 0
    assert result.sent == 0
    assert render_events == []
    assert client.events == []
    assert [event[0] for event in repository.events] == ["due", "report", "claim"]


@pytest.mark.parametrize(
    ("error", "expected_retryable"),
    [
        (ResendError("provider_timeout", retryable=True), True),
        (ResendError("recipient_rejected", retryable=False), False),
    ],
)
def test_provider_failure_uses_repository_finalization(
    error: ResendError,
    expected_retryable: bool,
) -> None:
    due = _due()
    repository = FakeRepository([due])
    client = FakeResend()
    client.error = error

    result = ReportDispatcher(repository, client, batch_size=5).run(NOW)

    assert result.failed == 1
    failed = next(event for event in repository.events if event[0] == "failed")
    assert failed[3] == error.code
    assert failed[4] is expected_retryable
    assert not any(event[0] == "sent" for event in repository.events)


def test_one_manager_failure_does_not_stop_later_manager() -> None:
    first = _due(recipient="first@example.test")
    second = _due(recipient="second@example.test")
    repository = FakeRepository([first, second])
    client = FakeResend()
    client.error_recipients.add("first@example.test")

    result = ReportDispatcher(repository, client, batch_size=5).run(NOW)

    assert result == DispatchResult(due=2, built=2, claimed=2, sent=1, failed=1)
    assert [event[1] for event in client.events] == [
        "first@example.test",
        "second@example.test",
    ]
    assert len([event for event in repository.events if event[0] == "failed"]) == 1
    assert len([event for event in repository.events if event[0] == "sent"]) == 1


def test_attempt_logs_are_structured_and_redact_delivery_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _due(recipient="first@example.test")
    second = _due(recipient="second@example.test")
    repository = FakeRepository([first, second])
    client = FakeResend()
    client.error_recipients.add("first@example.test")

    with caplog.at_level(logging.INFO, logger="engagemate.reports.dispatcher"):
        ReportDispatcher(repository, client, batch_size=5).run(NOW)

    outcome_records = [
        record.message
        for record in caplog.records
        if record.name == "engagemate.reports.dispatcher"
    ]
    assert len(outcome_records) == 2
    assert "status=failed error_code=provider_unavailable" in outcome_records[0]
    assert "status=sent error_code=none" in outcome_records[1]
    for message, manager in zip(outcome_records, (first, second), strict=True):
        assert f"user_id={manager.user_id}" in message
        assert "week_start=2026-08-10" in message
        assert "attempt=1" in message
        assert "delivery_id=" in message
    serialized = "\n".join(outcome_records)
    for forbidden in (
        "first@example.test",
        "second@example.test",
        "Subject",
        "<p>",
        "engagemate-weekly/",
        "provider-",
        "raw-provider-body",
    ):
        assert forbidden not in serialized


def test_failure_finalization_error_does_not_stop_later_manager() -> None:
    first = _due(recipient="first@example.test")
    second = _due(recipient="second@example.test")
    repository = FakeRepository([first, second])
    first_delivery_id = uuid.uuid5(uuid.NAMESPACE_URL, str(first.user_id))
    repository.raise_mark_failed_for.add(first_delivery_id)
    client = FakeResend()
    client.error_recipients.add("first@example.test")

    result = ReportDispatcher(repository, client, batch_size=5).run(NOW)

    assert result == DispatchResult(due=2, built=2, claimed=2, sent=1, failed=1)
    assert [event[1] for event in client.events] == [
        "first@example.test",
        "second@example.test",
    ]
    assert len([event for event in repository.events if event[0] == "sent"]) == 1


def test_dry_run_builds_and_renders_without_claim_or_provider() -> None:
    due = [_due(), _due()]
    repository = FakeRepository(due)
    render_events: list[tuple] = []
    dispatcher = ReportDispatcher(
        repository,
        None,
        batch_size=5,
        renderer=lambda report: _renderer(render_events, report),
    )

    result = dispatcher.run(NOW, dry_run=True)

    assert result == DispatchResult(due=2, built=2, claimed=0, sent=0, failed=0)
    assert len(render_events) == 2
    assert [event[0] for event in repository.events] == ["due", "report", "report"]


@pytest.mark.parametrize(
    ("user_id", "recipient"),
    [
        (None, None),
        (uuid.uuid4(), None),
        (None, "canary@example.test"),
        (uuid.uuid4(), "one@example.test,two@example.test"),
    ],
)
def test_canary_refuses_incomplete_or_multiple_configuration(
    user_id: uuid.UUID | None,
    recipient: str | None,
) -> None:
    repository = FakeRepository([_due()])
    dispatcher = ReportDispatcher(
        repository,
        FakeResend(),
        batch_size=5,
        canary_user_id=user_id,
        canary_recipient=recipient,
    )

    with pytest.raises(ValueError, match="canary configuration"):
        dispatcher.run(NOW, canary=True)

    assert repository.events == []


def test_canary_processes_only_configured_user_and_records_override_recipient() -> None:
    chosen = _due(recipient="production@example.test")
    other = _due(recipient="other@example.test")
    repository = FakeRepository([other, chosen])
    canary_repository = FakeCanaryRepository(repository, chosen)
    client = FakeResend()
    dispatcher = ReportDispatcher(
        repository,
        client,
        batch_size=5,
        canary_user_id=chosen.user_id,
        canary_recipient="canary@example.test",
        canary_repository=canary_repository,
    )

    result = dispatcher.run(NOW, canary=True)

    assert result == DispatchResult(due=1, built=1, claimed=1, sent=1, failed=0)
    claims = [event for event in repository.events if event[0] == "claim"]
    assert len(claims) == 1
    assert claims[0][1] == chosen.user_id
    assert claims[0][3] == "canary@example.test"
    assert client.events[0][1] == "canary@example.test"
    assert not any(event[0] == "due" for event in repository.events)
    assert canary_repository.events[0] == (
        "find_due",
        NOW,
        chosen.user_id,
        "canary@example.test",
    )


def test_canary_zero_due_is_an_explicit_no_send_result() -> None:
    configured_user = uuid.uuid4()
    repository = FakeRepository([_due()])
    canary_repository = FakeCanaryRepository(repository, None)
    client = FakeResend()

    result = ReportDispatcher(
        repository,
        client,
        batch_size=1,
        canary_user_id=configured_user,
        canary_recipient="canary@example.test",
        canary_repository=canary_repository,
    ).run(NOW, canary=True)

    assert result == DispatchResult(due=0, built=0, claimed=0, sent=0, failed=0)
    assert client.events == []
    assert not any(event[0] == "due" for event in repository.events)


def test_canary_rejects_lookup_for_a_different_user() -> None:
    configured_user = uuid.uuid4()
    repository = FakeRepository([])
    canary_repository = FakeCanaryRepository(repository, _due())

    with pytest.raises(ValueError, match="canary lookup mismatch"):
        ReportDispatcher(
            repository,
            FakeResend(),
            batch_size=1,
            canary_user_id=configured_user,
            canary_recipient="canary@example.test",
            canary_repository=canary_repository,
        ).run(NOW, canary=True)


def test_real_canary_claim_does_not_reuse_or_mutate_normal_recipient(pool) -> None:
    user_id = _seed_user(pool, "production@example.test")
    _seed_account(pool, user_id, "managed.account")
    repository = ReportRepository(pool)
    canary_repository = CanaryReportRepository(pool, repository)
    due = canary_repository.find_due_manager(
        NOW,
        user_id,
        "canary@example.test",
    )
    assert due is not None
    normal = repository.claim(
        user_id,
        due.period.week_start,
        "production@example.test",
        NOW,
    )
    assert normal is not None
    assert repository.lock_payload(
        normal.delivery_id,
        normal.lease_token,
        "a" * 64,
        NOW,
    )
    assert repository.mark_failed(
        normal.delivery_id,
        normal.lease_token,
        "provider_timeout",
        True,
        NOW,
    )
    before = _delivery_row(pool, user_id, due.period.week_start)

    claim = canary_repository.claim(
        user_id,
        due.period.week_start,
        "canary@example.test",
        NOW,
    )

    after = _delivery_row(pool, user_id, due.period.week_start)
    assert claim is None
    assert after == before
    assert after["recipient_email"] == "production@example.test"
    assert after["payload_hash"] == "a" * 64


def test_real_canary_query_and_claim_bind_the_configured_mailbox(pool) -> None:
    skipped_user = _seed_user(pool, "first@example.test")
    _seed_account(pool, skipped_user, "first.account")
    configured_user = _seed_user(pool, "production@example.test")
    _seed_account(pool, configured_user, "configured.account")
    repository = ReportRepository(pool)
    canary_repository = CanaryReportRepository(pool, repository)

    due = canary_repository.find_due_manager(
        NOW,
        configured_user,
        "canary@example.test",
    )
    assert due is not None
    claim = canary_repository.claim(
        configured_user,
        due.period.week_start,
        "canary@example.test",
        NOW,
    )

    assert due.user_id == configured_user
    assert due.recipient_email == "canary@example.test"
    assert claim is not None
    assert claim.user_id == configured_user
    assert claim.recipient_email == "canary@example.test"
    row = _delivery_row(pool, configured_user, due.period.week_start)
    assert row["recipient_email"] == "canary@example.test"
    with pool.connection() as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM weekly_report_deliveries "
            "WHERE user_id = %s",
            (skipped_user,),
        ).fetchone()["count"] == 0


def test_normal_mode_ignores_configured_canary_values() -> None:
    first = _due(recipient="first@example.test")
    second = _due(recipient="second@example.test")
    repository = FakeRepository([first, second])
    client = FakeResend()
    dispatcher = ReportDispatcher(
        repository,
        client,
        batch_size=5,
        canary_user_id=first.user_id,
        canary_recipient="canary@example.test",
    )

    result = dispatcher.run(NOW)

    assert result.due == 2
    assert [event[1] for event in client.events] == [
        "first@example.test",
        "second@example.test",
    ]


def test_dispatcher_passes_each_managers_unsubscribe_link_to_the_renderer() -> None:
    due = _due()
    repository = FakeRepository([due])
    seen: list[str] = []

    def renderer(report: ManagerWeeklyReport, *, unsubscribe_url: str) -> RenderedEmail:
        seen.append(unsubscribe_url)
        return RenderedEmail("Subject", "Text", "<p>HTML</p>")

    dispatcher = ReportDispatcher(
        repository, FakeResend(), batch_size=7, renderer=renderer,
        unsubscribe_url=lambda user_id: f"link-for-{user_id}",
    )
    assert dispatcher.run(NOW).sent == 1
    assert seen == [f"link-for-{due.user_id}"]
