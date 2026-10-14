"""Observe a single relationship through the sender's leased, read-only browser."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

from engagemate.browser.context import BrowserContextFactory
from engagemate.browser.lease import BrowserBusy, LeasedBrowserFactory
from engagemate.onboarding.profile_capture import (
    ProfileCaptureError,
    ProfileCaptureService,
)
from engagemate.worker.follow_runtime import LiveFollowPerformer
from engagemate.worker.follower_browser import (
    FollowerBrowserError,
    PlaywrightFollowerBrowser,
)
from engagemate.worker.relationship_browser import _read_request


def validated_sender(account_identity, owner_id):
    if type(account_identity) is not tuple or len(account_identity) != 2:
        return None

    sender, username = account_identity

    if type(sender) is not str or type(username) is not str:
        return None
    if not username or not sender.isdecimal():
        return None
    if type(owner_id) is not str or not owner_id.isdecimal():
        return None
    if sender == owner_id:
        return None

    return sender


class LiveReconciliationReader:
    def __init__(self, pool, settings, playwright, *, clock=None):
        self.pool, self.settings, self.playwright = pool, settings, playwright
        self.clock = clock or (lambda: datetime.now(UTC))
        # Reuse account-scoped identity and restriction pausing, never its performer.
        self.accounts = LiveFollowPerformer(pool, settings, playwright)

    async def __call__(self, claim, permitted):
        if not await permitted():
            return None, "policy_changed"
        if claim.cached_observation:
            return claim.cached_observation, None
        account_identity = await asyncio.to_thread(self.accounts._sender, claim)
        sender = validated_sender(account_identity, claim.owner_id)
        if sender is None:
            return None, "identity_mismatch"
        try:
            bound = await asyncio.to_thread(
                ProfileCaptureService(self.pool, self.settings)._load,
                claim.user_id,
                claim.ig_account_id,
            )
        except ProfileCaptureError as error:
            if error.reason == "reconnect_required":
                await asyncio.to_thread(
                    self.accounts._pause, claim, "authentication_required"
                )
            return None, "session_unavailable"
        driver = self.playwright

        class Chromium:
            async def launch_persistent_context(self, **kwargs):
                kwargs["service_workers"] = "block"
                return await driver.chromium.launch_persistent_context(**kwargs)

        factory = LeasedBrowserFactory(
            self.pool,
            BrowserContextFactory(playwright=SimpleNamespace(chromium=Chromium())),
        )
        blocked_health = None
        try:
            async with factory.open(bound) as browser:
                identities = {
                    c["value"]
                    for c in await browser.context.cookies("https://www.instagram.com")
                    if c.get("name") == "ds_user_id"
                }
                if identities != {sender}:
                    return None, "identity_mismatch"
                if not await permitted():
                    return None, "policy_changed"

                # Guard the entire context, including any page/popup created by the site.
                async def guard(route):
                    nonlocal blocked_health
                    url = urlsplit(route.request.url)
                    if route.request.resource_type == "document" and url.hostname in {
                        "www.instagram.com",
                        "instagram.com",
                    }:
                        if url.path.startswith(("/challenge/", "/checkpoint/")):
                            blocked_health = "checkpoint_required"
                        elif url.path.startswith("/accounts/"):
                            blocked_health = "authentication_required"
                    if blocked_health:
                        await route.abort()
                    elif _read_request(route.request, claim.handle, claim.owner_id):
                        await route.continue_()
                    else:
                        await route.abort()

                async def block_socket(socket):
                    await socket.close()

                def observe_status(response):
                    nonlocal blocked_health
                    if urlsplit(response.url).hostname in {
                        "www.instagram.com",
                        "instagram.com",
                    }:
                        if response.status == 429:
                            blocked_health = "rate_limited"
                        elif response.status == 401:
                            blocked_health = "authentication_required"
                        elif response.status == 403 and not blocked_health:
                            blocked_health = "response_failed"

                browser.context.on("response", observe_status)
                await browser.context.route("**/*", guard)
                await browser.context.route_web_socket("**/*", block_socket)
                page = await browser.context.new_page()
                try:
                    profile = await PlaywrightFollowerBrowser(page).inspect(
                        claim.handle
                    )
                    if blocked_health:
                        raise FollowerBrowserError(blocked_health)
                    if (
                        profile.get("owner_id") != claim.owner_id
                        or profile.get("handle") != claim.handle
                    ):
                        return None, "identity_mismatch"
                    if type(profile.get("followed_by")) is not bool:
                        return None, "unknown"
                    return {
                        "owner_id": claim.owner_id,
                        "identity_matches": True,
                        "followed_by": profile["followed_by"],
                        "observed_at": self.clock().isoformat(),
                    }, None
                finally:
                    await page.close()
        except BrowserBusy:
            return None, "account_browser_busy"
        except FollowerBrowserError as error:
            signal = error.reason if error.reason in {
                "rate_limited", "authentication_required", "checkpoint_required"
            } else blocked_health or error.reason
            if signal in {
                "rate_limited",
                "authentication_required",
                "checkpoint_required",
            }:
                await asyncio.to_thread(self.accounts._pause, claim, signal)
                return None, signal
            return None, "profile_unreadable"
