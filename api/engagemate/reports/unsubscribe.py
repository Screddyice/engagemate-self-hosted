"""Signed, login-free unsubscribe links for each category of user email.

A link carries the user id, the category, and an HMAC over both. The web app
verifies the same HMAC before it changes anything, so a link can only turn off
the one category it names for the one user it was sent to. The key is derived
from INTERNAL_API_KEY with its own label, the pattern extension tickets use,
and both the api and web containers hold that key. Rotating INTERNAL_API_KEY
invalidates links already sent; the Settings page still works.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import urlencode
from uuid import UUID

from engagemate.web_origin import web_url

CATEGORIES = ("activity", "onboarding", "weekly_report")
_LABEL = b"engagemate-email-unsubscribe-v1"


def _key(secret: str) -> bytes:
    if type(secret) is not str or not secret:
        raise ValueError("unsubscribe signing key is required")
    return hmac.new(secret.encode(), _LABEL, hashlib.sha256).digest()


def unsubscribe_token(secret: str, user_id: UUID | str, category: str) -> str:
    if category not in CATEGORIES:
        raise ValueError("unknown email category")
    message = f"{UUID(str(user_id))}:{category}".encode()
    digest = hmac.new(_key(secret), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def unsubscribe_url(secret: str, user_id: UUID | str, category: str) -> str:
    query = urlencode({"u": str(UUID(str(user_id))), "c": category,
                       "t": unsubscribe_token(secret, user_id, category)})
    return web_url(f"/unsubscribe?{query}")


def text_footer(url: str, description: str) -> str:
    return (f"\n\n--\nYou get {description} because you have an EngageMate account.\n"
            f"Unsubscribe: {url}\nManage email settings: {web_url('/settings')}")
