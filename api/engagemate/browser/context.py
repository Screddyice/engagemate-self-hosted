"""Disposable, account-isolated Chrome context construction."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
from typing import Any, AsyncIterator, Callable, Protocol
from uuid import UUID

from engagemate.browser.fingerprint import Fingerprint
from engagemate.proxy.base import ProxyEndpoint


_INSTAGRAM_DOMAIN = re.compile(
    r"\.?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*instagram\.com"
)
_COOKIE_NAME = re.compile(r"^[^\x00-\x20\x7f()<>@,;:\\\"/\[\]?={}]+$")
_MAX_COOKIE_EXPIRY = 253_402_300_799
_SAME_SITE = {
    "Strict": "Strict",
    "Lax": "Lax",
    "None": "None",
    "no_restriction": "None",
    "unspecified": None,
}


class BrowserContextError(RuntimeError):
    """A stable error that never includes browser, cookie, or proxy secrets."""


class BrowserContextLike(Protocol):
    async def add_init_script(self, *, script: str) -> None: ...

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True, repr=False)
class DecryptedCookieJar:
    """Own a decrypted cookie collection until its one browser load."""

    _cookies: list[Any] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DecryptedCookieJar":
        if not isinstance(payload, dict):
            raise BrowserContextError("invalid browser cookie payload")
        raw = payload.pop("cookies", None)
        payload.clear()
        cookies = raw if isinstance(raw, list) else [raw]
        return cls(cookies)

    def consume(self) -> list[Any]:
        cookies = self._cookies
        self._cookies = []
        return cookies

    def __repr__(self) -> str:
        return "DecryptedCookieJar(cookies=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class BrowserRunContext:
    """The account-bound values required to open one browser context."""

    ig_account_id: UUID
    endpoint: ProxyEndpoint
    fingerprint: Fingerprint
    cookies: DecryptedCookieJar

    def __repr__(self) -> str:
        return (
            "BrowserRunContext("
            f"ig_account_id={self.ig_account_id!r}, "
            f"endpoint={self.endpoint!r}, "
            f"fingerprint={self.fingerprint!r}, cookies=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BrowserSession:
    """One active disposable browser context."""

    context: BrowserContextLike
    profile_dir: Path
    fingerprint: Fingerprint

    def __repr__(self) -> str:
        return (
            "BrowserSession("
            f"profile_dir={str(self.profile_dir)!r}, "
            f"fingerprint={self.fingerprint!r}, context=<browser>)"
        )


_INIT_SCRIPT = """
(({offsets, masks, vendor, renderer}) => {
  const perturb = (image) => {
    const data = new Uint8ClampedArray(image.data);
    const pixels = Math.floor(data.length / 4);
    if (pixels > 0) {
      for (let index = 0; index < offsets.length; index += 1) {
        const pixel = offsets[index] %% pixels;
        const channel = (offsets[index] + index) %% 3;
        data[(pixel * 4) + channel] ^= masks[index];
      }
    }
    if (typeof ImageData === "function") {
      return new ImageData(data, image.width, image.height);
    }
    return {...image, data};
  };

  const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  const originalPutImageData = CanvasRenderingContext2D.prototype.putImageData;
  CanvasRenderingContext2D.prototype.getImageData = function(...args) {
    return perturb(originalGetImageData.apply(this, args));
  };

  const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
  const originalToBlob = HTMLCanvasElement.prototype.toBlob;
  const noisyCopy = (source) => {
    const copy = document.createElement("canvas");
    copy.width = source.width;
    copy.height = source.height;
    const context = copy.getContext("2d");
    context.drawImage(source, 0, 0);
    const image = originalGetImageData.call(
      context, 0, 0, copy.width, copy.height
    );
    originalPutImageData.call(context, perturb(image), 0, 0);
    return copy;
  };
  HTMLCanvasElement.prototype.toDataURL = function(...args) {
    return originalToDataURL.apply(noisyCopy(this), args);
  };
  HTMLCanvasElement.prototype.toBlob = function(callback, ...args) {
    return originalToBlob.call(noisyCopy(this), callback, ...args);
  };

  const patchWebGL = (prototype) => {
    if (!prototype) return;
    const originalGetParameter = prototype.getParameter;
    prototype.getParameter = function(parameter) {
      if (parameter === 37445) return vendor;
      if (parameter === 37446) return renderer;
      return originalGetParameter.call(this, parameter);
    };
  };
  patchWebGL(globalThis.WebGLRenderingContext?.prototype);
  patchWebGL(globalThis.WebGL2RenderingContext?.prototype);
})(%s);
"""


def fingerprint_init_script(fingerprint: Fingerprint) -> str:
    """Build a stable script without embedding either HMAC-derived seed."""

    canvas_bytes = bytes.fromhex(fingerprint.canvas_seed)
    offsets = [
        int.from_bytes(canvas_bytes[index : index + 2], "big")
        for index in range(0, 16, 2)
    ]
    masks = [1 + (canvas_bytes[index] % 7) for index in range(16, 24)]
    return _INIT_SCRIPT % json.dumps(
        {
            "offsets": offsets,
            "masks": masks,
            "vendor": fingerprint.webgl_vendor,
            "renderer": fingerprint.webgl_renderer,
        },
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class _AccountLock:
    root_fd: int
    locks_fd: int
    lock_fd: int


class BrowserContextFactory:
    """Open Chrome in an account-scoped profile that is always destroyed."""

    def __init__(
        self,
        *,
        playwright: Any,
        profile_root: str | Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._playwright = playwright
        self._profile_root = Path(
            profile_root
            if profile_root is not None
            else Path(tempfile.gettempdir()) / "engagemate-chrome"
        )
        self._now = now or (lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return (
            "BrowserContextFactory("
            f"profile_root={str(self._profile_root)!r}, "
            "playwright=<injected>, secrets=<redacted>)"
        )

    @asynccontextmanager
    async def open(
        self, run_context: BrowserRunContext
    ) -> AsyncIterator[BrowserSession]:
        """Open one disposable Chrome profile and remove it on every exit."""

        if type(run_context.ig_account_id) is not UUID:
            raise BrowserContextError("invalid browser account identity")
        account_lock = self._acquire_account_lock(run_context.ig_account_id)

        raw_cookies: list[Any] = []
        normalized_cookies: list[dict[str, Any]] = []
        profile_dir: Path | None = None
        browser_context: BrowserContextLike | None = None
        operation_error: BaseException | None = None
        close_cancellation: asyncio.CancelledError | None = None
        close_failed = False
        cleanup_failed = False
        lock_release_failed = False
        try:
            raw_cookies = run_context.cookies.consume()
            normalized_cookies = self._validate_cookies(raw_cookies)
            profile_dir = self._create_profile_directory(
                account_lock.root_fd, run_context.ig_account_id
            )
            launch_failed = False
            try:
                browser_context = (
                    await self._playwright.chromium.launch_persistent_context(
                        user_data_dir=str(profile_dir),
                        channel="chrome",
                        service_workers="block",
                        proxy={
                            "server": run_context.endpoint.server,
                            "username": run_context.endpoint.username,
                            "password": run_context.endpoint.password,
                        },
                        user_agent=run_context.fingerprint.user_agent,
                        viewport=run_context.fingerprint.viewport,
                        locale=run_context.fingerprint.locale,
                        timezone_id=run_context.fingerprint.timezone,
                        color_scheme=run_context.fingerprint.color_scheme,
                    )
                )
            except Exception:
                launch_failed = True
            if launch_failed:
                raise BrowserContextError("browser context launch failed")

            init_script = fingerprint_init_script(run_context.fingerprint)
            setup_failed = False
            try:
                await browser_context.add_init_script(script=init_script)
                await browser_context.add_cookies(normalized_cookies)
            except Exception:
                setup_failed = True
            finally:
                self._wipe_cookies(raw_cookies)
                self._wipe_cookies(normalized_cookies)
            if setup_failed:
                raise BrowserContextError("browser context setup failed")

            yield BrowserSession(
                context=browser_context,
                profile_dir=profile_dir,
                fingerprint=run_context.fingerprint,
            )
        except BaseException as error:
            operation_error = error
            raise
        finally:
            self._wipe_cookies(raw_cookies)
            self._wipe_cookies(normalized_cookies)
            try:
                if browser_context is not None:
                    close_cancellation, close_failed = await self._close_browser(
                        browser_context
                    )
            finally:
                try:
                    if profile_dir is not None:
                        try:
                            self._remove_profile(profile_dir)
                        except BrowserContextError:
                            cleanup_failed = True
                finally:
                    lock_release_failed = not self._release_account_lock(account_lock)
            if operation_error is None:
                if close_cancellation is not None:
                    raise close_cancellation
                if cleanup_failed:
                    raise BrowserContextError("browser profile cleanup failed")
                if lock_release_failed:
                    raise BrowserContextError("browser filesystem release failed")
                if close_failed:
                    raise BrowserContextError("browser context close failed")

    def _acquire_account_lock(self, account_id: UUID) -> _AccountLock:
        root_fd = -1
        locks_fd = -1
        lock_fd = -1
        blocked = False
        failed = False
        try:
            root_fd = self._open_secure_root()
            try:
                os.mkdir(".account-locks", mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            locks_fd = os.open(
                ".account-locks",
                self._directory_open_flags(),
                dir_fd=root_fd,
            )
            if not self._is_secure_directory(os.fstat(locks_fd)):
                raise OSError
            lock_flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                lock_flags |= os.O_NOFOLLOW
            lock_fd = os.open(
                str(account_id),
                lock_flags,
                0o600,
                dir_fd=locks_fd,
            )
            lock_status = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(lock_status.st_mode)
                or lock_status.st_uid != os.geteuid()
                or stat.S_IMODE(lock_status.st_mode) != 0o600
            ):
                raise OSError
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            blocked = True
        except OSError:
            failed = True
        if blocked or failed:
            self._close_fds(lock_fd, locks_fd, root_fd)
            if blocked:
                raise BrowserContextError(
                    "browser context already active for account"
                )
            raise BrowserContextError("browser filesystem is unsafe")
        return _AccountLock(root_fd, locks_fd, lock_fd)

    def _open_secure_root(self) -> int:
        failed = False
        root_fd = -1
        try:
            try:
                os.mkdir(self._profile_root, mode=0o700)
            except FileExistsError:
                pass
            root_status = os.lstat(self._profile_root)
            if stat.S_ISLNK(root_status.st_mode):
                raise OSError
            root_fd = os.open(self._profile_root, self._directory_open_flags())
            if not self._is_secure_directory(os.fstat(root_fd)):
                raise OSError
        except OSError:
            failed = True
        if failed:
            self._close_fds(root_fd)
            raise BrowserContextError("browser filesystem is unsafe")
        return root_fd

    @staticmethod
    def _directory_open_flags() -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return flags

    @staticmethod
    def _is_secure_directory(status: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(status.st_mode)
            and status.st_uid == os.geteuid()
            and stat.S_IMODE(status.st_mode) == 0o700
        )

    def _create_profile_directory(self, root_fd: int, account_id: UUID) -> Path:
        failed = False
        profile_name = ""
        for _ in range(16):
            candidate = f"profile-{account_id}-{secrets.token_hex(12)}"
            try:
                os.mkdir(candidate, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                continue
            except OSError:
                failed = True
            else:
                profile_name = candidate
            break
        if failed or not profile_name:
            raise BrowserContextError("browser filesystem is unsafe")
        return self._profile_root / profile_name

    @staticmethod
    async def _close_browser(
        browser_context: BrowserContextLike,
    ) -> tuple[asyncio.CancelledError | None, bool]:
        cancellation: asyncio.CancelledError | None = None
        close_failed = False
        for _ in range(2):
            close_task = asyncio.create_task(browser_context.close())
            while True:
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError as error:
                    if cancellation is None:
                        cancellation = error
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
                    if close_task.done():
                        if close_task.cancelled():
                            try:
                                close_task.result()
                            except asyncio.CancelledError as close_error:
                                cancellation = close_error
                        break
                    continue
                except Exception:
                    close_failed = True
                    break
                else:
                    return cancellation, close_failed
            if not close_task.cancelled():
                break
        return cancellation, close_failed

    @staticmethod
    def _release_account_lock(account_lock: _AccountLock) -> bool:
        failed = False
        try:
            fcntl.flock(account_lock.lock_fd, fcntl.LOCK_UN)
        except OSError:
            failed = True
        finally:
            for descriptor in (
                account_lock.lock_fd,
                account_lock.locks_fd,
                account_lock.root_fd,
            ):
                try:
                    os.close(descriptor)
                except OSError:
                    failed = True
        return not failed

    @staticmethod
    def _close_fds(*descriptors: int) -> None:
        for descriptor in descriptors:
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _validate_cookies(
        self, cookies: list[Any]
    ) -> list[dict[str, Any]]:
        now_timestamp = self._now().astimezone(timezone.utc).timestamp()
        normalized: list[dict[str, Any]] = []
        failure: str | None = None
        if 1 <= len(cookies) <= 100:
            normalized, failure = self._normalize_cookie_values(
                cookies, now_timestamp
            )
        else:
            failure = "payload"
        if failure is not None:
            self._wipe_cookies(cookies)
            self._wipe_cookies(normalized)
            message = (
                "exactly one secure HTTP-only sessionid is required"
                if failure == "session"
                else "invalid browser cookie payload"
            )
            raise BrowserContextError(message)
        return normalized

    @classmethod
    def _normalize_cookie_values(
        cls,
        cookies: list[Any],
        now_timestamp: float,
    ) -> tuple[list[dict[str, Any]], str | None]:
        normalized: list[dict[str, Any]] = []
        session_count = 0
        try:
            for raw_cookie in cookies:
                if not isinstance(raw_cookie, dict):
                    cls._wipe_cookies(normalized)
                    return [], "payload"
                if not set(raw_cookie).issubset(
                    {
                        "name",
                        "value",
                        "domain",
                        "path",
                        "secure",
                        "httpOnly",
                        "sameSite",
                        "expirationDate",
                    }
                ):
                    cls._wipe_cookies(normalized)
                    return [], "payload"
                name = raw_cookie.get("name")
                value = raw_cookie.get("value")
                domain = raw_cookie.get("domain")
                path = raw_cookie.get("path")
                secure = raw_cookie.get("secure")
                http_only = raw_cookie.get("httpOnly")
                same_site = raw_cookie.get("sameSite")
                expiry = raw_cookie.get("expirationDate")
                if (
                    not isinstance(name, str)
                    or not 1 <= len(name) <= 256
                    or _COOKIE_NAME.fullmatch(name) is None
                    or not isinstance(value, str)
                    or not 1 <= len(value) <= 16_384
                    or re.search(r"[\x00-\x1f\x7f]", value) is not None
                    or not isinstance(domain, str)
                    or not 1 <= len(domain) <= 253
                    or _INSTAGRAM_DOMAIN.fullmatch(domain.lower()) is None
                    or not isinstance(path, str)
                    or not 1 <= len(path) <= 2_048
                    or not path.startswith("/")
                    or re.search(r"[\x00-\x1f\x7f]", path) is not None
                    or type(secure) is not bool
                    or type(http_only) is not bool
                    or same_site not in _SAME_SITE
                ):
                    cls._wipe_cookies(normalized)
                    return [], "payload"
                if expiry is not None and (
                    isinstance(expiry, bool)
                    or not isinstance(expiry, (int, float))
                    or not math.isfinite(expiry)
                    or not now_timestamp < expiry <= _MAX_COOKIE_EXPIRY
                ):
                    cls._wipe_cookies(normalized)
                    return [], "payload"

                if name == "sessionid":
                    session_count += 1
                    if not secure or not http_only:
                        cls._wipe_cookies(normalized)
                        return [], "session"
                elif not secure:
                    cls._wipe_cookies(normalized)
                    return [], "payload"

                browser_cookie: dict[str, Any] = {
                    "name": name,
                    "value": value,
                    "domain": domain.lower(),
                    "path": path,
                    "secure": secure,
                    "httpOnly": http_only,
                }
                mapped_same_site = _SAME_SITE[same_site]
                if mapped_same_site is not None:
                    browser_cookie["sameSite"] = mapped_same_site
                if expiry is not None:
                    browser_cookie["expires"] = float(expiry)
                normalized.append(browser_cookie)
        except Exception:
            cls._wipe_cookies(normalized)
            return [], "payload"

        if session_count != 1:
            cls._wipe_cookies(normalized)
            return [], "session"
        return normalized, None

    @staticmethod
    def _wipe_cookies(cookies: list[Any]) -> None:
        for cookie in cookies:
            if isinstance(cookie, dict):
                try:
                    cookie.clear()
                except Exception:
                    pass
        cookies.clear()

    @staticmethod
    def _remove_profile(profile_dir: Path) -> None:
        for attempt in range(2):
            try:
                shutil.rmtree(profile_dir)
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt == 0:
                    try:
                        os.chmod(profile_dir, 0o700)
                    except OSError:
                        pass
                    continue
        raise BrowserContextError("browser profile cleanup failed") from None
