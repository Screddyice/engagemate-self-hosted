from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from engagemate.domain.budgets import ActionBudget
from engagemate.worker.active_run import ActiveRunRegistry
from engagemate.worker.execution_mcp_server import (
    build_execution_mcp,
    build_execution_mcp_app,
)
from engagemate.worker.story_types import (
    StoryContent,
    StoryMedia,
    StoryMediaType,
    StoryOpportunity,
)
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.types import ImageContent, TextContent

EXPECTED = {
    "instagram.discover",
    "instagram.view",
    "instagram.like",
    "instagram.reply",
    "instagram.complete_run",
}
KEY = "T9x_2AbCdEfGhJkLmNpQrStUvWxYz0123456789-"


class _Discovery:
    async def find(self, context, targets, budget):
        return [
            StoryOpportunity(
                UUID("33333333-3333-4333-8333-333333333333"),
                "owner-1",
                "approved.target",
                "vip",
                "media-1",
                datetime.now(UTC),
                "a" * 64,
                StoryContent(StoryMediaType.IMAGE, "caption", None, None, ()),
            )
        ]


def _active_run():
    return SimpleNamespace(
        completed=False,
        stopping=False,
        tool_tasks=set(),
        check_deadline=None,
        config_snapshot={},
        discovery=_Discovery(),
        discovery_context=SimpleNamespace(),
        context=SimpleNamespace(
            targets=(),
            history=SimpleNamespace(
                recent_interaction_summary="No prior contact", sent_messages=()
            ),
        ),
        budget=ActionBudget(1, 1, 1, plan_total=3, views=1),
        opportunities={},
    )


@pytest.mark.asyncio
async def test_private_protocol_initializes_and_binds_calls_to_the_active_run() -> None:
    registry = ActiveRunRegistry()
    server = build_execution_mcp(registry, KEY)

    with registry.activate(_active_run()):
        async with Client(server, mode="legacy") as client:
            tools = await client.list_tools()
            result = await client.call_tool("instagram.discover", {"limit": 1})

    assert {tool.name for tool in tools.tools} == EXPECTED
    assert len(result.structured_content["opportunities"]) == 1


def test_private_http_transport_requires_the_exact_bearer() -> None:
    app = build_execution_mcp_app(ActiveRunRegistry(), KEY)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "execution-test", "version": "1"},
        },
    }
    base_headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }

    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        missing = client.post("/mcp", headers=base_headers, json=initialize)
        wrong = client.post(
            "/mcp",
            headers={**base_headers, "authorization": f"Bearer {'x' * len(KEY)}"},
            json=initialize,
        )
        accepted = client.post(
            "/mcp",
            headers={**base_headers, "authorization": f"Bearer {KEY}"},
            json=initialize,
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert accepted.status_code == 200
    assert accepted.headers["mcp-session-id"]
    assert '"name":"engagemate-execution"' in accepted.text


def test_private_http_transport_rejects_non_loopback_bind() -> None:
    with pytest.raises(ValueError, match="IPv4 loopback"):
        build_execution_mcp_app(ActiveRunRegistry(), KEY, host="0.0.0.0")


@pytest.mark.asyncio
async def test_discovery_roundtrip_preserves_native_images_and_story_association():
    # A real one-pixel PNG fixture; the transport must preserve these exact bytes.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII="
    )
    media = StoryMedia("image/png", png)
    run = _active_run()
    (original,) = await run.discovery.find(None, None, None)
    image_story = replace(
        original,
        content=StoryContent(StoryMediaType.IMAGE, "first image", None, media, ()),
    )
    video_story = replace(
        original,
        target_handle="second.target",
        fingerprint="b" * 64,
        content=StoryContent(
            StoryMediaType.VIDEO, "second video", None, None, (media,) * 5
        ),
    )
    run.discovery.find = AsyncMock(return_value=[image_story, video_story])
    registry = ActiveRunRegistry()
    server = build_execution_mcp(registry, KEY)
    with registry.activate(run):
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("instagram.discover", {"limit": 2})

    assert not result.is_error
    assert len(result.content) == 6  # One metadata block, image, four video frames.
    text = result.content[0]
    assert isinstance(text, TextContent)
    assert json.loads(text.text) == result.structured_content
    assert "base64" not in text.text and media.data.hex() not in text.text
    assert base64.b64encode(png).decode("ascii") not in text.text
    entries = result.structured_content["opportunities"]
    assert [len(entry["story"]["media"]) for entry in entries] == [1, 4]
    for entry in entries:
        for frame_index, reference in enumerate(entry["story"]["media"]):
            native = result.content[reference["content_index"]]
            assert isinstance(native, ImageContent)
            assert native.mime_type == reference["mime_type"] == "image/png"
            assert base64.b64decode(native.data, validate=True) == png
            assert native.meta == {
                "opportunity_id": entry["opportunity_id"],
                "frame_index": frame_index,
            }


@pytest.mark.asyncio
async def test_discovery_roundtrip_omits_unusable_media_without_dangling_references():
    run = _active_run()
    (original,) = await run.discovery.find(None, None, None)
    run.discovery.find = AsyncMock(
        return_value=[
            replace(
                original,
                content=StoryContent(
                    StoryMediaType.VIDEO,
                    None,
                    None,
                    StoryMedia("image/jpeg", b"x" * (512 * 1024 + 1)),
                    (StoryMedia("video/mp4", b"video"), StoryMedia("image/png", b"")),
                ),
            )
        ]
    )
    registry = ActiveRunRegistry()
    with registry.activate(run):
        async with Client(build_execution_mcp(registry, KEY), mode="legacy") as client:
            result = await client.call_tool("instagram.discover", {"limit": 1})
    assert not result.is_error
    assert len(result.content) == 1
    assert result.structured_content["opportunities"][0]["story"]["media"] == []
    assert json.loads(result.content[0].text) == result.structured_content
