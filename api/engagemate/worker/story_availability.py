"""Normalize explicit Story availability evidence at the browser boundary."""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlsplit

# Match whole native status messages. A caption mentioning an unavailable
# product, appointment, or person is not an Instagram availability signal.
_UNAVAILABLE_STATUS = re.compile(
    r"^(?:this story is (?:unavailable|no longer available|not available|expired)|"
    r"this story (?:has expired|has been deleted|is no longer available)|"
    r"story (?:unavailable|not available|has expired)|"
    r"sorry, this story (?:is no longer available|is unavailable)|"
    r"this post is unavailable|this content isn['’]t available right now)\.?$",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"(?:placeholder|unavailable|not[_ -]?found|deleted|expired)", re.I)


async def visible_story_unavailable(page: Any) -> bool:
    """Veto only visible, explicit native Story status text, never loose keywords."""
    messages = page.get_by_text(_UNAVAILABLE_STATUS, exact=True)
    count = await messages.count()
    if count > 32:
        # An ambiguous page cannot establish usable Story content.
        return True
    for index in range(count):
        if await messages.nth(index).is_visible():
            return True
    return False


def story_media_unavailable(evidence: object) -> bool:
    """Recognize explicit error/placeholder asset paths, not an unloaded image."""
    source = evidence.get("source") if type(evidence) is dict else None
    if type(source) is not str:
        return False
    try:
        return bool(_PLACEHOLDER.search(urlsplit(source).path))
    except ValueError:
        return False


def loaded_story_media(evidence: object) -> bool:
    """Require decoded content from a media source, not a large UI placeholder."""
    if type(evidence) is not dict or evidence.get("loaded") is not True:
        return False
    width, height = evidence.get("width"), evidence.get("height")
    if (
        evidence.get("tag") not in {"IMG", "VIDEO"}
        or type(width) not in (int, float)
        or type(height) not in (int, float)
        or not math.isfinite(width)
        or not math.isfinite(height)
        or width < 200
        or height < 200
    ):
        return False
    source = evidence.get("source")
    if type(source) is not str or not source or _PLACEHOLDER.search(source):
        return False
    try:
        parsed = urlsplit(source)
        if parsed.scheme == "blob" and evidence.get("tag") == "VIDEO":
            parsed = urlsplit(source.removeprefix("blob:"))
            return parsed.scheme == "https" and parsed.netloc in {
                "www.instagram.com", "instagram.com"
            }
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and any(
            host == domain or host.endswith("." + domain)
            for domain in ("cdninstagram.com", "fbcdn.net")
        )
    except ValueError:
        return False
