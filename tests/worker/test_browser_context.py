from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from typing import Any
from uuid import UUID

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

import engagemate.browser.context as browser_context_module
from engagemate.browser.context import (
    BrowserContextError,
    BrowserContextFactory,
    BrowserRunContext,
    DecryptedCookieJar,
    fingerprint_init_script,
)
from engagemate.browser.fingerprint import (
    CapturedBrowserSession,
    FingerprintAccount,
    FingerprintFactory,
    FingerprintGeoRegistry,
)
from engagemate.proxy.base import Geo, ProxyEndpoint


ACCOUNT_A = UUID("11111111-1111-4111-8111-111111111111")
ACCOUNT_B = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 7, 31, tzinfo=timezone.utc)
EXPIRY = NOW.timestamp() + 86_400
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
)
FINGERPRINTS = FingerprintFactory(
    "fingerprint-seed-that-is-at-least-32-bytes",
    FingerprintGeoRegistry.from_json(
        json.dumps(
            [
                {
                    "country": "IN",
                    "region": "MH",
                    "city": "Mumbai",
                    "timezone": "Asia/Kolkata",
                    "locale": "en-IN",
                }
            ]
        )
    ),
)


class FakeBrowserContext:
    def __init__(
        self,
        profile_dir: Path,
        *,
        add_cookie_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.profile_dir = profile_dir
        self.cookies: list[dict[str, Any]] = []
        self.local_storage: dict[str, str] = {}
        self.init_scripts: list[str] = []
        self.events: list[str] = []
        self.closed = False
        self.close_calls = 0
        self._add_cookie_error = add_cookie_error
        self._close_error = close_error

    async def add_init_script(self, *, script: str) -> None:
        self.events.append("init")
        self.init_scripts.append(script)

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.events.append("cookies")
        if self._add_cookie_error is not None:
            raise self._add_cookie_error
        self.cookies = deepcopy(cookies)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class FakeChromium:
    def __init__(
        self,
        *,
        launch_error: Exception | None = None,
        add_cookie_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.launches: list[dict[str, Any]] = []
        self.contexts: list[FakeBrowserContext] = []
        self._launch_error = launch_error
        self._add_cookie_error = add_cookie_error
        self._close_error = close_error

    async def launch_persistent_context(
        self, user_data_dir: str, **kwargs: Any
    ) -> FakeBrowserContext:
        self.launches.append({"user_data_dir": user_data_dir, **deepcopy(kwargs)})
        if self._launch_error is not None:
            raise self._launch_error
        context = FakeBrowserContext(
            Path(user_data_dir),
            add_cookie_error=self._add_cookie_error,
            close_error=self._close_error,
        )
        self.contexts.append(context)
        return context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium | None = None) -> None:
        self.chromium = chromium or FakeChromium()


class FixedChromium:
    def __init__(self, context: FakeBrowserContext) -> None:
        self.context = context
        self.launches: list[dict[str, Any]] = []

    async def launch_persistent_context(
        self, user_data_dir: str, **kwargs: Any
    ) -> FakeBrowserContext:
        self.context.profile_dir = Path(user_data_dir)
        self.launches.append({"user_data_dir": user_data_dir, **deepcopy(kwargs)})
        return self.context


class CancelOnceCloseContext(FakeBrowserContext):
    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise asyncio.CancelledError("close cancelled once")
        self.closed = True


class BlockingCloseContext(FakeBrowserContext):
    def __init__(self, profile_dir: Path) -> None:
        super().__init__(profile_dir)
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.release_close.wait()
        self.closed = True


class SecretBearingInvalidValue:
    def __init__(self, secret: str, *, explode_hash: bool = False) -> None:
        self.secret = secret
        self.explode_hash = explode_hash

    def __repr__(self) -> str:
        return f"SecretBearingInvalidValue({self.secret})"

    def __hash__(self) -> int:
        if self.explode_hash:
            raise RuntimeError(f"invalid value contained {self.secret}")
        return id(self)


class CancellingHashValue:
    def __hash__(self) -> int:
        raise asyncio.CancelledError("cookie validation cancelled")


def _endpoint(
    account_id: UUID,
    *,
    username: str = "proxy-user",
    password: str = "proxy-password",
) -> ProxyEndpoint:
    suffix = str(account_id)[0]
    return ProxyEndpoint(
        provider="iproyal",
        provider_reference=f"lease-{suffix}",
        proxy_type="isp",
        geo=Geo("IN", "MH", "Mumbai"),
        host=f"192.0.2.{10 + int(suffix)}",
        port=12_345,
        username=username,
        password=password,
    )


def _raw_payload(
    account_id: UUID,
    *,
    same_site: str = "no_restriction",
    expires: float | None = EXPIRY,
) -> dict[str, Any]:
    cookie: dict[str, Any] = {
        "name": "sessionid",
        "value": f"cookie-{account_id}",
        "domain": ".instagram.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": same_site,
    }
    if expires is not None:
        cookie["expirationDate"] = expires
    return {"cookies": [cookie]}


def _run_context(
    account_id: UUID,
    *,
    payload: dict[str, Any] | None = None,
    endpoint: ProxyEndpoint | None = None,
) -> BrowserRunContext:
    geo = Geo("IN", "MH", "Mumbai")
    fingerprint = FINGERPRINTS.for_account(
        FingerprintAccount(account_id, geo),
        CapturedBrowserSession(USER_AGENT),
    )
    return BrowserRunContext(
        ig_account_id=account_id,
        endpoint=endpoint or _endpoint(account_id),
        fingerprint=fingerprint,
        cookies=DecryptedCookieJar.from_payload(
            payload if payload is not None else _raw_payload(account_id)
        ),
    )


def test_payload_ownership_moves_into_a_redacted_one_shot_jar() -> None:
    """Copying decrypted cookies would leave a caller-owned plaintext duplicate."""

    payload = _raw_payload(ACCOUNT_A)
    value = payload["cookies"][0]["value"]
    jar = DecryptedCookieJar.from_payload(payload)

    assert payload == {}
    assert value not in repr(jar)
    assert "redacted" in repr(jar)

    consumed = jar.consume()
    assert consumed[0]["value"] == value
    assert jar.consume() == []


def test_launch_applies_exact_identity_proxy_and_init_script_then_cleans_profile(
    tmp_path: Path,
) -> None:
    """Omitting a launch option, spoofing script, or cleanup would leak identity."""

    async def scenario() -> None:
        playwright = FakePlaywright()
        factory = BrowserContextFactory(
            playwright=playwright,
            profile_root=tmp_path,
            now=lambda: NOW,
        )
        run = _run_context(ACCOUNT_A)

        async with factory.open(run) as browser:
            launch = playwright.chromium.launches[0]
            context = playwright.chromium.contexts[0]
            profile_dir = browser.profile_dir
            assert Path(launch["user_data_dir"]) == profile_dir
            assert profile_dir.exists()
            assert launch["channel"] == "chrome"
            assert launch["service_workers"] == "block"
            assert launch["proxy"] == {
                "server": run.endpoint.server,
                "username": "proxy-user",
                "password": "proxy-password",
            }
            assert launch["user_agent"] == USER_AGENT
            assert launch["viewport"] == run.fingerprint.viewport
            assert launch["locale"] == "en-IN"
            assert launch["timezone_id"] == "Asia/Kolkata"
            assert launch["color_scheme"] == run.fingerprint.color_scheme
            assert context.cookies == [
                {
                    "name": "sessionid",
                    "value": f"cookie-{ACCOUNT_A}",
                    "domain": ".instagram.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "None",
                    "expires": EXPIRY,
                }
            ]
            assert len(context.init_scripts) == 1
            assert context.events == ["init", "cookies"]
            script = context.init_scripts[0]
            assert "CanvasRenderingContext2D" in script
            assert "WebGLRenderingContext" in script
            assert run.fingerprint.canvas_seed not in script
            assert run.fingerprint.webgl_seed not in script
            assert run.cookies.consume() == []

        assert context.closed
        assert not profile_dir.exists()

    asyncio.run(scenario())


def test_accounts_never_share_context_cookie_storage_or_profile(
    tmp_path: Path,
) -> None:
    """Reusing a persistent profile would cross tenant browser state."""

    async def scenario() -> None:
        playwright = FakePlaywright()
        factory = BrowserContextFactory(
            playwright=playwright, profile_root=tmp_path, now=lambda: NOW
        )
        first_run = _run_context(ACCOUNT_A)
        second_run = _run_context(ACCOUNT_B)

        async with factory.open(first_run) as first:
            first.context.local_storage["account"] = str(ACCOUNT_A)
            async with factory.open(second_run) as second:
                second.context.local_storage["account"] = str(ACCOUNT_B)
                assert first.context is not second.context
                assert first.profile_dir != second.profile_dir
                assert first.context.cookies[0]["value"] == f"cookie-{ACCOUNT_A}"
                assert second.context.cookies[0]["value"] == f"cookie-{ACCOUNT_B}"
                assert first.context.local_storage == {"account": str(ACCOUNT_A)}
                assert second.context.local_storage == {"account": str(ACCOUNT_B)}
                assert (
                    playwright.chromium.launches[0]["proxy"]
                    != playwright.chromium.launches[1]["proxy"]
                )

        assert not first.profile_dir.exists()
        assert not second.profile_dir.exists()

    asyncio.run(scenario())


def test_same_account_contexts_are_serialized_and_lock_is_released(
    tmp_path: Path,
) -> None:
    """Allowing two active runs for one account would race its browser state."""

    async def scenario() -> None:
        factory = BrowserContextFactory(
            playwright=FakePlaywright(), profile_root=tmp_path, now=lambda: NOW
        )
        first_manager = factory.open(_run_context(ACCOUNT_A))
        first = await first_manager.__aenter__()
        try:
            with pytest.raises(BrowserContextError, match="already active"):
                async with factory.open(_run_context(ACCOUNT_A)):
                    pytest.fail("second same-account context opened")
        finally:
            await first_manager.__aexit__(None, None, None)

        async with factory.open(_run_context(ACCOUNT_A)) as reopened:
            assert reopened.profile_dir != first.profile_dir

    asyncio.run(scenario())


def test_stale_lock_file_does_not_block_a_new_account_context(
    tmp_path: Path,
) -> None:
    """A process crash must not leave a permanent mkdir-style account lock."""

    locks_root = tmp_path / ".account-locks"
    locks_root.mkdir(mode=0o700)
    locks_root.chmod(0o700)
    (locks_root / str(ACCOUNT_A)).touch(mode=0o600)
    factory = BrowserContextFactory(
        playwright=FakePlaywright(), profile_root=tmp_path, now=lambda: NOW
    )

    async def scenario() -> None:
        async with factory.open(_run_context(ACCOUNT_A)) as browser:
            assert browser.profile_dir.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["symlink", "permissive", "file", "wrong-owner"])
def test_insecure_profile_root_fails_closed_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Following or trusting an unsafe root would expose plaintext Chrome state."""

    root = tmp_path / "profiles"
    if kind == "symlink":
        target = tmp_path / "attacker-root"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    elif kind == "permissive":
        root.mkdir()
        root.chmod(0o777)
    elif kind == "file":
        root.write_text("not a directory")
    else:
        root.mkdir(mode=0o700)
        monkeypatch.setattr(
            browser_context_module.os,
            "geteuid",
            lambda: os.stat(root).st_uid + 1,
        )
    playwright = FakePlaywright()
    factory = BrowserContextFactory(
        playwright=playwright, profile_root=root, now=lambda: NOW
    )

    async def scenario() -> None:
        with pytest.raises(BrowserContextError, match="filesystem"):
            async with factory.open(_run_context(ACCOUNT_A)):
                pytest.fail("unsafe root launched")
        assert playwright.chromium.launches == []

    asyncio.run(scenario())


def test_symlinked_lock_parent_fails_closed_before_lock_creation(
    tmp_path: Path,
) -> None:
    """Swapping the account-lock directory must not redirect lock creation."""

    root = tmp_path / "profiles"
    root.mkdir(mode=0o700)
    attacker = tmp_path / "attacker-locks"
    attacker.mkdir(mode=0o700)
    (root / ".account-locks").symlink_to(attacker, target_is_directory=True)
    playwright = FakePlaywright()

    async def scenario() -> None:
        with pytest.raises(BrowserContextError, match="filesystem"):
            async with BrowserContextFactory(
                playwright=playwright, profile_root=root, now=lambda: NOW
            ).open(_run_context(ACCOUNT_A)):
                pytest.fail("symlinked lock parent launched")
        assert list(attacker.iterdir()) == []
        assert playwright.chromium.launches == []

    asyncio.run(scenario())


def test_body_cancellation_completes_teardown_and_releases_account_lock(
    tmp_path: Path,
) -> None:
    """A cancelled run must not retain its profile or account lock."""

    factory = BrowserContextFactory(
        playwright=FakePlaywright(), profile_root=tmp_path, now=lambda: NOW
    )

    async def scenario() -> None:
        profile_dir: Path | None = None
        with pytest.raises(asyncio.CancelledError, match="body cancelled"):
            async with factory.open(_run_context(ACCOUNT_A)) as browser:
                profile_dir = browser.profile_dir
                raise asyncio.CancelledError("body cancelled")
        assert profile_dir is not None and not profile_dir.exists()
        async with factory.open(_run_context(ACCOUNT_A)) as reopened:
            assert reopened.profile_dir.exists()

    asyncio.run(scenario())


def test_actual_task_cancellation_inside_body_completes_teardown(
    tmp_path: Path,
) -> None:
    """Task cancellation in caller code must run the full context-manager cleanup."""

    context = FakeBrowserContext(tmp_path / "placeholder")
    factory = BrowserContextFactory(
        playwright=FakePlaywright(FixedChromium(context)),  # type: ignore[arg-type]
        profile_root=tmp_path,
        now=lambda: NOW,
    )

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        profile_dir: Path | None = None

        async def run_body() -> None:
            nonlocal profile_dir
            async with factory.open(_run_context(ACCOUNT_A)) as browser:
                profile_dir = browser.profile_dir
                entered.set()
                await release.wait()

        task = asyncio.create_task(run_body())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel("cancel active body")
        with pytest.raises(asyncio.CancelledError, match="cancel active body"):
            await task
        assert context.closed
        assert profile_dir is not None and not profile_dir.exists()
        async with BrowserContextFactory(
            playwright=FakePlaywright(), profile_root=tmp_path, now=lambda: NOW
        ).open(_run_context(ACCOUNT_A)):
            pass

    asyncio.run(scenario())


def test_close_coroutine_cancellation_is_retried_then_propagated_after_cleanup(
    tmp_path: Path,
) -> None:
    """A close coroutine cancellation must not interrupt teardown or hide cancellation."""

    context = CancelOnceCloseContext(tmp_path / "placeholder")
    factory = BrowserContextFactory(
        playwright=FakePlaywright(FixedChromium(context)),  # type: ignore[arg-type]
        profile_root=tmp_path,
        now=lambda: NOW,
    )

    async def scenario() -> None:
        profile_dir: Path | None = None
        with pytest.raises(asyncio.CancelledError, match="close cancelled"):
            async with factory.open(_run_context(ACCOUNT_A)) as browser:
                profile_dir = browser.profile_dir
        assert context.close_calls == 2
        assert context.closed
        assert profile_dir is not None and not profile_dir.exists()
        async with BrowserContextFactory(
            playwright=FakePlaywright(), profile_root=tmp_path, now=lambda: NOW
        ).open(_run_context(ACCOUNT_A)):
            pass

    asyncio.run(scenario())


def test_outer_task_cancellation_during_close_waits_for_close_and_unlocks(
    tmp_path: Path,
) -> None:
    """Cancelling `__aexit__` must not strand Chrome while teardown is running."""

    context = BlockingCloseContext(tmp_path / "placeholder")
    factory = BrowserContextFactory(
        playwright=FakePlaywright(FixedChromium(context)),  # type: ignore[arg-type]
        profile_root=tmp_path,
        now=lambda: NOW,
    )

    async def scenario() -> None:
        manager = factory.open(_run_context(ACCOUNT_A))
        browser = await manager.__aenter__()
        exit_task = asyncio.create_task(manager.__aexit__(None, None, None))
        await asyncio.wait_for(context.close_started.wait(), timeout=1)
        exit_task.cancel("outer close cancellation")
        await asyncio.sleep(0)
        assert not exit_task.done()
        context.release_close.set()
        with pytest.raises(asyncio.CancelledError, match="outer close cancellation"):
            await exit_task
        assert context.closed
        assert not browser.profile_dir.exists()
        async with BrowserContextFactory(
            playwright=FakePlaywright(), profile_root=tmp_path, now=lambda: NOW
        ).open(_run_context(ACCOUNT_A)):
            pass

    asyncio.run(scenario())


def test_cleanup_failure_does_not_override_cancellation_or_skip_unlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deletion error during cancellation must still close the lock FD."""

    real_rmtree = browser_context_module.shutil.rmtree

    def failed_rmtree(_path: Path) -> None:
        raise OSError("forced cleanup failure")

    monkeypatch.setattr(browser_context_module.shutil, "rmtree", failed_rmtree)
    factory = BrowserContextFactory(
        playwright=FakePlaywright(), profile_root=tmp_path, now=lambda: NOW
    )

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError, match="body cancelled"):
            async with factory.open(_run_context(ACCOUNT_A)):
                raise asyncio.CancelledError("body cancelled")
        monkeypatch.setattr(browser_context_module.shutil, "rmtree", real_rmtree)
        for profile in tmp_path.glob("profile-*"):
            real_rmtree(profile)
        async with factory.open(_run_context(ACCOUNT_A)):
            pass

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["launch", "cookie"])
def test_failures_close_context_remove_profile_and_redact_secrets(
    tmp_path: Path,
    phase: str,
) -> None:
    """Raw dependency errors could leak credentials or retain cookie profiles."""

    username = "sensitive-proxy-user"
    password = "sensitive-proxy-password"
    cookie = "sensitive-cookie-value"
    leaked = RuntimeError(f"{username}:{password}:{cookie}")
    chromium = FakeChromium(
        launch_error=leaked if phase == "launch" else None,
        add_cookie_error=leaked if phase == "cookie" else None,
    )
    factory = BrowserContextFactory(
        playwright=FakePlaywright(chromium),
        profile_root=tmp_path,
        now=lambda: NOW,
    )
    run = _run_context(
        ACCOUNT_A,
        payload={
            "cookies": [
                {
                    **_raw_payload(ACCOUNT_A)["cookies"][0],
                    "value": cookie,
                }
            ]
        },
        endpoint=_endpoint(ACCOUNT_A, username=username, password=password),
    )

    async def scenario() -> None:
        with pytest.raises(BrowserContextError) as raised:
            async with factory.open(run):
                pytest.fail("failed context opened")
        rendered = f"{raised.value!r} {raised.value}"
        formatted = "".join(
            traceback.format_exception(
                type(raised.value), raised.value, raised.value.__traceback__
            )
        )
        assert raised.value.__context__ is None
        assert raised.value.__cause__ is None
        assert username not in rendered
        assert password not in rendered
        assert cookie not in rendered
        assert username not in formatted
        assert password not in formatted
        assert cookie not in formatted
        assert not list(tmp_path.glob("profile-*"))
        assert run.cookies.consume() == []
        if chromium.contexts:
            assert chromium.contexts[0].closed

    asyncio.run(scenario())


def test_generated_fingerprint_script_changes_all_canvas_paths_stably(
    tmp_path: Path,
) -> None:
    """A one-path or cumulative patch would leave observable canvas calls inconsistent."""

    first = _run_context(ACCOUNT_A).fingerprint
    second = _run_context(ACCOUNT_B).fingerprint
    windows = FINGERPRINTS.for_account(
        FingerprintAccount(ACCOUNT_A, Geo("IN", "MH", "Mumbai")),
        CapturedBrowserSession(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
        ),
    )
    linux = FINGERPRINTS.for_account(
        FingerprintAccount(ACCOUNT_A, Geo("IN", "MH", "Mumbai")),
        CapturedBrowserSession(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
        ),
    )
    fingerprints = [first, first, second, windows, linux]
    scripts = [fingerprint_init_script(item) for item in fingerprints]
    first_script = scripts[0]
    assert first.canvas_seed not in first_script
    assert first.webgl_seed not in first_script
    assert first.webgl_renderer in first_script

    harness = r"""
const vm = require("node:vm");
const scripts = JSON.parse(process.argv[1]);
function execute(initScript) {
  class CanvasRenderingContext2D {
    constructor(canvas) { this.canvas = canvas; }
    getImageData() {
      return {data: new Uint8ClampedArray(this.canvas.pixels), width: 2, height: 2};
    }
    putImageData(image) { this.canvas.pixels = Array.from(image.data); }
    drawImage(source) { this.canvas.pixels = Array.from(source.pixels); }
  }
  class HTMLCanvasElement {
    constructor() {
      this.width = 2; this.height = 2;
      this.pixels = [10,20,30,255,40,50,60,255,70,80,90,255,100,110,120,255];
      this.context = new CanvasRenderingContext2D(this);
    }
    getContext(kind) { return kind === "2d" ? this.context : null; }
    toDataURL() { return "data:" + this.pixels.join(","); }
    toBlob(callback) { callback(Buffer.from(this.pixels)); }
  }
  class WebGLRenderingContext {
    getParameter(parameter) { return "original-" + parameter; }
  }
  class WebGL2RenderingContext extends WebGLRenderingContext {}
  const sandbox = {
    Buffer, Uint8ClampedArray, CanvasRenderingContext2D, HTMLCanvasElement,
    WebGLRenderingContext, WebGL2RenderingContext,
    document: {createElement: () => new HTMLCanvasElement()},
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(initScript, sandbox);
  const canvas = new sandbox.HTMLCanvasElement();
  const firstImage = Array.from(canvas.getContext("2d").getImageData().data);
  const secondImage = Array.from(canvas.getContext("2d").getImageData().data);
  const firstUrl = canvas.toDataURL();
  const secondUrl = canvas.toDataURL();
  let blob;
  canvas.toBlob((value) => { blob = value.toString("hex"); });
  const gl = new sandbox.WebGLRenderingContext();
  return {
    firstImage, secondImage, firstUrl, secondUrl, blob,
    vendor: gl.getParameter(37445), renderer: gl.getParameter(37446),
  };
}
process.stdout.write(JSON.stringify(scripts.map(execute)));
"""
    completed = subprocess.run(
        ["node", "-e", harness, json.dumps(scripts)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    (
        first_result,
        repeated_result,
        second_result,
        windows_result,
        linux_result,
    ) = json.loads(completed.stdout)
    assert first_result == repeated_result
    assert first_result["firstImage"] == first_result["secondImage"]
    assert first_result["firstUrl"] == first_result["secondUrl"]
    assert first_result["firstImage"] != second_result["firstImage"]
    assert first_result["firstUrl"] != second_result["firstUrl"]
    assert first_result["blob"] != second_result["blob"]
    assert "Apple" in first_result["vendor"]
    assert first_result["renderer"].startswith("ANGLE (Apple")
    assert not any(character in first_result["renderer"] for character in "#{}")
    for fingerprint, result in zip(
        (first, windows, linux),
        (first_result, windows_result, linux_result),
        strict=True,
    ):
        assert result["vendor"] == fingerprint.webgl_vendor
        assert result["renderer"] == fingerprint.webgl_renderer


@pytest.mark.parametrize(
    "field",
    [
        "path",
        "domain",
        "name",
        "value",
        "expirationDate",
        "sameSite",
        "secure",
        "httpOnly",
    ],
)
def test_malformed_cookie_errors_drop_secret_values_from_exception_graph(
    tmp_path: Path,
    field: str,
) -> None:
    """A rejected cookie must not stay reachable through exception traceback locals."""

    secret = f"adversarial-{field}-secret"
    invalid_values: dict[str, Any] = {
        "path": f"/valid\n{secret}",
        "domain": f".instagram.com.{secret}.invalid",
        "name": f"bad\n{secret}",
        "value": f"{secret}\n",
        "expirationDate": SecretBearingInvalidValue(secret),
        "sameSite": SecretBearingInvalidValue(secret, explode_hash=True),
        "secure": SecretBearingInvalidValue(secret),
        "httpOnly": SecretBearingInvalidValue(secret),
    }
    payload = _raw_payload(ACCOUNT_A)
    payload["cookies"][0][field] = invalid_values[field]
    run = _run_context(ACCOUNT_A, payload=payload)
    factory = BrowserContextFactory(
        playwright=FakePlaywright(), profile_root=tmp_path, now=lambda: NOW
    )

    async def capture_error() -> BrowserContextError:
        try:
            async with factory.open(run):
                pytest.fail("malformed cookie launched")
        except BrowserContextError as error:
            return error
        raise AssertionError("missing browser error")

    raised = asyncio.run(capture_error())
    assert raised.__context__ is None
    assert raised.__cause__ is None
    formatted = "".join(
        traceback.format_exception(type(raised), raised, raised.__traceback__)
    )
    assert secret not in formatted

    chain: BaseException | None = raised
    while chain is not None:
        assert secret not in repr(chain)
        chain = chain.__cause__ or chain.__context__

    current = raised.__traceback__
    while current is not None:
        for local_value in current.tb_frame.f_locals.values():
            assert secret not in repr(local_value)
        current = current.tb_next
    assert run.cookies.consume() == []


def test_cookie_validation_does_not_translate_cancellation(
    tmp_path: Path,
) -> None:
    """Cookie validation must preserve cancellation while wiping and unlocking."""

    payload = _raw_payload(ACCOUNT_A)
    payload["cookies"][0]["sameSite"] = CancellingHashValue()
    run = _run_context(ACCOUNT_A, payload=payload)
    factory = BrowserContextFactory(
        playwright=FakePlaywright(), profile_root=tmp_path, now=lambda: NOW
    )

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError, match="validation cancelled"):
            async with factory.open(run):
                pytest.fail("cancelled validation launched")
        assert run.cookies.consume() == []
        async with factory.open(_run_context(ACCOUNT_A)):
            pass

    asyncio.run(scenario())


def test_close_failure_is_redacted_and_still_removes_profile(tmp_path: Path) -> None:
    """A close exception must not preserve the profile or expose credentials."""

    secret = "sensitive-proxy-password"
    chromium = FakeChromium(close_error=RuntimeError(secret))
    factory = BrowserContextFactory(
        playwright=FakePlaywright(chromium), profile_root=tmp_path, now=lambda: NOW
    )

    async def scenario() -> None:
        profile_dir: Path | None = None
        with pytest.raises(BrowserContextError) as raised:
            async with factory.open(
                _run_context(
                    ACCOUNT_A,
                    endpoint=_endpoint(ACCOUNT_A, password=secret),
                )
            ) as browser:
                profile_dir = browser.profile_dir
        assert secret not in f"{raised.value!r} {raised.value}"
        assert profile_dir is not None and not profile_dir.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"domain": ".evilinstagram.com"}, "cookie payload"),
        ({"domain": ".instagram.com.evil.test"}, "cookie payload"),
        ({"path": "relative"}, "cookie payload"),
        ({"path": "/valid\ninjected"}, "cookie payload"),
        ({"expirationDate": float("nan")}, "cookie payload"),
        ({"expirationDate": float("inf")}, "cookie payload"),
        ({"expirationDate": NOW.timestamp() - 1}, "cookie payload"),
        ({"expirationDate": 253_402_300_800}, "cookie payload"),
        ({"secure": False}, "sessionid"),
        ({"httpOnly": False}, "sessionid"),
        ({"sameSite": "mystery"}, "cookie payload"),
        ({"name": "bad\nname"}, "cookie payload"),
        ({"value": "x" * 16_385}, "cookie payload"),
    ],
)
def test_decrypted_cookie_payload_is_revalidated_before_launch(
    tmp_path: Path,
    mutation: dict[str, Any],
    match: str,
) -> None:
    """Trusting stored encrypted JSON would bypass the capture API boundary."""

    payload = _raw_payload(ACCOUNT_A)
    payload["cookies"][0].update(mutation)
    playwright = FakePlaywright()
    factory = BrowserContextFactory(
        playwright=playwright, profile_root=tmp_path, now=lambda: NOW
    )
    run = _run_context(ACCOUNT_A, payload=payload)

    async def scenario() -> None:
        with pytest.raises(BrowserContextError, match=match):
            async with factory.open(run):
                pytest.fail("invalid cookies launched")
        assert playwright.chromium.launches == []
        assert not list(tmp_path.glob("profile-*"))
        assert run.cookies.consume() == []

    asyncio.run(scenario())


def test_exactly_one_secure_http_only_sessionid_is_required(tmp_path: Path) -> None:
    """Missing or duplicate session cookies could launch an ambiguous identity."""

    async def rejected(payload: dict[str, Any]) -> None:
        playwright = FakePlaywright()
        run = _run_context(ACCOUNT_A, payload=payload)
        with pytest.raises(BrowserContextError, match="sessionid"):
            async with BrowserContextFactory(
                playwright=playwright, profile_root=tmp_path, now=lambda: NOW
            ).open(run):
                pytest.fail("invalid sessionid launched")
        assert playwright.chromium.launches == []

    missing = _raw_payload(ACCOUNT_A)
    missing["cookies"][0]["name"] = "csrftoken"
    duplicate = _raw_payload(ACCOUNT_A)
    duplicate["cookies"].append(deepcopy(duplicate["cookies"][0]))
    insecure = _raw_payload(ACCOUNT_A)
    insecure["cookies"][0]["secure"] = False

    asyncio.run(rejected(missing))
    asyncio.run(rejected(duplicate))
    asyncio.run(rejected(insecure))


def test_profile_cleanup_retries_a_transient_filesystem_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silently ignoring a failed profile deletion would leave plaintext cookies."""

    real_rmtree = browser_context_module.shutil.rmtree
    calls = 0

    def flaky_rmtree(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("transient cleanup failure")
        real_rmtree(path)

    monkeypatch.setattr(browser_context_module.shutil, "rmtree", flaky_rmtree)
    factory = BrowserContextFactory(
        playwright=FakePlaywright(), profile_root=tmp_path, now=lambda: NOW
    )

    async def scenario() -> None:
        async with factory.open(_run_context(ACCOUNT_A)) as browser:
            profile_dir = browser.profile_dir
        assert calls == 2
        assert not profile_dir.exists()

    asyncio.run(scenario())


def test_factory_and_context_representations_redact_runtime_secrets(
    tmp_path: Path,
) -> None:
    """Default dataclass reprs could expose proxy credentials or cookie values."""

    password = "sensitive-proxy-password"
    cookie = "sensitive-cookie-value"
    run = _run_context(
        ACCOUNT_A,
        payload={
            "cookies": [
                {
                    **_raw_payload(ACCOUNT_A)["cookies"][0],
                    "value": cookie,
                }
            ]
        },
        endpoint=_endpoint(ACCOUNT_A, password=password),
    )
    factory = BrowserContextFactory(
        playwright=FakePlaywright(), profile_root=tmp_path
    )

    assert password not in repr(run)
    assert cookie not in repr(run)
    assert password not in repr(factory)
    assert "redacted" in repr(run)
