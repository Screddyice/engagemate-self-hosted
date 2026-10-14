"""Weekly report command and settings contracts."""

from __future__ import annotations

import base64
import logging
import os
import uuid
from types import SimpleNamespace

import pytest

from engagemate.config import Settings
from engagemate.reports import main as reports_main
from engagemate.reports.dispatcher import DispatchResult


def _settings(**values: object) -> Settings:
    configured = {
        "DATABASE_URL": "postgresql://localhost/engagemate",
        "SESSION_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32))
        .decode()
        .rstrip("="),
        "INTERNAL_API_KEY": "internal-test-key",
        "_env_file": None,
    }
    configured.update(values)
    return Settings(**configured)


def test_reporting_settings_have_bounded_defaults_aliases_and_redacted_repr() -> None:
    canary_id = uuid.uuid4()
    defaults = _settings()
    configured = _settings(
        RESEND_API_KEY="re_private_secret",
        WEEKLY_REPORT_FROM="EngageMate <reports@example.test>",
        WEEKLY_REPORT_BATCH_SIZE="12",
        WEEKLY_REPORT_CANARY_USER_ID=str(canary_id),
        WEEKLY_REPORT_CANARY_RECIPIENT="canary@example.test",
    )

    assert defaults.resend_api_key is None
    assert defaults.weekly_report_from is None
    assert defaults.weekly_report_batch_size == 25
    assert defaults.weekly_report_canary_user_id is None
    assert defaults.weekly_report_canary_recipient is None
    assert configured.resend_api_key.get_secret_value() == "re_private_secret"
    assert configured.weekly_report_from == "EngageMate <reports@example.test>"
    assert configured.weekly_report_batch_size == 12
    assert configured.weekly_report_canary_user_id == canary_id
    assert configured.weekly_report_canary_recipient == "canary@example.test"
    serialized = repr(configured)
    assert "re_private_secret" not in serialized
    assert "canary@example.test" not in serialized
    assert "reports@example.test" not in serialized


@pytest.mark.parametrize("batch_size", [0, 101])
def test_reporting_batch_size_is_bounded(batch_size: int) -> None:
    with pytest.raises(ValueError):
        _settings(WEEKLY_REPORT_BATCH_SIZE=batch_size)


def test_parse_args_exposes_only_dry_run_and_canary_modes() -> None:
    assert reports_main.parse_args([]) == reports_main.CommandOptions(
        dry_run=False,
        canary=False,
    )
    assert reports_main.parse_args(["--dry-run"]).dry_run is True
    assert reports_main.parse_args(["--canary"]).canary is True

    with pytest.raises(SystemExit):
        reports_main.parse_args(["--dry-run", "--canary"])
    with pytest.raises(SystemExit):
        reports_main.parse_args(["--api-key", "re_private_secret"])
    with pytest.raises(SystemExit):
        reports_main.parse_args(["--recipient", "manager@example.test"])


def test_execute_dry_run_needs_no_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple] = []

    class Pool:
        def close(self):
            events.append(("pool_close",))

    class Dispatcher:
        def __init__(self, repository, client, **kwargs):
            events.append(("dispatcher", repository, client, kwargs))

        def run(self, now, *, dry_run=False, canary=False):
            events.append(("run", now, dry_run, canary))
            return DispatchResult(due=1, built=1, claimed=0, sent=0, failed=0)

    pool = Pool()
    repository = object()
    canary_repository = object()
    monkeypatch.setattr(
        reports_main,
        "Settings",
        lambda: SimpleNamespace(
            database_url="postgresql://test",
            resend_api_key=None,
            weekly_report_from=None,
            weekly_report_batch_size=9,
            weekly_report_canary_user_id=None,
            weekly_report_canary_recipient=None,
            internal_api_key=SimpleNamespace(get_secret_value=lambda: "test-internal-key"),
        ),
    )
    monkeypatch.setattr(reports_main, "create_pool", lambda *_args, **_kwargs: pool)
    monkeypatch.setattr(
        reports_main, "ReportRepository", lambda received: repository
    )
    monkeypatch.setattr(
        reports_main,
        "CanaryReportRepository",
        lambda received_pool, received_repository: canary_repository,
    )
    monkeypatch.setattr(reports_main, "ReportDispatcher", Dispatcher)

    result = reports_main.execute(reports_main.CommandOptions(True, False))

    assert result.built == 1
    dispatcher_event = next(event for event in events if event[0] == "dispatcher")
    assert dispatcher_event[2] is None
    assert dispatcher_event[3]["canary_repository"] is canary_repository
    assert next(event for event in events if event[0] == "run")[2:] == (True, False)
    assert events[-1] == ("pool_close",)


def test_execute_normal_mode_wires_provider_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple] = []
    canary_id = uuid.uuid4()

    class Pool:
        def close(self):
            events.append(("pool_close",))

    class Http:
        def close(self):
            events.append(("http_close",))

    class Client:
        def __init__(self, **kwargs):
            events.append(("client", kwargs))

    class Dispatcher:
        def __init__(self, repository, client, **kwargs):
            events.append(("dispatcher", repository, client, kwargs))

        def run(self, now, *, dry_run=False, canary=False):
            events.append(("run", now, dry_run, canary))
            return DispatchResult(due=1, built=1, claimed=1, sent=1, failed=0)

    pool = Pool()
    http = Http()
    repository = object()
    canary_repository = object()
    secret = SimpleNamespace(get_secret_value=lambda: "re_private_secret")
    monkeypatch.setattr(
        reports_main,
        "Settings",
        lambda: SimpleNamespace(
            database_url="postgresql://test",
            resend_api_key=secret,
            weekly_report_from="reports@example.test",
            weekly_report_batch_size=9,
            weekly_report_canary_user_id=canary_id,
            weekly_report_canary_recipient="canary@example.test",
            internal_api_key=SimpleNamespace(get_secret_value=lambda: "test-internal-key"),
        ),
    )
    monkeypatch.setattr(reports_main, "create_pool", lambda *_args, **_kwargs: pool)
    monkeypatch.setattr(reports_main.httpx, "Client", lambda: http)
    monkeypatch.setattr(reports_main, "ReportRepository", lambda _pool: repository)
    monkeypatch.setattr(
        reports_main,
        "CanaryReportRepository",
        lambda received_pool, received_repository: canary_repository,
    )
    monkeypatch.setattr(reports_main, "ResendClient", Client)
    monkeypatch.setattr(reports_main, "ReportDispatcher", Dispatcher)

    result = reports_main.execute(reports_main.CommandOptions(False, True))

    assert result.sent == 1
    client_kwargs = next(event for event in events if event[0] == "client")[1]
    assert client_kwargs == {
        "api_key": "re_private_secret",
        "sender": "reports@example.test",
        "http": http,
    }
    dispatcher_kwargs = next(event for event in events if event[0] == "dispatcher")[3]
    link = dispatcher_kwargs.pop("unsubscribe_url")(uuid.UUID("11111111-1111-4111-8111-111111111111"))
    assert link.startswith("https://app.example.com/unsubscribe?u=11111111-1111-4111-8111-111111111111&c=weekly_report&t=")
    callback = dispatcher_kwargs.pop("copy_notifications")
    assert callback.args == (pool,)
    assert callback.func.__name__ == "queue_weekly_copies"
    assert dispatcher_kwargs == {
        "batch_size": 9,
        "canary_user_id": canary_id,
        "canary_recipient": "canary@example.test",
        "canary_repository": canary_repository,
    }
    assert next(event for event in events if event[0] == "run")[2:] == (False, True)
    assert events[-2:] == [("http_close",), ("pool_close",)]


def test_execute_closes_provider_client_when_database_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Http:
        def close(self):
            events.append("http_close")

    http = Http()
    secret = SimpleNamespace(get_secret_value=lambda: "re_private_secret")
    monkeypatch.setattr(
        reports_main,
        "Settings",
        lambda: SimpleNamespace(
            database_url="postgresql://test",
            resend_api_key=secret,
            weekly_report_from="reports@example.test",
            weekly_report_batch_size=9,
            weekly_report_canary_user_id=None,
            weekly_report_canary_recipient=None,
            internal_api_key=SimpleNamespace(get_secret_value=lambda: "test-internal-key"),
        ),
    )
    monkeypatch.setattr(reports_main.httpx, "Client", lambda: http)
    monkeypatch.setattr(reports_main, "ResendClient", lambda **_kwargs: object())

    def fail_pool(*_args, **_kwargs):
        raise RuntimeError("database startup failed")

    monkeypatch.setattr(reports_main, "create_pool", fail_pool)

    with pytest.raises(RuntimeError, match="database startup failed"):
        reports_main.execute(reports_main.CommandOptions(False, False))

    assert events == ["http_close"]


def test_execute_closes_http_client_when_sender_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Http:
        def close(self):
            events.append("http_close")

    secret = SimpleNamespace(get_secret_value=lambda: "re_private_secret")
    monkeypatch.setattr(
        reports_main,
        "Settings",
        lambda: SimpleNamespace(
            database_url="postgresql://test",
            resend_api_key=secret,
            weekly_report_from="invalid sender",
            weekly_report_batch_size=9,
            weekly_report_canary_user_id=None,
            weekly_report_canary_recipient=None,
            internal_api_key=SimpleNamespace(get_secret_value=lambda: "test-internal-key"),
        ),
    )
    monkeypatch.setattr(reports_main.httpx, "Client", Http)

    with pytest.raises(ValueError, match="email address is invalid"):
        reports_main.execute(reports_main.CommandOptions(False, False))

    assert events == ["http_close"]


def test_main_returns_nonzero_without_logging_secret_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(_options):
        raise RuntimeError("re_private_secret recipient@example.test")

    monkeypatch.setattr(reports_main, "execute", fail)

    with caplog.at_level(logging.ERROR):
        exit_code = reports_main.main([])

    assert exit_code == 1
    assert "status=failed error_code=batch_failed" in caplog.text
    assert "re_private_secret" not in caplog.text
    assert "recipient@example.test" not in caplog.text


def test_main_returns_zero_after_at_least_one_normal_send(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        reports_main,
        "execute",
        lambda _options: DispatchResult(
            due=2,
            built=2,
            claimed=2,
            sent=1,
            failed=1,
        ),
    )

    with caplog.at_level(logging.INFO):
        exit_code = reports_main.main([])

    assert exit_code == 0
    assert "status=complete" in caplog.text
    assert "due=" not in caplog.text
    assert "built=" not in caplog.text
    assert "claimed=" not in caplog.text
    assert "sent=" not in caplog.text


def test_main_returns_nonzero_when_all_normal_sends_fail(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        reports_main,
        "execute",
        lambda _options: DispatchResult(
            due=2,
            built=2,
            claimed=2,
            sent=0,
            failed=2,
        ),
    )

    with caplog.at_level(logging.INFO):
        exit_code = reports_main.main([])

    assert exit_code == 1
    assert "status=failed error_code=batch_delivery_failed" in caplog.text


@pytest.mark.parametrize(
    ("result", "error_code"),
    [
        (
            DispatchResult(due=0, built=0, claimed=0, sent=0, failed=0),
            "canary_user_not_due",
        ),
        (
            DispatchResult(due=1, built=1, claimed=1, sent=0, failed=1),
            "canary_delivery_failed",
        ),
    ],
)
def test_main_returns_nonzero_when_canary_does_not_send_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    result: DispatchResult,
    error_code: str,
) -> None:
    monkeypatch.setattr(reports_main, "execute", lambda _options: result)

    with caplog.at_level(logging.INFO):
        exit_code = reports_main.main(["--canary"])

    assert exit_code == 1
    assert f"status=failed error_code={error_code}" in caplog.text


def test_main_returns_zero_only_after_one_canary_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reports_main,
        "execute",
        lambda _options: DispatchResult(
            due=1,
            built=1,
            claimed=1,
            sent=1,
            failed=0,
        ),
    )

    assert reports_main.main(["--canary"]) == 0
