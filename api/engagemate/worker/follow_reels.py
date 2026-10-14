"""Choose the three newest verified sender reels and compose a friendly ask."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

_CODE = re.compile(r"[A-Za-z0-9_-]{5,32}\Z")
DEFAULT_MESSAGE_TEMPLATE = (
    "Hey, hope you're doing well! Wanted to ask for your support on my latest reel. "
    "A like or comment would go a long way as I'm growing my account. Appreciate you!"
)

_OWNER = re.compile(r"[1-9][0-9]{0,31}\Z")


@dataclass(frozen=True, slots=True)
class Reel:
    code: str
    posted_at: datetime
    owner_id: str

    def __post_init__(self) -> None:
        if not _CODE.fullmatch(self.code):
            raise ValueError("invalid reel code")
        if self.posted_at.tzinfo is None:
            raise ValueError("reel timestamp must have a timezone")
        if not _OWNER.fullmatch(self.owner_id):
            raise ValueError("invalid reel owner")

    @property
    def url(self) -> str:
        return f"https://www.instagram.com/reel/{self.code}/"


def choose_reel(reels: list[Reel], sender_id: str, rotation_slot: int) -> Reel:
    """Sort by publication time so pinned or reordered tiles do not skew rotation."""
    if type(rotation_slot) is not int or rotation_slot < 0 or not _OWNER.fullmatch(sender_id):
        raise ValueError("invalid reel rotation")
    distinct: dict[str, Reel] = {}
    for reel in reels:
        if reel.owner_id != sender_id or reel.posted_at > datetime.now(UTC):
            continue
        previous = distinct.get(reel.code)
        if previous is None or reel.posted_at > previous.posted_at:
            distinct[reel.code] = reel
    latest = sorted(distinct.values(), key=lambda item: (item.posted_at, item.code), reverse=True)[:3]
    if len(latest) != 3:
        raise ValueError("three verified sender reels are required")
    return latest[rotation_slot % 3]


def compose_message(reel_url: str, template: str = DEFAULT_MESSAGE_TEMPLATE) -> str:
    parsed = urlsplit(reel_url)
    if (parsed.scheme != "https" or parsed.hostname != "www.instagram.com"
            or parsed.username or parsed.password or parsed.port is not None
            or not re.fullmatch(r"/reel/[A-Za-z0-9_-]{5,32}/", parsed.path)
            or parsed.query or parsed.fragment):
        raise ValueError("invalid reel URL")
    if (not isinstance(template, str) or not template.strip() or len(template) > 500
            or any(ord(char) < 32 or ord(char) == 127 for char in template)
            or re.search(r"(?:[a-z][a-z0-9+.-]*://|www\.|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:\b|/))",
                         template, re.IGNORECASE)):
        raise ValueError("invalid reel message template")
    return f"{template.strip()}\n{reel_url}"
