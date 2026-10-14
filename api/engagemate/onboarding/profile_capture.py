"""Paced source-profile reads through the sender's existing bound proxy."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from playwright.async_api import async_playwright
from psycopg.types.json import Jsonb

from engagemate.browser.context import (
    BrowserContextFactory,
    BrowserRunContext,
    DecryptedCookieJar,
)
from engagemate.browser.fingerprint import (
    CapturedBrowserSession,
    FingerprintAccount,
    FingerprintFactory,
)
from engagemate.browser.lease import browser_lease
from engagemate.db.proxies import _ACTIVE_BINDING_SQL, ProxyRepository
from engagemate.db.text import text
from engagemate.domain.session_policy import INSTAGRAM_SESSION_MAX_AGE
from engagemate.proxy.base import Geo, ProxyHealthStatus
from engagemate.proxy.iproyal import IPRoyalProvider
from engagemate.security.session_cipher import SessionCipher
from engagemate.worker.follower_browser import (
    FollowerBrowserError,
    PlaywrightFollowerBrowser,
)


class ProfileCaptureError(RuntimeError):
    def __init__(self, reason, retry_after=None):
        self.reason, self.retry_after = reason, retry_after
        super().__init__(reason)


_TRANSIENT_PROXY_HEALTH = frozenset({"proxy_access_denied", "connection_failed"})


class ProfileCaptureService:
    def __init__(
        self,
        pool,
        settings,
        *,
        playwright_factory=async_playwright,
        browser_factory=BrowserContextFactory,
        adapter_factory=PlaywrightFollowerBrowser,
        sleep=asyncio.sleep,
    ):
        self.pool, self.settings = pool, settings
        self.playwright_factory, self.browser_factory = (
            playwright_factory,
            browser_factory,
        )
        self.adapter_factory, self.sleep = adapter_factory, sleep

    async def capture(self, user_id, account_id, usernames):
        # One onboarding browser across API processes; campaign workers share
        # the account lock so they cannot drive this session at the same time.
        with browser_lease(self.pool, account_id, profile_capture=True):
            cached = await asyncio.to_thread(
                self._cached, user_id, account_id, usernames
            )
            missing = [name for name in usernames if name not in cached]
            if not missing:
                return [cached[name] for name in usernames]
            await asyncio.to_thread(self._reserve, user_id, account_id)
            context = await asyncio.to_thread(self._load, user_id, account_id)
            try:
                async with asyncio.timeout(105):
                    async with self.playwright_factory() as playwright:
                        async with self.browser_factory(playwright=playwright).open(
                            context
                        ) as browser:
                            page = await browser.context.new_page()
                            adapter = self.adapter_factory(page)
                            for index, name in enumerate(missing):
                                if index:
                                    await self.sleep(5)
                                profile = await adapter.public_profile(name)
                                await asyncio.to_thread(
                                    self._save, user_id, account_id, name, profile
                                )
                                cached[name] = profile
            except FollowerBrowserError as error:
                restricted = error.reason in {
                    "rate_limited",
                    "checkpoint_required",
                    "authentication_required",
                }
                await asyncio.to_thread(
                    self._failure, user_id, account_id, error.reason, restricted
                )
                raise ProfileCaptureError(
                    error.reason, 1800 if restricted else 120
                ) from None
            except TimeoutError:
                await asyncio.to_thread(
                    self._failure, user_id, account_id, "profile_check_timeout", False
                )
                raise ProfileCaptureError("profile_check_timeout", 120) from None
            return [cached[name] for name in usernames]

    def _cached(self, user_id, account_id, usernames):
        with self.pool.connection() as connection:
            # Requesting arbitrary handles would turn this into an unbounded
            # scraping API. Only the current owned source list is eligible.
            rows = connection.execute(
                """
                SELECT r.username FROM brand_reference_accounts r
                JOIN brand_profiles p ON p.id=r.brand_profile_id
                WHERE p.user_id=%s AND p.ig_account_id=%s
            """,
                (user_id, account_id),
            ).fetchall()
            if set(usernames) != {text(row["username"]) for row in rows}:
                raise ProfileCaptureError("source_accounts_changed")
            rows = connection.execute(
                """
                SELECT username, profile FROM onboarding_profile_cache
                WHERE user_id=%s AND ig_account_id=%s AND username=ANY(%s)
                  AND captured_at > now() - interval '6 hours'
            """,
                (user_id, account_id, usernames),
            ).fetchall()
        return {text(row["username"]): row["profile"] for row in rows}

    def _reserve(self, user_id, account_id):
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO profile_check_state(user_id, ig_account_id, next_allowed_at)
                VALUES (%s,%s,now()+interval '2 minutes')
                ON CONFLICT(user_id,ig_account_id) DO UPDATE
                SET next_allowed_at=excluded.next_allowed_at, failure_reason=NULL
                WHERE profile_check_state.next_allowed_at <= now()
                RETURNING ig_account_id
            """,
                (user_id, account_id),
            ).fetchone()
            if row is None:
                raise ProfileCaptureError("profile_check_cooldown", 120)

    def _load(self, user_id, account_id):
        settings = self.settings
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT a.health_status, a.login_country, a.login_region, a.login_city,
                       s.encrypted_cookie_blob, s.captured_user_agent, s.captured_at, s.expires_at
                FROM ig_accounts a JOIN ig_sessions s ON s.ig_account_id=a.id AND s.user_id=a.user_id
                WHERE a.user_id=%s AND a.id=%s
                ORDER BY s.captured_at DESC LIMIT 1
            """,
                (user_id, account_id),
            ).fetchone()
            now = datetime.now(UTC)
            if row is None or text(row["health_status"]) != "active":
                raise ProfileCaptureError("reconnect_required")
            if row["captured_at"] < now - INSTAGRAM_SESSION_MAX_AGE or (
                row["expires_at"] and row["expires_at"] <= now
            ):
                raise ProfileCaptureError("reconnect_required")
            binding = connection.execute(
                _ACTIVE_BINDING_SQL + " AND proxy.health_status = 'active'",
                {"user_id": user_id, "account_id": account_id},
            ).fetchone()
            if binding is None:
                raise ProfileCaptureError("proxy_binding_required")
            # Read-only reuse: never allocate or rotate a proxy during a check.
            endpoint = ProxyRepository(
                self.pool, settings.session_encryption_key.get_secret_value()
            )._endpoint_from_row(binding, account_id)
        geo = Geo(
            text(row["login_country"]),
            text(row["login_region"]),
            text(row["login_city"]),
        )
        if endpoint.provider != "iproyal" or endpoint.geo != geo:
            raise ProfileCaptureError("proxy_binding_mismatch")
        provider = IPRoyalProvider.from_settings(settings)
        health = provider.check(endpoint)
        if health.reason in _TRANSIENT_PROXY_HEALTH:
            # The provider already retries once internally. A second bounded
            # preflight gives a short-lived CONNECT failure one more chance
            # without rotating the account proxy or replaying an action.
            time.sleep(1.0)
            health = provider.check(endpoint)
        if health.status is not ProxyHealthStatus.HEALTHY:
            raise ProfileCaptureError(health.reason)
        fingerprint = FingerprintFactory.from_settings(settings).for_account(
            FingerprintAccount(account_id, geo),
            CapturedBrowserSession(text(row["captured_user_agent"])),
        )
        cookies = DecryptedCookieJar.from_payload(
            SessionCipher(
                settings.session_encryption_key.get_secret_value(), account_id
            ).decrypt(text(row["encrypted_cookie_blob"]))
        )
        return BrowserRunContext(account_id, endpoint, fingerprint, cookies)

    def _save(self, user_id, account_id, username, profile):
        with self.pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO onboarding_profile_cache(user_id,ig_account_id,username,profile)
                VALUES (%s,%s,%s,%s) ON CONFLICT(user_id,ig_account_id,username)
                DO UPDATE SET profile=excluded.profile,captured_at=now()
            """,
                (user_id, account_id, username, Jsonb(profile)),
            )

    def _failure(self, user_id, account_id, reason, restricted):
        with self.pool.connection() as connection:
            connection.execute(
                """
                UPDATE profile_check_state SET next_allowed_at=now()+%s, failure_reason=%s
                WHERE user_id=%s AND ig_account_id=%s
            """,
                (
                    timedelta(minutes=30 if restricted else 2),
                    reason,
                    user_id,
                    account_id,
                ),
            )
            if restricted:
                signal = {
                    "rate_limited": "rate_limit",
                    "authentication_required": "login_required",
                    "checkpoint_required": "checkpoint",
                }.get(reason, "health_unavailable")
                paused = connection.execute(
                    """
                    UPDATE ig_accounts SET health_status='paused',paused_at=now(),pause_reason=%s,
                        updated_at=now()
                    WHERE user_id=%s AND id=%s
                      AND health_status IN ('active','degraded')
                    RETURNING id
                """,
                    (signal, user_id, account_id),
                ).fetchone()
                if paused is None:
                    return
                connection.execute(
                    """INSERT INTO account_health_events(user_id,ig_account_id,event_type,severity,evidence)
                       VALUES (%s,%s,%s,'warning',%s)""",
                    (
                        user_id,
                        account_id,
                        signal,
                        Jsonb({"source": "onboarding_profile_check", "reason": reason}),
                    ),
                )
