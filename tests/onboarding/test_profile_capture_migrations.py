"""Restriction persistence against the deployed migration chain."""

import pytest
from engagemate.db.text import text
from engagemate.onboarding.profile_capture import ProfileCaptureService

from tests.worker import test_canary_scripts as canary_fixtures

database_url = canary_fixtures.database_url
world = canary_fixtures.world


def test_profile_restriction_pauses_account_and_records_health_event(world):
    service = ProfileCaptureService(world["pool"], object())
    user, account = world["user_id"], world["account_id"]
    service._reserve(user, account)
    service._failure(user, account, "rate_limited", True)
    with world["pool"].connection() as connection:
        row = connection.execute(
            "SELECT health_status,pause_reason,paused_at FROM ig_accounts WHERE id=%s",
            (account,),
        ).fetchone()
        event = connection.execute(
            "SELECT event_type,evidence,created_at FROM account_health_events WHERE user_id=%s AND ig_account_id=%s",
            (user, account),
        ).fetchone()
        cooldown = connection.execute(
            "SELECT next_allowed_at > now()+interval '29 minutes' AS cooling FROM profile_check_state WHERE user_id=%s AND ig_account_id=%s",
            (user, account),
        ).fetchone()
    assert text(row["health_status"]) == "paused"
    assert text(row["pause_reason"]) == "rate_limit"
    assert row["paused_at"] is not None
    assert text(event["event_type"]) == "rate_limit"
    assert event["evidence"] == {
        "source": "onboarding_profile_check",
        "reason": "rate_limited",
    }
    assert event["created_at"] is not None
    assert cooldown["cooling"] is True
    service._failure(user, account, "rate_limited", True)
    with world["pool"].connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM account_health_events WHERE ig_account_id=%s",
                (account,),
            ).fetchone()["n"]
            == 1
        )


@pytest.mark.parametrize("health", ["paused", "challenged", "banned"])
def test_profile_restriction_preserves_existing_protective_state(world, health):
    service = ProfileCaptureService(world["pool"], object())
    user, account = world["user_id"], world["account_id"]
    service._reserve(user, account)
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE ig_accounts SET health_status=%s,pause_reason='operator_stop' WHERE id=%s",
            (health, account),
        )
    service._failure(user, account, "rate_limited", True)
    with world["pool"].connection() as connection:
        row = connection.execute(
            "SELECT health_status,pause_reason FROM ig_accounts WHERE id=%s", (account,)
        ).fetchone()
        assert text(row["health_status"]) == health
        assert text(row["pause_reason"]) == "operator_stop"
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM account_health_events WHERE ig_account_id=%s",
                (account,),
            ).fetchone()["n"]
            == 0
        )
