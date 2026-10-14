"""Reuse the sender's existing proxy and browser lease for relationship work."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from psycopg.types.json import Jsonb

from engagemate.browser.context import BrowserContextFactory
from engagemate.browser.lease import BrowserBusy, LeasedBrowserFactory
from engagemate.db.accounts import (
    AccountRepository,
    canonicalize_instagram_user_id,
    canonicalize_instagram_username,
)
from engagemate.db.text import text
from engagemate.onboarding.profile_capture import (
    ProfileCaptureError,
    ProfileCaptureService,
)
from engagemate.proxy.preflight import preflight_failure_evidence
from engagemate.worker.follow_maintenance import InterruptedFollowResult

_INSTAGRAM_IDENTITY_URL = (
    "https://www.instagram.com/api/v1/accounts/current_user/?edit=true"
)
_INSTAGRAM_WEB_APP_ID = "936619743392459"


class LiveFollowPerformer:
    def __init__(self, pool, settings, playwright):
        self.pool, self.settings, self.playwright = pool, settings, playwright

    def _sender(self, claim):
        with self.pool.connection() as connection:
            row = connection.execute(
                """SELECT ig_user_id, username FROM ig_accounts
                   WHERE id=%s AND user_id=%s AND health_status='active'
                     AND paused_at IS NULL""",
                (claim.ig_account_id, claim.user_id),
            ).fetchone()
        return None if row is None else (text(row["ig_user_id"]), text(row["username"]))

    async def _verified_current_identity(self, context, username, cookie_user_id):
        try:
            response = await context.request.get(
                _INSTAGRAM_IDENTITY_URL,
                headers={"X-IG-App-ID": _INSTAGRAM_WEB_APP_ID},
                timeout=8000,
                max_redirects=0,
            )
            if response.status != 200:
                return None
            payload = await response.json()
        except Exception:  # noqa: BLE001 - fail closed without leaking browser data
            return None
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict):
            return None
        identity_values = set()
        for key in ("pk", "pk_id", "id"):
            value = user.get(key)
            if type(value) not in (str, int):
                continue
            try:
                identity_values.add(canonicalize_instagram_user_id(str(value)))
            except ValueError:
                return None
        try:
            verified_username = canonicalize_instagram_username(user.get("username"))
        except (TypeError, ValueError):
            return None
        if identity_values != {cookie_user_id} or verified_username != username:
            return None
        return cookie_user_id

    def _pause(self, claim, signal):
        event = {
            "rate_limited": "rate_limit",
            "authentication_required": "login_required",
            "checkpoint_required": "checkpoint",
            "identity_mismatch": "identity_mismatch",
        }[signal]
        changed = AccountRepository(
            self.pool, self.settings.session_encryption_key.get_secret_value()
        ).set_paused(
            claim.user_id, claim.ig_account_id, paused=True,
            reason="login_required" if event == "login_required" else f"relationship_{event}",
        )
        if changed is not None:
            with self.pool.connection() as connection:
                connection.execute(
                    """INSERT INTO account_health_events
                       (user_id,ig_account_id,event_type,severity,evidence)
                       VALUES (%s,%s,%s,'warning',%s)""",
                    (claim.user_id, claim.ig_account_id, event,
                     Jsonb({"source": "relationship_maintenance", "reason": event})),
                )

    async def __call__(self, claim, before_mutation):
        return await self.perform_with_result(claim, before_mutation, None)

    async def perform_with_result(self, claim, before_mutation, on_result):
        observed_result = None

        def checked(outcome, evidence):
            from engagemate.worker.relationship_browser import (
                confirmed_relationship_evidence,
            )
            if outcome == "confirmed" and not confirmed_relationship_evidence(claim.action, evidence):
                return "uncertain", {"reason": "relationship_receipt_unverified"}
            return outcome, dict(evidence)

        async def observed(outcome, evidence):
            nonlocal observed_result
            outcome, evidence = checked(outcome, evidence)
            observed_result = outcome, evidence
            if on_result is not None:
                await on_result(outcome, evidence)

        try:
            outcome, evidence = await self._perform_with_result(claim, before_mutation, observed)
            return checked(outcome, evidence)
        except asyncio.CancelledError as error:
            # Preflight or third-party carriers never prove this action. Only
            # a locally observed receipt/readback result may cross the boundary.
            error.__dict__.pop("_engagemate_follow_result", None)
            if observed_result is not None:
                error._engagemate_follow_result = InterruptedFollowResult(
                    observed_result[0], dict(observed_result[1]))
            raise


    async def _perform_with_result(self, claim, before_mutation, on_result):
        from engagemate.worker.relationship_browser import RelationshipBrowser

        account_identity = await asyncio.to_thread(self._sender, claim)
        if not account_identity:
            return "deferred", {"reason": "sender_unavailable"}
        sender, sender_username = account_identity
        if sender == claim.owner_id:
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
        interruption = None

        @asynccontextmanager
        async def opened():
            nonlocal interruption
            async with factory.open(bound) as browser:
                try:
                    yield browser
                except asyncio.CancelledError as error:
                    interruption = error
                    raise

        async def receive_result(outcome, evidence):
            nonlocal result
            result = outcome, dict(evidence)
            if on_result is not None:
                await on_result(outcome, result[1])

        try:
            async with opened() as browser:
                cookies = await browser.context.cookies("https://www.instagram.com")
                identity_values = [
                    c["value"]
                    for c in cookies
                    if c.get("name") == "ds_user_id"
                    and isinstance(c.get("value"), str)
                ]
                identities = set(identity_values)
                del cookies
                if identities != {sender}:
                    repaired_identity = None
                    if len(identity_values) == 1:
                        try:
                            observed_id = canonicalize_instagram_user_id(identity_values[0])
                        except ValueError:
                            observed_id = None
                        if observed_id is not None:
                            repaired_identity = await self._verified_current_identity(
                                browser.context, sender_username, observed_id
                            )
                            if repaired_identity is not None:
                                repaired = await asyncio.to_thread(
                                    AccountRepository(
                                        self.pool,
                                        self.settings.session_encryption_key.get_secret_value(),
                                    ).repair_verified_identity,
                                    claim.user_id,
                                    claim.ig_account_id,
                                    expected_ig_user_id=sender,
                                    verified_ig_user_id=repaired_identity,
                                    verified_username=sender_username,
                                )
                                if not repaired:
                                    repaired_identity = None
                    if repaired_identity is None:
                        await asyncio.to_thread(
                            self._pause, claim, "identity_mismatch"
                        )
                        return "deferred", {"reason": "sender_identity_mismatch"}
                    sender = repaired_identity
                adapter = RelationshipBrowser(browser.context)
                observed_perform = getattr(adapter, "perform_with_result", None)
                if callable(observed_perform):
                    outcome, evidence = await observed_perform(
                        claim.handle, claim.owner_id, claim.action, before_mutation, receive_result,
                    )
                else:
                    outcome, evidence = await adapter.perform(
                        claim.handle, claim.owner_id, claim.action, before_mutation,
                    )
                    await receive_result(outcome, evidence)
                # Returned cleanup flags remain diagnostic; the first result
                # was already completed before page/context cleanup or pause.
                result = outcome, dict(evidence)
                evidence = result[1]
                if evidence.get("health_signal") in {
                    "rate_limited", "authentication_required", "checkpoint_required",
                }:
                    try:
                        await asyncio.to_thread(self._pause, claim, evidence["health_signal"])
                    except Exception:  # noqa: BLE001 - retain receipt if health persistence fails
                        evidence["pause_persistence_failed"] = True
                return result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if interruption is not None:
                raise interruption from None
            if result is not None:
                result[1]["browser_cleanup_failed"] = True
                return result
            if isinstance(error, BrowserBusy):
                return "deferred", {"reason": "account_browser_busy"}
            raise

class StoryFollowPerformer:
    """Follow one Story target inside the Story run's leased browser.

    The maintenance lane opens its own leased context and therefore cannot
    satisfy a follow-before-reply ordering while the Story job is running.
    This adapter reuses the already identity-bound context, while retaining
    RelationshipBrowser's native receipt and readback guards.
    """

    def __init__(self, pool, browser_context):
        self.pool = pool
        self.browser_context = browser_context

    def _sender(self, claim):
        with self.pool.connection() as connection:
            row = connection.execute(
                """SELECT ig_user_id, username FROM ig_accounts
                   WHERE id=%s AND user_id=%s AND health_status='active'
                     AND paused_at IS NULL""",
                (claim.ig_account_id, claim.user_id),
            ).fetchone()
        return None if row is None else (text(row["ig_user_id"]), text(row["username"]))

    async def perform_with_result(self, claim, before_mutation, on_result):
        from engagemate.worker.relationship_browser import (
            RelationshipBrowser,
            confirmed_relationship_evidence,
        )

        account_identity = await asyncio.to_thread(self._sender, claim)
        if not account_identity:
            return "deferred", {"reason": "sender_unavailable"}
        sender, sender_username = account_identity
        try:
            cookies = await self.browser_context.cookies("https://www.instagram.com")
            identities = {
                c["value"] for c in cookies
                if c.get("name") == "ds_user_id" and isinstance(c.get("value"), str)
            }
            if identities != {sender}:
                return "deferred", {"reason": "sender_identity_mismatch"}
            verified = await LiveFollowPerformer(
                self.pool, None, None
            )._verified_current_identity(self.browser_context, sender_username, sender)
            if verified != sender:
                return "deferred", {"reason": "sender_identity_mismatch"}
        except Exception:  # noqa: BLE001 - identity failures must block the write
            return "deferred", {"reason": "sender_identity_unavailable"}

        observed_result = None

        def checked(outcome, evidence):
            if outcome == "confirmed" and not confirmed_relationship_evidence("follow", evidence):
                return "uncertain", {"reason": "relationship_receipt_unverified"}
            return outcome, dict(evidence)

        async def observed(outcome, evidence):
            nonlocal observed_result
            outcome, evidence = checked(outcome, evidence)
            observed_result = outcome, evidence
            if on_result is not None:
                await on_result(outcome, evidence)

        try:
            outcome, evidence = await RelationshipBrowser(
                self.browser_context
            ).perform_with_result(
                claim.handle, claim.owner_id, "follow", before_mutation, observed,
            )
            return checked(outcome, evidence)
        except asyncio.CancelledError as error:
            error.__dict__.pop("_engagemate_follow_result", None)
            if observed_result is not None:
                error._engagemate_follow_result = InterruptedFollowResult(
                    observed_result[0], dict(observed_result[1])
                )
            raise
