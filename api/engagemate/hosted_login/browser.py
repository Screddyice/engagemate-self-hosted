"""Disposable Chrome controlled only by the signed-in customer."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

from engagemate.browser.fingerprint import FingerprintGeoRegistry
from engagemate.control.routes.accounts import CookieCapture, LoginGeo, SessionCapture
from engagemate.hosted_login.protocol import (
    WIDTH, HEIGHT, BrowserCleanupError, ChooseAction, Command, ConnectError,
    SignIn, SubmitCode, allowed_destination,
)

LOGIN_URL = "https://www.instagram.com/accounts/login/"
logger = logging.getLogger(__name__)
COOKIE_NAMES = {"sessionid", "ds_user_id", "csrftoken", "mid", "ig_did", "rur", "datr"}
ACTION_LABELS = {
    "send_code": {"send code", "send security code", "send login code"},
    "continue_verification": {"continue", "next"},
    "resend_code": {"resend code", "send new code", "get new code", "get a new code", "send code again", "resend security code"},
    "try_another_way": {"try another way", "try another method", "use another method"},
    "use_sms": {"text message", "text message (sms)", "sms", "send code via sms instead"},
    "use_whatsapp": {"whatsapp", "whatsapp message"},
    "use_email": {"email", "email address"},
    "use_authenticator": {"authentication app", "authenticator app"},
    "use_backup_code": {"use backup code", "backup code", "backup codes", "recovery code"},
    "confirm_login": {"this was me", "it was me", "yes, it's me", "approve login"},
}
CODE_SELECTOR = (
    'input[autocomplete="one-time-code"]:visible, input[name*="code" i]:visible, '
    'input[id*="code" i]:visible, input[aria-label*="code" i]:visible, '
    'input[placeholder*="code" i]:visible'
)


def code_channel(instructions: str) -> str:
    """Classify explicit code instructions, never an alternate-method button."""
    matches = set()
    for line in instructions.casefold().splitlines():
        if not re.search(r"\bcode(?:s)?\b", line):
            continue
        if re.search(r"\b(instead|another|different|try|resend)\b|send (?:a |the )?(?:new )?code", line):
            continue
        if not re.search(r"\b(sent|received|enter|from|generated|use|find)\b", line):
            continue
        for channel, pattern in {
            "whatsapp": r"whatsapp",
            "sms": r"text message|\bsms\b",
            "email": r"e-?mail",
            "authenticator": r"authenticat(?:or|ion) app|google authenticator|authy",
            "backup": r"backup code|recovery code",
        }.items():
            if re.search(pattern, line):
                matches.add(channel)
    return next(iter(matches)) if len(matches) == 1 else "unknown"


class LoginBrowser:
    def __init__(self, browser, context, page, geo):
        self.browser, self.context, self.page, self.geo = browser, context, page, geo

    def __repr__(self):
        return "LoginBrowser(resources=<private>)"

    @classmethod
    async def open(cls, playwright, reservation, settings):
        browser = None
        try:
            timezone, locale = FingerprintGeoRegistry.from_settings(settings).require(reservation.geo)
            endpoint = reservation.endpoint
            executable = settings.hosted_login_browser_executable
            browser = await playwright.chromium.launch(
                **({"executable_path": executable} if executable else {"channel": "chrome"}),
                headless=True,
                proxy={"server": endpoint.server, "username": endpoint.username,
                       "password": endpoint.password},
                args=["--disable-background-networking", "--disable-breakpad",
                      "--disable-component-update", "--disable-sync",
                      "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
            )
            context = await browser.new_context(
                viewport={"width": WIDTH, "height": HEIGHT},
                timezone_id=timezone, locale=locale,
                accept_downloads=False, service_workers="block",
                permissions=[],
            )
            context.set_default_timeout(8000)
            context.set_default_navigation_timeout(30000)

            async def route_request(route):
                request = route.request
                # Challenge iframes may load external assets, but the top page
                # stays on Instagram. Browser request routing covers redirects.
                top_navigation = request.is_navigation_request() and request.frame.parent_frame is None
                if allowed_destination(request.url, navigation=top_navigation):
                    await route.continue_()
                else:
                    await route.abort()

            await context.route("**/*", route_request)
            await context.route_web_socket("**/*", lambda ws: ws.close())
            page = await context.new_page()

            async def close_extra(extra):
                if extra != page:
                    with suppress(Exception):
                        await extra.close()

            context.on("page", close_extra)
            page.on("dialog", lambda dialog: dialog.dismiss())
            page.on("download", lambda download: download.cancel())
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            return cls(browser, context, page, reservation.geo)
        except BaseException:
            if browser is not None:
                try:
                    await asyncio.wait_for(browser.close(), 10)
                except BaseException:
                    raise BrowserCleanupError(browser) from None
            raise

    async def input(self, command: Command):
        if isinstance(command, SignIn):
            await self.sign_in(command.username, command.password)
        elif isinstance(command, SubmitCode):
            await self.submit_code(command.code)
        elif isinstance(command, ChooseAction):
            await self.choose_action(command.action)

    def _action_controls(self):
        return self.page.locator('button:visible, [role="button"]:visible, [role="radio"]:visible, [role="link"]:visible, label:visible, a:visible')

    async def _available_actions(self):
        labels = await self._action_controls().all_text_contents()
        normalized = [" ".join(value.split()).casefold() for value in labels]
        actions = {}
        for key, names in ACTION_LABELS.items():
            matches = [index for index, label in enumerate(normalized) if label in names]
            if len(matches) == 1:
                actions[key] = matches[0]
        return actions

    async def step(self) -> dict:
        """Expose only fixed step names, never Instagram text or a screenshot."""
        if not allowed_destination(self.page.url, navigation=True):
            return {"kind": "unsupported", "options": []}
        parsed = urlsplit(self.page.url)
        if parsed.hostname != "www.instagram.com":
            return {"kind": "unsupported", "options": []}
        path = parsed.path
        auth_path = path.startswith(("/accounts/login", "/challenge", "/checkpoint")) or "/two_factor" in path
        if not auth_path:
            cookies = await self.context.cookies(["https://www.instagram.com/"])
            try:
                by_name = {cookie["name"]: cookie for cookie in cookies}
                session = by_name.get("sessionid", {})
                if (by_name.get("ds_user_id", {}).get("value", "").isdigit()
                        and session.get("value") and session.get("httpOnly") and session.get("secure")):
                    return {"kind": "ready", "options": []}
            finally:
                for cookie in cookies:
                    cookie.clear()
                cookies.clear()
            # Navigation alone is not proof of a signed-in session. Still inspect
            # visible verification controls below, including unfamiliar routes.
        code_fields = self.page.locator(CODE_SELECTOR)
        count = await code_fields.count()
        if count not in (1, 4, 6, 8):
            count = await self.page.locator('input[maxlength="1"]:visible').count()
        actions = await self._available_actions()
        if count in (1, 4, 6, 8):
            body = (await self.page.locator("body").inner_text(timeout=5000))[:12000].casefold()
            channel = code_channel(body)
            invalid = any(phrase in body for phrase in ("incorrect code", "invalid code", "expired code"))
            return {"kind": "code", "channel": channel, "invalid": invalid,
                    "options": [key for key in actions if key not in {"confirm_login", "continue_verification"}]}
        if path.rstrip("/") == "/accounts/login":
            return {"kind": "login", "options": []}
        body = (await self.page.locator("body").inner_text(timeout=5000))[:12000].casefold()
        if any(phrase in body for phrase in ("approve the login", "approve this login", "check your phone",
                                             "another device", "waiting for approval")):
            return {"kind": "approval", "options": [key for key in actions if key != "continue_verification"]}
        if actions:
            return {"kind": "method", "options": list(actions)}
        return {"kind": "unsupported", "options": []}

    async def submit_code(self, code: str) -> None:
        if (await self.step())["kind"] != "code":
            raise ConnectError("sign_in_required", 422)
        fields = self.page.locator(CODE_SELECTOR)
        count = await fields.count()
        if count == 1:
            await fields.fill(code, timeout=5000)
        else:
            fields = self.page.locator('input[maxlength="1"]:visible')
            count = await fields.count()
            digits = re.sub(r"[ -]", "", code)
            if count not in (4, 6, 8) or len(digits) != count:
                raise ConnectError("invalid_input", 422)
            for index, digit in enumerate(digits):
                await fields.nth(index).fill(digit, timeout=5000)
        submit = self.page.locator('button[type="submit"]:visible')
        if await submit.count() != 1:
            submit = self.page.get_by_role("button", name=re.compile(r"^(confirm|verify|continue|next|submit)$", re.I))
        if await submit.count() != 1:
            raise ConnectError("sign_in_required", 422)
        await submit.click(timeout=5000, no_wait_after=True)

    async def choose_action(self, action: str) -> None:
        step = await self.step()
        if step["kind"] not in ("code", "approval", "method") or action not in step["options"]:
            raise ConnectError("sign_in_required", 422)
        controls = self._action_controls()
        index = (await self._available_actions()).get(action)
        if index is None:
            raise ConnectError("sign_in_required", 422)
        await controls.nth(index).click(timeout=5000, no_wait_after=True)

    async def sign_in(self, username: str, password: str) -> None:
        """Fill Instagram's visible login form; never store or return a credential."""
        parsed = urlsplit(self.page.url)
        if parsed.scheme != "https" or parsed.hostname != "www.instagram.com" or parsed.path != "/accounts/login/":
            raise ConnectError("sign_in_required", 422)
        chooser = self.page.get_by_role("button", name="Use another profile", exact=True)
        if await chooser.count() == 1 and await chooser.is_visible():
            await chooser.click(timeout=5000)
        user_field = self.page.locator('input[name="email"]:visible')
        pass_field = self.page.locator('input[name="pass"][type="password"]:visible')
        submit = self.page.get_by_role("button", name="Log In", exact=True)
        if (await user_field.count() != 1 or await pass_field.count() != 1
                or await submit.count() != 1):
            raise ConnectError("sign_in_required", 422)
        await user_field.fill(username, timeout=5000)
        await pass_field.fill(password, timeout=5000)
        if not await submit.is_enabled():
            raise ConnectError("sign_in_required", 422)
        await submit.click(timeout=5000, no_wait_after=True)

    async def capture(self, expected_username: str) -> SessionCapture:
        try:
            return await asyncio.wait_for(self._capture(expected_username), timeout=12)
        except ConnectError:
            raise
        except Exception as error:
            # Browser navigation failures and delayed bootstrap data are retryable.
            # Never log exception text: upstream URLs may contain credentials.
            logger.warning("hosted_login_capture unavailable type=%s", type(error).__name__)
            raise ConnectError("identity_check_unavailable", 503) from None

    async def _capture(self, expected_username: str) -> SessionCapture:
        if not allowed_destination(self.page.url, navigation=True):
            raise ConnectError("sign_in_required", 422)
        path = urlsplit(self.page.url).path
        if any(part in path for part in ("/challenge", "/checkpoint", "/two_factor")):
            raise ConnectError("sign_in_required", 422)
        if path.rstrip("/") == "/accounts/onetap":
            dismiss = self.page.get_by_role("button", name="Not now", exact=True)
            if await dismiss.count() == 1 and await dismiss.is_visible():
                await dismiss.click(timeout=5000)
                await self.page.wait_for_url(
                    lambda url: urlsplit(str(url)).path.rstrip("/") != "/accounts/onetap",
                    timeout=5000, wait_until="domcontentloaded",
                )
            if not allowed_destination(self.page.url, navigation=True):
                raise ConnectError("sign_in_required", 422)
            path = urlsplit(self.page.url).path
            if any(part in path for part in ("/accounts/login", "/challenge", "/checkpoint", "/two_factor")):
                raise ConnectError("sign_in_required", 422)
        cookies = await self.context.cookies(["https://www.instagram.com/"])
        try:
            by_name = {cookie["name"]: cookie for cookie in cookies}
            numeric_id = by_name.get("ds_user_id", {}).get("value", "")
            session = by_name.get("sessionid", {})
            if (not numeric_id.isdigit() or not session.get("value")
                    or not session.get("httpOnly") or not session.get("secure")):
                logger.warning("hosted_login_capture blocked: missing authenticated cookie metadata")
                raise ConnectError("session_unverified", 422)
            # Read the authenticated viewer bootstrap, not a public profile or
            # the retired current_user REST route. Reload the fixed home URL so
            # stale page data cannot confirm a different/currently logged-out user.
            response = await self.page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=8000)
            if response is not None and response.status == 429:
                raise ConnectError("instagram_rate_limited", 429)
            if self.page.url.rstrip("/") != "https://www.instagram.com":
                raise ConnectError("sign_in_required", 422)
            if response is None or response.status != 200:
                raise ConnectError("identity_check_unavailable", 503)
            viewer_handle = await self.page.wait_for_function("""() => {
                const found = [];
                function walk(value) {
                    if (!value || typeof value !== 'object') return;
                    if (Array.isArray(value) && value[0] === 'PolarisViewer'
                            && value[2] && typeof value[2] === 'object') {
                        const viewer = value[2];
                        found.push({id: viewer.id, user_id: viewer.data?.id,
                                    username: viewer.data?.username});
                        return;
                    }
                    for (const child of Object.values(value)) {
                        if (child && typeof child === 'object') walk(child);
                    }
                }
                for (const el of document.querySelectorAll('script[type="application/json"]')) {
                    try { walk(JSON.parse(el.textContent)); } catch {}
                }
                return found.length ? found : false;
            }""", timeout=4000)
            viewers = await viewer_handle.json_value()
            await viewer_handle.dispose()
            if not isinstance(viewers, list) or not viewers:
                raise ConnectError("identity_check_unavailable", 503)
            handles = set()
            for viewer in viewers:
                if not isinstance(viewer, dict):
                    raise ConnectError("identity_unavailable", 422)
                ids = {str(viewer.get(k)) for k in ("id", "user_id")}
                handle = viewer.get("username")
                if ids != {numeric_id} or not isinstance(handle, str):
                    raise ConnectError("identity_unavailable", 422)
                handles.add(handle.lower())
            if handles != {expected_username.lower()}:
                raise ConnectError("identity_mismatch", 422)
            # Navigation may rotate cookies. Persist only the fresh, matching
            # authenticated session rather than the pre-navigation snapshot.
            fresh = await self.context.cookies(["https://www.instagram.com/"])
            for cookie in cookies:
                cookie.clear()
            cookies.clear()
            cookies.extend(fresh)
            latest = {cookie["name"]: cookie for cookie in cookies}
            session = latest.get("sessionid", {})
            if (latest.get("ds_user_id", {}).get("value") != numeric_id
                    or not session.get("value") or not session.get("httpOnly") or not session.get("secure")):
                raise ConnectError("session_unverified", 422)
            captured = []
            for cookie in cookies:
                if cookie["name"] not in COOKIE_NAMES:
                    continue
                expiry = cookie.get("expires", -1)
                captured.append(CookieCapture(
                    name=cookie["name"], value=cookie["value"], domain=cookie["domain"],
                    path=cookie["path"], secure=cookie["secure"],
                    httpOnly=cookie["httpOnly"], sameSite=cookie["sameSite"],
                    expirationDate=expiry if expiry > 0 else None,
                ))
            user_agent = await self.page.evaluate("() => navigator.userAgent")
            return SessionCapture(
                username=expected_username.lower(), ig_user_id=numeric_id, cookies=captured,
                user_agent=user_agent,
                login_geo=LoginGeo(country=self.geo.country, region=self.geo.region, city=self.geo.city),
            )
        finally:
            for cookie in cookies:
                cookie.clear()
            cookies.clear()

    async def close(self):
        await self.browser.close()
