"""Bounded collection of public managed-account growth observations."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from engagemate.growth.metrics import ProfileMetrics, parse_profile_metrics
from engagemate.growth.repository import DueAccount
from engagemate.onboarding.hypercrawl import DiscoveryError

logger = logging.getLogger(__name__)


class GrowthRepositoryPort(Protocol):
    def list_due(
        self,
        now: datetime,
        limit: int,
        *,
        user_id: UUID | None = None,
    ) -> list[DueAccount]: ...

    def upsert(
        self,
        account: DueAccount,
        metrics: ProfileMetrics,
        captured_at: datetime,
        failure_code: str | None = None,
    ) -> bool: ...


class HyperCrawlPort(Protocol):
    async def profiles(self, reference_urls: list[str]) -> list[object]: ...


@dataclass(frozen=True, slots=True)
class AccountCollectionResult:
    ig_account_id: UUID
    status: Literal["complete", "partial", "unavailable"]
    failure_code: str | None
    accepted: bool


@dataclass(frozen=True, slots=True)
class CollectionResult:
    due_account_ids: tuple[UUID, ...]
    profile_urls: tuple[str, ...]
    outcomes: tuple[AccountCollectionResult, ...]

    @property
    def succeeded(self) -> int:
        return sum(outcome.status != "unavailable" for outcome in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(outcome.status == "unavailable" for outcome in self.outcomes)


def _unavailable() -> ProfileMetrics:
    return ProfileMetrics(None, None, None, None, 0, "unavailable")


class GrowthCollector:
    """Collect one public snapshot per due account with bounded concurrency."""

    def __init__(
        self,
        repository: GrowthRepositoryPort,
        hypercrawl: HyperCrawlPort,
        *,
        batch_size: int,
        concurrency: int,
    ) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        if type(concurrency) is not int or concurrency <= 0:
            raise ValueError("concurrency must be a positive integer")
        self._repository = repository
        self._hypercrawl = hypercrawl
        self._batch_size = batch_size
        self._concurrency = concurrency

    async def run(
        self,
        now: datetime,
        dry_run: bool = False,
        user_id: UUID | None = None,
    ) -> CollectionResult:
        """Read one due batch and collect each account independently."""

        if user_id is None:
            accounts = await asyncio.to_thread(
                self._repository.list_due,
                now,
                self._batch_size,
            )
        else:
            accounts = await asyncio.to_thread(
                self._repository.list_due,
                now,
                self._batch_size,
                user_id=user_id,
            )
        account_ids = tuple(account.ig_account_id for account in accounts)
        semaphore = asyncio.Semaphore(self._concurrency)
        if dry_run:
            planned = await asyncio.gather(
                *(self._dry_run_one(account, semaphore) for account in accounts)
            )
            return CollectionResult(
                account_ids,
                tuple(url for _, url in planned if url is not None),
                tuple(outcome for outcome, _ in planned if outcome is not None),
            )

        collected = await asyncio.gather(
            *(
                self._collect_one(account, now, semaphore)
                for account in accounts
            )
        )
        return CollectionResult(
            account_ids,
            tuple(url for _, url in collected if url is not None),
            tuple(outcome for outcome, _ in collected),
        )

    async def _dry_run_one(
        self,
        account: DueAccount,
        semaphore: asyncio.Semaphore,
    ) -> tuple[AccountCollectionResult | None, str | None]:
        async with semaphore:
            try:
                profile_url = account.profile_url
            except (TypeError, ValueError):
                logger.info(
                    "growth account planned account=%s status=unavailable "
                    "code=invalid_username",
                    account.ig_account_id,
                )
                return (
                    AccountCollectionResult(
                        account.ig_account_id,
                        "unavailable",
                        "invalid_username",
                        False,
                    ),
                    None,
                )
        return None, profile_url

    async def _collect_one(
        self,
        account: DueAccount,
        captured_at: datetime,
        semaphore: asyncio.Semaphore,
    ) -> tuple[AccountCollectionResult, str | None]:
        async with semaphore:
            try:
                profile_url = account.profile_url
            except (TypeError, ValueError):
                profile_url = None
                metrics = _unavailable()
                failure_code = "invalid_username"
            else:
                metrics, failure_code = await self._fetch_metrics(profile_url)

            try:
                accepted = await asyncio.to_thread(
                    self._repository.upsert,
                    account,
                    metrics,
                    captured_at,
                    failure_code,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one account cannot stop the batch
                logger.error(
                    "growth account finished account=%s status=unavailable "
                    "code=persistence_failed",
                    account.ig_account_id,
                )
                return (
                    AccountCollectionResult(
                        account.ig_account_id,
                        "unavailable",
                        "persistence_failed",
                        False,
                    ),
                    profile_url,
                )

            logger.info(
                "growth account finished account=%s status=%s code=%s",
                account.ig_account_id,
                metrics.status,
                failure_code or "none",
            )
            return (
                AccountCollectionResult(
                    account.ig_account_id,
                    metrics.status,
                    failure_code,
                    accepted,
                ),
                profile_url,
            )

    async def _fetch_metrics(
        self, profile_url: str
    ) -> tuple[ProfileMetrics, str | None]:
        try:
            profiles = await self._hypercrawl.profiles([profile_url])
            if len(profiles) != 1:
                return _unavailable(), "invalid_profile"
            profile = profiles[0]
            if isinstance(profile, dict) and profile.get("isPrivate") is True:
                return _unavailable(), "private_profile"
            metrics = parse_profile_metrics(profile)
            failure_code = (
                "invalid_profile" if metrics.status == "unavailable" else None
            )
            return metrics, failure_code
        except asyncio.CancelledError:
            raise
        except DiscoveryError:
            return _unavailable(), "provider_unavailable"
        except TimeoutError:
            return _unavailable(), "provider_unavailable"
        except (TypeError, ValueError):
            return _unavailable(), "invalid_profile"
        except Exception:  # noqa: BLE001 - provider details must stay redacted
            return _unavailable(), "provider_unavailable"
