"""Restore native Story images at Hermes' profile-scoped middleware boundary.

The installed MCP bridge caches ImageContent and emits MEDIA paths. This plugin
accepts only its exact discovery envelope: matching JSON metadata followed by
one cache reference per indexed image. It never treats a caption's MEDIA text
as a file reference and registers no model tools.
"""

# ruff: noqa: N999

from __future__ import annotations

import base64
import json
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

_TOOL = "mcp__engagemate_execution__instagram_discover"
_MAX_TEXT = 256 * 1024
_MAX_IMAGE = 512 * 1024
_MAX_TOTAL = 10 * 1024 * 1024
_MAX_OPPORTUNITIES = 25
_MAX_FRAMES = 4
_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_CACHE_NAME = re.compile(r"img_[0-9a-f]{12}\.(?:png|jpe?g|gif|webp)\Z")
_ERROR = json.dumps(
    {
        "error": "story_media_unavailable",
        "message": "Story images could not be validated. Do not reply or DM from this result.",
    }
)
_SERVER = "engagemate_execution"
_TOOLSET = f"mcp-{_SERVER}"
_EXECUTION_TOOLS = frozenset(
    f"mcp__{_SERVER}__instagram_{action}"
    for action in ("discover", "view", "like", "reply", "complete_run")
)
_EXECUTION_REQUIRED_ARGS = {
    f"mcp__{_SERVER}__instagram_discover": frozenset(),
    # Hermes occasionally omits the model-generated idempotency key. The
    # execution boundary derives a stable key from the opaque opportunity ID,
    # while still validating any key the model supplies.
    f"mcp__{_SERVER}__instagram_view": frozenset({"opportunity_id"}),
    f"mcp__{_SERVER}__instagram_like": frozenset({"opportunity_id"}),
    f"mcp__{_SERVER}__instagram_reply": frozenset(
        {"opportunity_id", "generated_text", "story_detail"}
    ),
    f"mcp__{_SERVER}__instagram_complete_run": frozenset({"reason"}),
}
_ADMISSION_SECONDS = 15.0
_SESSION = re.compile(r"em_[A-Za-z0-9_-]{32}\Z")
_READY_ERROR = "engagemate_execution_mcp_unavailable"
_LOG = logging.getLogger(__name__)


class _InvalidImageResult(ValueError):
    pass


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidImageResult()
        result[key] = value
    return result


def _load(value: str) -> Any:
    return json.loads(value, object_pairs_hook=_unique)


def _references(metadata: dict[str, Any]) -> list[str]:
    opportunities = metadata.get("opportunities")
    if type(opportunities) is not list or len(opportunities) > _MAX_OPPORTUNITIES:
        raise _InvalidImageResult()
    identities = set()
    mimes = []
    for opportunity in opportunities:
        if type(opportunity) is not dict:
            raise _InvalidImageResult()
        identity = opportunity.get("opportunity_id")
        if (
            type(identity) is not str
            or str(UUID(identity)) != identity
            or identity in identities
        ):
            raise _InvalidImageResult()
        identities.add(identity)
        story = opportunity.get("story")
        if type(story) is not dict or story.get("media_type") not in {"image", "video"}:
            raise _InvalidImageResult()
        frames = story.get("media")
        if type(frames) is not list or not 1 <= len(frames) <= _MAX_FRAMES:
            raise _InvalidImageResult()
        if story["media_type"] == "image" and len(frames) != 1:
            raise _InvalidImageResult()
        for frame in frames:
            if type(frame) is not dict or set(frame) != {"content_index", "mime_type"}:
                raise _InvalidImageResult()
            if (
                type(frame["content_index"]) is not int
                or frame["content_index"] != len(mimes) + 1
            ):
                raise _InvalidImageResult()
            mime = frame["mime_type"]
            if type(mime) is not str or mime not in _MIMES:
                raise _InvalidImageResult()
            mimes.append(mime)
    return mimes


def _mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _cached_image(reference: str, mime: str, root: Path) -> bytes:
    if type(reference) is not str or not reference.startswith("MEDIA:"):
        raise _InvalidImageResult()
    path = Path(reference.removeprefix("MEDIA:"))
    if (
        not path.is_absolute()
        or path.parent != root
        or _CACHE_NAME.fullmatch(path.name) is None
    ):
        raise _InvalidImageResult()
    # Resolve the trusted cache root before this function. Open by directory
    # descriptor and reject symlinks, including a root swap between validation
    # and open. O_NONBLOCK prevents a forged FIFO from stalling validation.
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        image_fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd
        )
        try:
            info = os.fstat(image_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not 0 < info.st_size <= _MAX_IMAGE
            ):
                raise _InvalidImageResult()
            with os.fdopen(image_fd, "rb", closefd=False) as stream:
                data = stream.read(_MAX_IMAGE + 1)
            if len(data) != info.st_size or _mime(data) != mime:
                raise _InvalidImageResult()
            return data
        finally:
            os.close(image_fd)
    finally:
        os.close(root_fd)


def transform_discovery_result(
    result: Any, cache_root: Path | Callable[[], Path]
) -> Any:
    """Convert the bridge's indexed image result; preserve denied/plain results.

    Catch validation failures here: Hermes execution middleware deliberately
    falls back to the original result if a callback raises after next_call.
    A malformed image result must instead withhold opportunities from the model.
    """
    if type(result) is not str:
        return result
    try:
        if len(result) > _MAX_TEXT:
            raise _InvalidImageResult()
        try:
            wrapper = _load(result)
        except json.JSONDecodeError:
            return result
        if type(wrapper) is not dict:
            return result
        metadata = wrapper.get("structuredContent")
        text = wrapper.get("result")
        if metadata is None:
            # Denials and plain tool results carry no indexed native images.
            return result
        if type(metadata) is not dict:
            raise _InvalidImageResult()
        if metadata.get("status") == "denied" or "opportunities" not in metadata:
            return result
        mimes = _references(metadata)
        if not mimes:
            return result
        if type(text) is not str:
            raise _InvalidImageResult()
        stripped = text.lstrip()
        decoded, end = json.JSONDecoder(object_pairs_hook=_unique).raw_decode(stripped)
        if decoded != metadata:
            raise _InvalidImageResult()
        remainder = stripped[end:]
        if not remainder.startswith("\n"):
            raise _InvalidImageResult()
        references = remainder[1:].split("\n")
        if len(references) != len(mimes) or len(set(references)) != len(references):
            raise _InvalidImageResult()
        root = Path(cache_root() if callable(cache_root) else cache_root)
        if root.is_symlink() or not root.is_dir():
            raise _InvalidImageResult()
        root = root.resolve(strict=True)
        summary = json.dumps(metadata, ensure_ascii=False)
        content = [{"type": "text", "text": summary}]
        total = 0
        for reference, mime in zip(references, mimes, strict=True):
            data = _cached_image(reference, mime, root)
            total += len(data)
            if total > _MAX_TOTAL:
                raise _InvalidImageResult()
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                    },
                }
            )
        # Index zero remains metadata; its content_index values therefore bind
        # each image to its opportunity and frame order without extra tool calls.
        return {"_multimodal": True, "content": content, "text_summary": summary}
    except Exception:  # noqa: BLE001 - prevent Hermes middleware from falling back to unreadable evidence
        return _ERROR


def _middleware(*, tool_name: str, args: dict, next_call: Any, **_: Any) -> Any:
    result = next_call(args)
    if tool_name != _TOOL:
        return result
    return transform_discovery_result(result, _current_cache_root)


def _current_cache_root() -> Path:
    from gateway.platforms.base import get_image_cache_dir

    return get_image_cache_dir()


def _execution_config(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Admit only this profile's authenticated loopback execution endpoint."""
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict) or set(servers) != {_SERVER}:
        raise ValueError(_READY_ERROR)
    server = servers[_SERVER]
    key = os.environ.get("ENGAGEMATE_EXECUTION_MCP_KEY", "")
    search = (config.get("tools") or {}).get("tool_search")
    if (
        config.get("toolsets") != []
        or (config.get("platform_toolsets") or {}).get("api_server") != [_TOOLSET]
        or not isinstance(search, dict)
        or search.get("enabled") != "off"
        or (config.get("agent") or {}).get("disabled_toolsets", []) != []
        or not isinstance(server, dict)
        or server.get("url") != "http://127.0.0.1:8766/mcp"
        or server.get("headers") not in (
            {"Authorization": "Bearer ${ENGAGEMATE_EXECUTION_MCP_KEY}"},
            {"Authorization": f"Bearer {key}"},
        )
        or server.get("supports_parallel_tool_calls") is not False
        or server.get("enabled", True) is not True
        or server.get("lazy", False) is not False
        or server.get("tools") != {"resources": False, "prompts": False}
        or any(name in server for name in ("command", "args", "auth", "transport"))
        or len(key) < 32
        or any(character.isspace() for character in key)
    ):
        raise ValueError(_READY_ERROR)
    # Explicit native registration expects already interpolated headers.
    return {_SERVER: {**server, "headers": {"Authorization": f"Bearer {key}"}}}


def _clear_execution_schema_cache() -> None:
    """Force the next registration to discover the live execution schemas.

    Hermes persists MCP manifests separately from the running server. Older
    cache writers can save an empty ``inputSchema`` when the SDK exposes the
    snake_case attribute, so a cached tool name can look healthy while every
    write argument is rejected. Clearing only this profile's entry is safe;
    the readiness check below still fails closed if the refreshed surface is
    malformed.
    """
    try:
        from tools import mcp_schema_cache

        clear = getattr(mcp_schema_cache, "clear_cache_entry", None)
        if callable(clear):
            clear(_SERVER)
    except Exception:  # noqa: BLE001 - cache cleanup must not expose provider details
        _LOG.warning("%s stage=schema_cache_clear", _READY_ERROR)


def _execution_tool_surface_ready(agent: Any) -> bool:
    """Require the complete, parameterized execution surface before the LLM."""
    if set(getattr(agent, "valid_tool_names", ())) != _EXECUTION_TOOLS:
        return False
    tools = getattr(agent, "tools", None)
    if not isinstance(tools, list) or len(tools) != len(_EXECUTION_TOOLS):
        return False
    seen: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            return False
        function = tool.get("function")
        if not isinstance(function, dict):
            return False
        name = function.get("name")
        if not isinstance(name, str) or name in seen or name not in _EXECUTION_TOOLS:
            return False
        parameters = function.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            return False
        properties = parameters.get("properties")
        required = parameters.get("required", [])
        expected = _EXECUTION_REQUIRED_ARGS[name]
        if (
            not isinstance(properties, dict)
            or not isinstance(required, list)
            or any(not isinstance(argument, str) for argument in required)
            or set(required) != expected
            or not expected.issubset(properties)
        ):
            return False
        seen.add(name)
    return seen == _EXECUTION_TOOLS


def _warm_execution(agent: Any, config: dict, native: Any, cancelled: threading.Event) -> bool:
    """Reconnect only the configured server; never invoke an execution tool."""
    servers = _execution_config(config)
    deadline = time.monotonic() + _ADMISSION_SECONDS
    _clear_execution_schema_cache()
    with native._lock:
        cached = native._servers.get(_SERVER)
    native.register_mcp_servers(servers)
    if cached is not None and not cancelled.is_set():
        # Native status alone accepts a cached, dead socket. Hermes' helper
        # clears readiness on its MCP loop and requires a different session.
        if not native._signal_reconnect_and_wait(
            _SERVER, cached, op_description="EngageMate run admission",
            timeout=max(0.0, deadline - time.monotonic()),
        ):
            _LOG.warning("%s stage=reconnect", _READY_ERROR)
            return False
    # Registration wakes parked transports asynchronously. Waiting only for its
    # return would refresh from the same empty registry that caused the failure.
    while not cancelled.is_set() and time.monotonic() < deadline:
        status = next(
            (entry for entry in native.get_mcp_status() if entry.get("name") == _SERVER),
            {},
        )
        if status.get("connected") is True and status.get("tools", 0) >= len(_EXECUTION_TOOLS):
            # Restore the validated profile selection, not an agent's stale
            # startup snapshot. Use native refresh so generation guards and
            # atomic (tools, valid_tool_names) publication remain intact.
            native.refresh_agent_mcp_tools(
                agent, enabled_override=[_TOOLSET], disabled_override=[], quiet_mode=True,
            )
            ready = _execution_tool_surface_ready(agent)
            if not ready:
                _LOG.warning("%s stage=tool_surface_schema", _READY_ERROR)
            return ready
        if cancelled.wait(0.1):
            break
    _LOG.warning("%s stage=deadline", _READY_ERROR)
    return False


def _admit_execution(*, session_id: str, platform: str = "", **_: Any) -> None:
    """Stop an unready execution session before Hermes' first model call.

    Hermes isolates plugin exceptions, so an exception alone would fail open.
    Use its native hard interrupt, checked before the model loop's first call.
    The active-agent ContextVar is bound by run_agent for the entire turn.
    """
    if not _SESSION.fullmatch(session_id) or platform != "api_server":
        return
    from agent.subagent_lifecycle import get_active_subagent_parent

    agent = get_active_subagent_parent()
    if agent is None:
        # The installed-host contract probe rejects versions without this bind.
        raise RuntimeError(_READY_ERROR)
    if getattr(agent, "session_id", None) != session_id:
        agent.interrupt(_READY_ERROR, hard_cancel=True)
        _LOG.warning(_READY_ERROR)
        return
    admission = getattr(agent, "_engagemate_execution_admission", None)
    if admission is True:
        return
    if admission is False:
        agent.interrupt(_READY_ERROR, hard_cancel=True)
        return
    agent._engagemate_execution_admission = False
    cancelled = threading.Event()
    ready = threading.Event()

    def warm() -> None:
        try:
            from hermes_cli.config import load_config
            from tools import mcp_tool

            if _warm_execution(agent, load_config(), mcp_tool, cancelled):
                ready.set()
        except Exception:  # noqa: BLE001 - emit only a fixed admission failure
            _LOG.warning("%s stage=admission_exception", _READY_ERROR)

    admitted = False
    try:
        task = threading.Thread(target=warm, name="engagemate-mcp-admission", daemon=True)
        task.start()
        task.join(_ADMISSION_SECONDS)
        admitted = ready.is_set() and not task.is_alive()
    except Exception:  # noqa: BLE001 - native hooks swallow errors, so interrupt below
        pass
    finally:
        cancelled.set()
    if admitted:
        agent._engagemate_execution_admission = True
        _LOG.info("engagemate_execution_mcp_ready")
        return
    agent.interrupt(_READY_ERROR, hard_cancel=True)
    _LOG.warning(_READY_ERROR)


def register(ctx: Any) -> None:
    ctx.register_middleware("tool_execution", _middleware)
    ctx.register_hook("on_session_start", _admit_execution)
    ctx.register_hook("pre_llm_call", _admit_execution)
