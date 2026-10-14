"""Bounded browser commands; never accept a URL, selector, or CDP method."""

from typing import Annotated, Literal
from uuid import UUID
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

WIDTH, HEIGHT = 1024, 768


class ConnectError(Exception):
    def __init__(self, code: str, status: int = 503):
        self.code, self.status = code, status
        super().__init__(code)


class BrowserCleanupError(Exception):
    """An incompletely closed Chrome must keep its reserved endpoint."""
    def __init__(self, browser):
        self.browser = browser
        super().__init__("browser_cleanup_failed")


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=30, pattern=r"^[a-z0-9._]+$")
    timezone: str = Field(min_length=1, max_length=64)
    account_id: UUID | None = None


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class SignIn(_Command):
    """One customer-initiated login attempt in the owned temporary browser."""

    type: Literal["sign_in"]
    username: str = Field(min_length=1, max_length=30, pattern=r"^[a-z0-9._]+$")
    password: str = Field(min_length=1, max_length=1024, repr=False)

    @field_validator("password")
    @classmethod
    def no_password_controls(cls, value):
        if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
            raise ValueError("unsupported password")
        return value


class SubmitCode(_Command):
    type: Literal["submit_code"]
    code: str = Field(min_length=4, max_length=20, pattern=r"^[A-Za-z0-9 -]+$", repr=False)


class ChooseAction(_Command):
    type: Literal["choose_action"]
    action: Literal[
        "send_code", "continue_verification", "resend_code", "try_another_way", "use_sms", "use_whatsapp",
        "use_authenticator", "use_email", "use_backup_code", "confirm_login",
    ]


Command = Annotated[SignIn | SubmitCode | ChooseAction, Field(discriminator="type")]


def allowed_destination(url: str, navigation: bool = False) -> bool:
    if not isinstance(url, str) or not url:
        return False

    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return False

    try:
        parsed = urlsplit(url)

        if parsed.scheme.lower() != "https" or not parsed.netloc:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        if parsed.port not in (None, 443):
            return False

        host = parsed.hostname
        if not host or host.endswith(".") or "\\" in host:
            return False

        host = host.lower()

        if host == "instagram.com" or host.endswith(".instagram.com"):
            return True

        if navigation:
            return False

        for domain in ("cdninstagram.com", "fbcdn.net", "recaptcha.net", "gstatic.com"):
            if host == domain or host.endswith("." + domain):
                return True

        return host == "www.google.com" and parsed.path.startswith("/recaptcha/")
    except (TypeError, ValueError, UnicodeError):
        return False
