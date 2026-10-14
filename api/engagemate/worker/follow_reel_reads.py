"""Validate the measured reel grid and its separate publication metadata."""

from datetime import UTC, datetime
import re

from engagemate.worker.follow_reels import Reel

_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_CODE = re.compile(r"[A-Za-z0-9_-]{5,32}\Z")


def reel_candidates(payload, sender_id):
    """Return exact owned reel candidates and the next native cursor.

    The grid can be reordered. Callers must exhaust bounded pagination and
    fetch publication metadata before selecting the newest reels.
    """
    if not isinstance(payload, dict) or payload.get("errors"):
        raise ValueError("reel response failed")
    data = payload.get("data")
    user = data.get("fetch__XDTUserDict") if isinstance(data, dict) else None
    connection = user.get("clips_connection") if isinstance(user, dict) else None
    if not isinstance(connection, dict):
        raise ValueError("reel connection missing")
    edges, paging = connection.get("edges"), connection.get("page_info")
    if (not isinstance(edges, list) or not 0 <= len(edges) <= 50
            or not isinstance(paging, dict) or type(paging.get("has_next_page")) is not bool):
        raise ValueError("reel page changed")
    candidates, seen = [], set()
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        media = node.get("media") if isinstance(node, dict) else None
        if not isinstance(media, dict):
            raise ValueError("reel media missing")
        # Instagram's include_feed_video also returns regular feed videos.
        if media.get("product_type") != "clips":
            continue
        owner = media.get("user")
        if not isinstance(owner, dict) or str(owner.get("pk")) != sender_id:
            continue
        key, code = str(media.get("pk")), media.get("code")
        pins = media.get("clips_tab_pinned_user_ids")
        if (not _ID.fullmatch(key) or not isinstance(code, str) or not _CODE.fullmatch(code)
                or not isinstance(pins, list) or any(not isinstance(p, str) or not _ID.fullmatch(p) for p in pins)
                or key in seen):
            raise ValueError("reel identity changed")
        pinned = sender_id in pins
        seen.add(key)
        candidates.append({"id": key, "code": code, "pinned": pinned})
    cursor = paging.get("end_cursor") if paging["has_next_page"] else None
    if paging["has_next_page"] and (
            not edges or not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048
            or any(ord(c) < 32 or ord(c) == 127 for c in cursor)):
        raise ValueError("reel cursor changed")
    return candidates, cursor


def verified_reel_detail(payload, candidate, sender_id):
    """Require exact media, owner, reel type, and real publication timestamp."""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ValueError("media read failed")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("ambiguous media")
    media = items[0]
    owner, stamp = media.get("user"), media.get("taken_at")
    if (str(media.get("pk")) != candidate["id"] or media.get("code") != candidate["code"]
            or media.get("product_type") != "clips" or not isinstance(owner, dict)
            or str(owner.get("pk")) != sender_id or type(stamp) is not int
            or not 0 < stamp <= datetime.now(UTC).timestamp()):
        raise ValueError("unverified reel metadata")
    return Reel(candidate["code"], datetime.fromtimestamp(stamp, UTC), sender_id)
