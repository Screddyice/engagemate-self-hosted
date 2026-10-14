"""Collector orchestration tests with external boundaries replaced by fakes."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import date, datetime, timezone

import pytest

from engagemate.growth.collector import GrowthCollector
from engagemate.growth.repository import DueAccount
from engagemate.onboarding.hypercrawl import DiscoveryError


NOW = datetime(2026, 8, 14, 3, tzinfo=timezone.utc)


def _account(username: str) -> DueAccount:
    return DueAccount(
        uuid.uuid4(), uuid.uuid4(), username, "UTC", date(2026, 8, 14)
    )


class FakeRepository:
    def __init__(self, due: list[DueAccount]) -> None:
        self.due = due
        self.list_calls: list[tuple[datetime, int, uuid.UUID | None]] = []
        self.writes: list[tuple[DueAccount, object, datetime, str | None]] = []
        self._lock = threading.Lock()

    def list_due(
        self,
        now: datetime,
        limit: int,
        *,
        user_id: uuid.UUID | None = None,
    ) -> list[DueAccount]:
        self.list_calls.append((now, limit, user_id))
        return self.due[:limit]

    def upsert(self, account, metrics, captured_at, failure_code=None) -> bool:
        with self._lock:
            self.writes.append((account, metrics, captured_at, failure_code))
        return True


class FakeHyperCrawl:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    async def profiles(self, urls: list[str]) -> list[object]:
        assert len(urls) == 1
        url = urls[0]
        self.urls.append(url)
        username = url.removeprefix("https://www.instagram.com/").removesuffix("/")
        response = self.responses[username]
        if isinstance(response, BaseException):
            raise response
        return [response]


@pytest.mark.asyncio
async def test_collects_complete_partial_and_safe_failure_cases_independently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    accounts = [
        _account("Complete.Account"),
        _account("partial_account"),
        _account("timeout_account"),
        _account("malformed_account"),
        _account("private_account"),
        _account("unavailable_account"),
    ]
    provider = FakeHyperCrawl(
        {
            "complete.account": {
                "followersCount": 1_000,
                "followingCount": 100,
                "postsCount": 20,
                "latestPosts": [{"likesCount": 20, "commentsCount": 5}],
            },
            "partial_account": {"followersCount": 500},
            "timeout_account": TimeoutError("provider-secret-timeout-body"),
            "malformed_account": {
                "followersCount": "provider-secret-malformed-body"
            },
            "private_account": {
                "isPrivate": True,
                "followersCount": 900,
                "followingCount": 100,
                "postsCount": 10,
            },
            "unavailable_account": {},
        }
    )
    repository = FakeRepository(accounts)
    collector = GrowthCollector(repository, provider, batch_size=25, concurrency=3)

    with caplog.at_level(logging.INFO):
        result = await collector.run(NOW)

    outcomes = {outcome.ig_account_id: outcome for outcome in result.outcomes}
    writes = {write[0].ig_account_id: write for write in repository.writes}
    assert len(outcomes) == 6
    assert len(writes) == 6
    assert outcomes[accounts[0].ig_account_id].status == "complete"
    assert outcomes[accounts[0].ig_account_id].failure_code is None
    assert outcomes[accounts[1].ig_account_id].status == "partial"
    assert outcomes[accounts[1].ig_account_id].failure_code is None
    assert outcomes[accounts[2].ig_account_id].failure_code == "provider_unavailable"
    assert outcomes[accounts[3].ig_account_id].failure_code == "invalid_profile"
    assert outcomes[accounts[4].ig_account_id].failure_code == "private_profile"
    assert outcomes[accounts[5].ig_account_id].failure_code == "invalid_profile"
    assert all(
        outcomes[account.ig_account_id].status == "unavailable"
        for account in accounts[2:]
    )
    assert [write[3] for write in repository.writes if write[3] is not None] == [
        outcomes[write[0].ig_account_id].failure_code
        for write in repository.writes
        if outcomes[write[0].ig_account_id].failure_code is not None
    ]
    persisted_and_logged = repr(repository.writes) + caplog.text
    assert "provider-secret-timeout-body" not in persisted_and_logged
    assert "provider-secret-malformed-body" not in persisted_and_logged
    assert result.succeeded == 2
    assert result.failed == 4


@pytest.mark.asyncio
async def test_builds_normalized_public_profile_urls() -> None:
    account = _account("  @Mixed.User  ")
    repository = FakeRepository([account])
    provider = FakeHyperCrawl(
        {
            "mixed.user": {
                "followersCount": 100,
                "followingCount": 20,
                "postsCount": 10,
                "latestPosts": [{"likesCount": 1, "commentsCount": 1}],
            }
        }
    )

    result = await GrowthCollector(
        repository, provider, batch_size=25, concurrency=1
    ).run(NOW)

    assert provider.urls == ["https://www.instagram.com/mixed.user/"]
    assert result.profile_urls == ("https://www.instagram.com/mixed.user/",)


@pytest.mark.asyncio
async def test_caps_simultaneous_provider_calls_at_configured_concurrency() -> None:
    accounts = [_account(f"account.{index}") for index in range(6)]

    class CountingHyperCrawl:
        def __init__(self) -> None:
            self.active = 0
            self.maximum = 0
            self.lock = asyncio.Lock()

        async def profiles(self, _urls: list[str]) -> list[dict[str, object]]:
            async with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0.01)
            async with self.lock:
                self.active -= 1
            return [{"followersCount": 100}]

    provider = CountingHyperCrawl()
    repository = FakeRepository(accounts)

    await GrowthCollector(repository, provider, batch_size=25, concurrency=2).run(NOW)

    assert provider.maximum == 2
    assert len(repository.writes) == 6


@pytest.mark.asyncio
async def test_caps_slow_upserts_at_configured_concurrency() -> None:
    accounts = [_account(f"account.{index}") for index in range(6)]

    class SlowRepository(FakeRepository):
        def __init__(self, due: list[DueAccount]) -> None:
            super().__init__(due)
            self.active = 0
            self.maximum = 0

        def upsert(self, account, metrics, captured_at, failure_code=None) -> bool:
            with self._lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            time.sleep(0.02)
            with self._lock:
                self.active -= 1
            return super().upsert(account, metrics, captured_at, failure_code)

    repository = SlowRepository(accounts)
    provider = FakeHyperCrawl(
        {account.username: {"followersCount": 100} for account in accounts}
    )

    await GrowthCollector(repository, provider, batch_size=25, concurrency=2).run(NOW)

    assert repository.maximum == 2
    assert len(repository.writes) == 6


@pytest.mark.asyncio
async def test_provider_error_does_not_suppress_other_accounts() -> None:
    failed = _account("failed")
    healthy = _account("healthy")
    repository = FakeRepository([failed, healthy])
    provider = FakeHyperCrawl(
        {
            "failed": DiscoveryError("raw-provider-response-secret"),
            "healthy": {"followersCount": 100},
        }
    )

    result = await GrowthCollector(
        repository, provider, batch_size=25, concurrency=2
    ).run(NOW)

    assert len(result.outcomes) == 2
    assert {outcome.status for outcome in result.outcomes} == {
        "partial",
        "unavailable",
    }
    assert len(repository.writes) == 2


@pytest.mark.asyncio
async def test_unscoped_run_supports_the_two_argument_repository_contract() -> None:
    account = _account("managed")

    class TwoArgumentRepository:
        def __init__(self) -> None:
            self.writes = []

        def list_due(self, now: datetime, limit: int) -> list[DueAccount]:
            assert (now, limit) == (NOW, 25)
            return [account]

        def upsert(self, *args) -> bool:
            self.writes.append(args)
            return True

    repository = TwoArgumentRepository()
    provider = FakeHyperCrawl({"managed": {"followersCount": 100}})

    result = await GrowthCollector(
        repository,  # type: ignore[arg-type]
        provider,
        batch_size=25,
        concurrency=1,
    ).run(NOW)

    assert result.succeeded == 1
    assert len(repository.writes) == 1


@pytest.mark.asyncio
async def test_dry_run_reads_due_accounts_without_provider_calls_or_writes() -> None:
    owner_id = uuid.uuid4()
    accounts = [
        DueAccount(
            owner_id,
            uuid.uuid4(),
            "@One.Account",
            "UTC",
            date(2026, 8, 14),
        ),
        DueAccount(
            owner_id,
            uuid.uuid4(),
            "two_account",
            "UTC",
            date(2026, 8, 14),
        ),
    ]
    repository = FakeRepository(accounts)
    provider = FakeHyperCrawl({})

    result = await GrowthCollector(
        repository, provider, batch_size=25, concurrency=4
    ).run(NOW, dry_run=True, user_id=owner_id)

    assert repository.list_calls == [(NOW, 25, owner_id)]
    assert provider.urls == []
    assert repository.writes == []
    assert result.due_account_ids == tuple(account.ig_account_id for account in accounts)
    assert result.profile_urls == (
        "https://www.instagram.com/one.account/",
        "https://www.instagram.com/two_account/",
    )
    assert result.outcomes == ()


@pytest.mark.asyncio
async def test_invalid_username_isolated_in_normal_and_dry_run_modes() -> None:
    invalid_accounts = [_account("  @  "), _account("bad/path")]
    valid = _account("healthy.account")
    provider = FakeHyperCrawl({"healthy.account": {"followersCount": 100}})
    repository = FakeRepository([*invalid_accounts, valid])

    result = await GrowthCollector(
        repository, provider, batch_size=25, concurrency=2
    ).run(NOW)

    outcomes = {outcome.ig_account_id: outcome for outcome in result.outcomes}
    for invalid in invalid_accounts:
        assert outcomes[invalid.ig_account_id].status == "unavailable"
        assert outcomes[invalid.ig_account_id].failure_code == "invalid_username"
    assert outcomes[valid.ig_account_id].status == "partial"
    assert provider.urls == ["https://www.instagram.com/healthy.account/"]
    assert len(repository.writes) == 3
    persisted_codes = {
        write[0].ig_account_id: write[3] for write in repository.writes
    }
    for invalid in invalid_accounts:
        assert persisted_codes[invalid.ig_account_id] == "invalid_username"
    assert result.profile_urls == ("https://www.instagram.com/healthy.account/",)

    dry_provider = FakeHyperCrawl({})
    dry_repository = FakeRepository([*invalid_accounts, valid])
    dry_result = await GrowthCollector(
        dry_repository, dry_provider, batch_size=25, concurrency=2
    ).run(NOW, dry_run=True)

    assert dry_provider.urls == []
    assert dry_repository.writes == []
    assert dry_result.profile_urls == (
        "https://www.instagram.com/healthy.account/",
    )
    assert dry_result.outcomes == tuple(
        type(result.outcomes[0])(
            invalid.ig_account_id,
            "unavailable",
            "invalid_username",
            False,
        )
        for invalid in invalid_accounts
    )
