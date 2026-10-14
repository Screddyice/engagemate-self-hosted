"""The public origin of the EngageMate web app, taken from configuration only.

There is deliberately no built-in default. The project's original domain was
given up, so any hardcoded hostname would point links, emails, and trust checks
at whoever registers it next. A deployment names its own origin in
ENGAGEMATE_WEB_ORIGIN; until it does, anything that needs a public link stops
with an error instead of guessing.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

WEB_ORIGIN_ENV = "ENGAGEMATE_WEB_ORIGIN"
SUPPORT_EMAIL_ENV = "ENGAGEMATE_SUPPORT_EMAIL"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class WebOriginNotConfigured(RuntimeError):
    """Raised when a public link is needed but no origin is configured."""


def normalize_origin(value: str, *, name: str = WEB_ORIGIN_ENV) -> str:
    """Return `scheme://host[:port]` or raise ValueError for anything else.

    HTTPS is required except on loopback, so a local development origin works
    and a production one cannot be downgraded.
    """

    parsed = urlsplit(str(value).strip())
    host = parsed.hostname
    if not host:
        raise ValueError(f"{name} must include a host")
    if parsed.username or parsed.password:
        raise ValueError(f"{name} must not include credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError(f"{name} must be a bare origin such as https://app.example.com")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and host in LOOPBACK_HOSTS
    ):
        raise ValueError(f"{name} must use HTTPS outside loopback")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def web_origin(environ: Mapping[str, str] | None = None) -> str:
    """The configured web origin. Raises when it is unset or malformed."""

    env = os.environ if environ is None else environ
    value = (env.get(WEB_ORIGIN_ENV) or "").strip()
    if not value:
        raise WebOriginNotConfigured(
            f"{WEB_ORIGIN_ENV} is not set. Set it to the public origin of the "
            "EngageMate web app, for example https://app.example.com."
        )
    return normalize_origin(value)


def web_url(path: str, environ: Mapping[str, str] | None = None) -> str:
    """An absolute link on the configured web origin."""

    if not path.startswith("/"):
        raise ValueError("web_url paths must start with /")
    return web_origin(environ) + path


def support_email(environ: Mapping[str, str] | None = None) -> str | None:
    """The configured support address, or None when no address is configured."""

    env = os.environ if environ is None else environ
    value = (env.get(SUPPORT_EMAIL_ENV) or "").strip()
    if not value:
        return None
    local, _, domain = value.partition("@")
    if not local or "." not in domain or any(ch.isspace() for ch in value):
        raise ValueError(f"{SUPPORT_EMAIL_ENV} must be a plain email address")
    return value
