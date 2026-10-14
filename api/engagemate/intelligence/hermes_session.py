"""Bounded deletion of one private Hermes session before releasing its caller."""

from __future__ import annotations

import asyncio

import httpx

# Session deletion must not inherit the 1500-second inference request timeout.
_SESSION_CLEANUP_SECONDS = 15.0
_MAX_RESPONSE_BYTES = 1_000_000


async def delete_hermes_session(
    http: httpx.AsyncClient, session_base: str, session_id: str, api_key: str,
) -> None:
    """Drain a single deletion even if shutdown cancels the caller repeatedly."""
    deletion = asyncio.create_task(_delete(http, session_base, session_id, api_key))
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(deletion)
            break
        except asyncio.CancelledError as error:
            if deletion.cancelled():
                raise RuntimeError("Hermes session cleanup failed") from None
            cancellation = error
        except RuntimeError:
            raise RuntimeError("Hermes session cleanup failed") from cancellation
    if cancellation is not None:
        raise cancellation


async def _delete(
    http: httpx.AsyncClient, session_base: str, session_id: str, api_key: str,
) -> None:
    try:
        async with asyncio.timeout(_SESSION_CLEANUP_SECONDS):
            response = await http.delete(
                f"{session_base}/{session_id}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=_SESSION_CLEANUP_SECONDS,
            )
            if response.status_code != 200 or len(response.content) > _MAX_RESPONSE_BYTES:
                raise ValueError
            body = response.json()
            if not isinstance(body, dict) or body.get("deleted") is not True:
                raise ValueError
    except Exception:
        raise RuntimeError("Hermes session cleanup failed") from None
