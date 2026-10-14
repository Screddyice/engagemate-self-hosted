"""Run a post-follow message inside the sender's existing browser lease."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from engagemate.browser.context import BrowserContextFactory
from engagemate.browser.lease import BrowserBusy, LeasedBrowserFactory
from engagemate.onboarding.profile_capture import ProfileCaptureError, ProfileCaptureService
from engagemate.proxy.preflight import preflight_failure_evidence
from engagemate.worker.follow_reel_browser import FollowReelBrowser, ReelBrowserError
from engagemate.worker.follow_reels import choose_reel, compose_message
from engagemate.worker.follow_reel_maintenance import InterruptedReelResult
from engagemate.worker.follow_runtime import LiveFollowPerformer


class LiveFollowReelPerformer(LiveFollowPerformer):
    async def __call__(self, claim, prepare, before_mutation):
        return await self.perform_with_result(claim, prepare, before_mutation, None)

    async def perform_with_result(self, claim, prepare, before_mutation, on_result):
        identity = await asyncio.to_thread(self._sender, claim)
        if identity != (claim.sender_id, claim.sender_handle):
            return "deferred", {"reason": "sender_unavailable"}
        try:
            bound = await asyncio.to_thread(
                ProfileCaptureService(self.pool, self.settings)._load,
                claim.user_id, claim.ig_account_id,
            )
        except ProfileCaptureError as error:
            if error.reason == "reconnect_required":
                await asyncio.to_thread(self._pause, claim, "authentication_required")
                return "deferred", {"reason": "reconnect_required"}
            return "deferred", preflight_failure_evidence(error.reason)
        driver = self.playwright

        class Chromium:
            async def launch_persistent_context(self, **kwargs):
                kwargs["service_workers"] = "block"
                return await driver.chromium.launch_persistent_context(**kwargs)

        factory = LeasedBrowserFactory(self.pool, BrowserContextFactory(
            playwright=SimpleNamespace(chromium=Chromium()),
        ))
        result = None
        cancelled = False
        try:
            async with factory.open(bound) as browser:
                adapter = FollowReelBrowser(browser.context)
                try:
                    await adapter.verify_accounts(claim.sender_id, claim.sender_handle,
                                                  claim.recipient_id, claim.handle)
                    if claim.reel_url and claim.body:
                        reel_url, body = claim.reel_url, claim.body
                    else:
                        reels = await adapter.read_reels(claim.sender_id)
                        if len({r.code for r in reels}) < 3:
                            return "deferred", {"reason": "three_sender_reels_required"}
                        reel = choose_reel(reels, claim.sender_id, claim.rotation_slot)
                        reel_url, body = reel.url, compose_message(reel.url, claim.message_template)
                    if not await prepare(reel_url, body):
                        return "deferred", {"reason": "message_preparation_refused"}
                    outcome, evidence = await adapter.send(
                        sender_id=claim.sender_id, recipient_id=claim.recipient_id,
                        reel_url=reel_url, body=body, before_mutation=before_mutation,
                    )
                    evidence = dict(evidence)
                    result = outcome, evidence
                    # Finish while the claim and browser leases are still live.
                    # Browser teardown may wait beyond the claim's expiry.
                    if on_result is not None:
                        await on_result(outcome, evidence)
                    if evidence.get("health_signal"):
                        try:
                            await asyncio.to_thread(self._pause, claim, evidence["health_signal"])
                        except Exception:
                            evidence["pause_persistence_failed"] = True
                    return result
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                finally:
                    close = getattr(adapter, "close", None)
                    if close is not None:
                        try:
                            await close()
                        except asyncio.CancelledError:
                            cancelled = True
                            raise
                        except Exception:
                            if result is not None:
                                result[1]["adapter_cleanup_failed"] = True
                            elif not cancelled:
                                raise
        except asyncio.CancelledError as error:
            if result is not None:
                error._engagemate_reel_result = InterruptedReelResult(result[0], dict(result[1]))
            raise
        except Exception as error:
            # Context cleanup must not erase an external receipt or an uncertain
            # write. Keep only fixed diagnostic flags, never exception text.
            if cancelled:
                interruption = asyncio.CancelledError()
                if result is not None:
                    interruption._engagemate_reel_result = InterruptedReelResult(result[0], dict(result[1]))
                raise interruption from None
            if result is not None:
                result[1]["browser_cleanup_failed"] = True
                return result
            if isinstance(error, BrowserBusy):
                return "deferred", {"reason": "account_browser_busy"}
            if isinstance(error, ReelBrowserError):
                if error.health_signal:
                    await asyncio.to_thread(self._pause, claim, error.health_signal)
                return "deferred", {"reason": error.reason}
            raise
