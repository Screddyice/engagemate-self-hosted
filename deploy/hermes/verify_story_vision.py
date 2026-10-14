"""Offline installed-Hermes proof and fixture-only MCP/gateway vision check.

Modes never contact Instagram or start a gateway. `serve` exposes only generated
fixture discovery and completion on loopback. Start an isolated Hermes gateway
configured for that MCP port, then run `request` to verify pixels reach its model.
Credentials are read from environment and never emitted.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import secrets
import struct
import tempfile
import typing
import unicodedata
import uuid
import zlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = (
    ROOT
    / "deploy/hermes/engagemate-agent/plugins/engagemate-story-vision/__init__.py"
)
TOOL = "mcp__engagemate_execution__instagram_discover"
EXPECTED = {"code": "5827", "left_color": "red", "right_color": "blue"}


def fixture_image() -> bytes:
    width, height = 240, 160
    pixels = bytearray([255] * width * height * 3)

    def rect(x, y, w, h, color):
        for row in range(y, y + h):
            for column in range(x, x + w):
                offset = (row * width + column) * 3
                pixels[offset : offset + 3] = bytes(color)

    rect(15, 12, 85, 65, (255, 0, 0))
    rect(140, 12, 85, 65, (0, 0, 255))
    glyphs = {
        "5": [31, 16, 16, 30, 1, 1, 30],
        "8": [14, 17, 17, 14, 17, 17, 14],
        "2": [14, 17, 1, 2, 4, 8, 31],
        "7": [31, 1, 2, 4, 8, 8, 8],
    }
    for index, character in enumerate(EXPECTED["code"]):
        for row, bits in enumerate(glyphs[character]):
            for column in range(5):
                if bits & (1 << (4 - column)):
                    rect(39 + index * 42 + column * 6, 99 + row * 6, 6, 6, (0, 0, 0))

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    scanlines = b"".join(
        b"\0" + pixels[row * width * 3 : (row + 1) * width * 3] for row in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


def metadata() -> dict:
    return {
        "opportunities": [
            {
                "opportunity_id": str(uuid.uuid4()),
                "target": {"username": "fixture_only"},
                "story": {
                    "media_type": "image",
                    "caption": "",
                    "sticker": "",
                    "media": [{"content_index": 1, "mime_type": "image/png"}],
                },
                "allowed_actions": [],
            }
        ],
        "remaining_budget": {"views": 0, "likes": 0, "replies": 0, "dms": 0},
    }


def verify_execution_admission(hermes_root: Path) -> None:
    """Reject native versions without the bound-agent, reconnect, interrupt path."""
    def functions(relative):
        tree = ast.parse((hermes_root / relative).read_text())
        return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

    native = functions("tools/mcp_tool.py")
    required = {"register_mcp_servers", "get_mcp_status", "refresh_agent_mcp_tools",
                "_signal_reconnect_and_wait", "_wait_for_server_session_ready"}
    if not required.issubset(native):
        raise RuntimeError("installed Hermes execution admission contract changed")
    refresh = native["refresh_agent_mcp_tools"]
    if not {"enabled_override", "disabled_override"}.issubset(
        {arg.arg for arg in refresh.args.kwonlyargs}
    ):
        raise RuntimeError("installed Hermes execution admission contract changed")
    wait_source = ast.unparse(native["_wait_for_server_session_ready"])
    if "session is not old_session" not in wait_source or "is_ready" not in wait_source:
        raise RuntimeError("installed Hermes execution admission contract changed")
    reconnect = ast.unparse(native["_signal_reconnect_and_wait"])
    if "ready.clear()" not in reconnect or "old_session=old_session" not in reconnect:
        raise RuntimeError("installed Hermes execution admission contract changed")
    lifecycle = functions("agent/subagent_lifecycle.py")
    accessor = lifecycle.get("get_active_subagent_parent")
    if accessor is None or "_ACTIVE_PARENT_AGENT.get()" not in ast.unparse(accessor):
        raise RuntimeError("installed Hermes execution admission contract changed")
    facade = functions("run_agent.py")
    bound = any(
        isinstance(node, ast.With)
        and any("bind_subagent_parent(self)" == ast.unparse(item.context_expr)
                for item in node.items)
        and any(isinstance(child, ast.Call) and ast.unparse(child.func) == "run_conversation"
                for child in ast.walk(node))
        for node in ast.walk(facade["run_conversation"])
    )
    interrupt = facade["interrupt"]
    if not bound or "hard_cancel" not in {arg.arg for arg in interrupt.args.kwonlyargs} \
            or "self._interrupt_requested = True" not in ast.unparse(interrupt):
        raise RuntimeError("installed Hermes execution admission contract changed")
    loop = functions("agent/conversation_loop.py")["run_conversation"]
    for relative, hook in (("agent/conversation_loop.py", "on_session_start"),
                           ("agent/turn_context.py", "pre_llm_call")):
        tree = ast.parse((hermes_root / relative).read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and node.args
                 and isinstance(node.args[0], ast.Constant) and node.args[0].value == hook]
        if not any({kw.arg: ast.unparse(kw.value) for kw in node.keywords}.get("session_id") == "agent.session_id"
                   and {kw.arg: ast.unparse(kw.value) for kw in node.keywords}.get("platform") == "getattr(agent, 'platform', None) or ''"
                   for node in calls):
            raise RuntimeError("installed Hermes execution admission contract changed")
    api = ast.unparse(functions("gateway/platforms/api_server.py")["_create_agent"])
    if "'platform': 'api_server'" not in api or "'session_id': session_id" not in api:
        raise RuntimeError("installed Hermes execution admission contract changed")
    guard = False
    for node in ast.walk(loop):
        if not isinstance(node, ast.While):
            continue
        interrupt_positions = [index for index, child in enumerate(node.body)
                               if isinstance(child, ast.If)
                               and ast.unparse(child.test) == "agent._interrupt_requested"
                               and any(isinstance(descendant, ast.Break) for descendant in ast.walk(child))]
        call_positions = [index for index, child in enumerate(node.body)
                          if isinstance(child, ast.AugAssign)
                          and ast.unparse(child.target) == "api_call_count"]
        guard |= bool(interrupt_positions and call_positions
                      and min(interrupt_positions) < min(call_positions))
    if not guard:
        raise RuntimeError("installed Hermes execution admission contract changed")


def offline(hermes_root: Path, *, plugin_code: str | None = None) -> dict:
    """Extract pure installed functions; no Hermes bootstrap or network call."""
    verify_execution_admission(hermes_root)
    namespace = {
        **vars(typing),
        "logger": logging.getLogger("fixture"),
        "json": json,
        "hashlib": hashlib,
        "re": re,
        "uuid": uuid,
        "unicodedata": unicodedata,
        "DEFAULT_AGENT_IDENTITY": "fixture",
        "OBSERVER_SCHEMA_VERSION": "hermes.observer.v1",
        "MIDDLEWARE_SCHEMA_VERSION": "hermes.middleware.v1",
        "sanitize_text": lambda value: value,
    }
    sources = {
        "hermes_cli/middleware.py": {"middleware_payload", "_run_execution_chain"},
        "agent/tool_dispatch_helpers.py": {
            "_is_multimodal_tool_result",
            "_multimodal_text_summary",
        },
        "run_agent.py": {"_tool_result_content_for_active_model"},
        "agent/codex_responses_adapter.py": None,
    }
    for relative, names in sources.items():
        tree = ast.parse((hermes_root / relative).read_text())
        nodes = [
            node
            for node in (tree.body if names is None else ast.walk(tree))
            if isinstance(node, ast.FunctionDef)
            and (names is None or node.name in names)
        ]
        if names is not None and {node.name for node in nodes} != names:
            raise RuntimeError("installed Hermes fixture contract changed")
        if names is None:
            nodes = [
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.Assign))
                and not (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "logger"
                        for target in node.targets
                    )
                )
            ]
        module = ast.Module(body=nodes, type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), relative, "exec"), namespace)  # noqa: S102 - trusted installed source only
    plugin = {}
    exec(compile(plugin_code or PLUGIN.read_text(), str(PLUGIN), "exec"), plugin)  # noqa: S102 - versioned plugin only
    data = fixture_image()
    report = metadata()
    calls = []
    with tempfile.TemporaryDirectory(prefix="em-vision-fixture-") as temporary:
        cache = Path(temporary).resolve()
        image = cache / "img_0123456789ab.png"
        image.write_bytes(data)
        bridge = json.dumps(
            {
                "result": json.dumps(report) + "\nMEDIA:" + str(image),
                "structuredContent": report,
            }
        )
        plugin["_current_cache_root"] = lambda: cache

        def terminal(args):
            calls.append(args)
            return bridge

        native = namespace["_run_execution_chain"](
            "tool_execution",
            [plugin["_middleware"]],
            terminal,
            tool_name=TOOL,
            args={"limit": 1},
        )
        agent = SimpleNamespace(
            _content_has_image_parts=lambda _: True,
            _model_supports_vision=lambda: True,
            _provider_supports_vision_tool_messages=lambda: True,
            provider="openai-codex",
            model="fixture",
        )
        content = namespace["_tool_result_content_for_active_model"](
            agent, TOOL, native
        )
        outgoing = namespace["_chat_messages_to_responses_input"](
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_fixture",
                            "type": "function",
                            "function": {"name": TOOL, "arguments": '{"limit":1}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_fixture", "content": content},
            ]
        )
        outputs = [
            item for item in outgoing if item.get("type") == "function_call_output"
        ]
        assert len(outputs) == 1 and calls == [{"limit": 1}]
        parts = outputs[0]["output"]
        assert (
            parts[0]["type"] == "input_text" and json.loads(parts[0]["text"]) == report
        )
        assert parts[1]["type"] == "input_image"
        assert (
            base64.b64decode(parts[1]["image_url"].split(",", 1)[1], validate=True)
            == data
        )
        assert len(parts) == 2 and "MEDIA:" not in json.dumps(outgoing)
    return {
        "status": "passed",
        "terminal_calls": len(calls),
        "input_images": 1,
        "image_sha256": hashlib.sha256(data).hexdigest(),
        "execution_admission": "passed",
    }


def fixture_server(key: str):
    auth_path = ROOT / "api/engagemate/worker/execution_mcp_auth.py"
    spec = importlib.util.spec_from_file_location("fixture_execution_auth", auth_path)
    auth_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(auth_module)
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.mcpserver import MCPServer
    from mcp.types import CallToolResult, ImageContent, TextContent
    from pydantic import AnyHttpUrl

    server = MCPServer(
        "engagemate-vision-fixture",
        token_verifier=auth_module.ExecutionTokenVerifier(key),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl("http://localhost"),
            resource_server_url=None,
            required_scopes=["instagram:execute"],
        ),
    )
    counts = {"discover": 0, "complete_run": 0}

    @server.tool(name="instagram.discover")
    async def discover(limit: int = 1):
        counts["discover"] += 1
        report = metadata()
        if counts["discover"] != 1 or limit != 1:
            report = {"status": "denied", "reason": "fixture_discovery_already_used"}
            return CallToolResult(
                content=[TextContent(text=json.dumps(report))],
                structured_content=report,
            )
        return CallToolResult(
            content=[
                TextContent(text=json.dumps(report)),
                ImageContent(
                    data=base64.b64encode(fixture_image()).decode("ascii"),
                    mime_type="image/png",
                ),
            ],
            structured_content=report,
        )

    @server.tool(name="instagram.complete_run")
    async def complete_run(reason: typing.Literal["completed"]) -> dict:
        counts["complete_run"] += 1
        return {"status": "completed", "reason": reason, "fixture_counts": dict(counts)}

    return server, counts


def fixture_app(key: str):
    from starlette.responses import JSONResponse

    server, counts = fixture_server(key)
    app = server.streamable_http_app(streamable_http_path="/mcp", host="127.0.0.1")

    async def status(request):
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + key
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(dict(counts))

    app.add_route("/fixture-status", status, methods=["GET"])
    return app


async def request_fixture(
    url: str, *, timeout: int, fixture_url: str = "http://127.0.0.1:8767/fixture-status"
) -> dict:
    import httpx

    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.path != "/v1/chat/completions"
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError(
            "fixture gateway must be an explicit IPv4-loopback chat endpoint"
        )
    fixture_parsed = urlsplit(fixture_url)
    if (
        fixture_parsed.scheme != "http"
        or fixture_parsed.hostname != "127.0.0.1"
        or fixture_parsed.path != "/fixture-status"
        or fixture_parsed.query
        or fixture_parsed.fragment
        or fixture_parsed.username
    ):
        raise ValueError("fixture status must be an explicit IPv4-loopback endpoint")
    fixture_key = os.environ.get("ENGAGEMATE_EXECUTION_MCP_KEY")
    if not fixture_key:
        raise ValueError("fixture MCP credential is missing")
    key = os.environ.get("API_SERVER_KEY") or os.environ.get("HERMES_AGENT_API_KEY")
    if not key:
        raise ValueError("fixture gateway credential is missing")
    # This isolated image fixture has two generated tools and no account. It
    # never impersonates a production worker admission with its five tools.
    session = "emvision_" + secrets.token_urlsafe(24)
    headers = {"Authorization": "Bearer " + key, "X-Hermes-Session-Id": session}
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10)) as http:
        fixture_headers = {"Authorization": "Bearer " + fixture_key}
        initial = await http.get(fixture_url, headers=fixture_headers)
        initial.raise_for_status()
        if initial.json() != {"discover": 0, "complete_run": 0}:
            raise RuntimeError("fixture server must be fresh")
        try:
            response = await http.post(
                url,
                headers=headers,
                json={
                    "model": "engagemate-agent",
                    "stream": False,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "This is a fixture-only visual input test. Call instagram.discover(limit=1) exactly once. "
                                "Inspect its image pixels, then call instagram.complete_run(reason='completed'). "
                                "No Instagram account or action exists in this fixture. Return only a JSON object with "
                                "code (the four printed digits as a string), left_color, and right_color. "
                                "If no native image is available, return an error instead of guessing."
                            ),
                        }
                    ],
                },
            )
            response.raise_for_status()
            answer = json.loads(response.json()["choices"][0]["message"]["content"])
            if (
                answer != EXPECTED
                or response.headers.get("X-Hermes-Session-Id") != session
            ):
                raise RuntimeError("fixture vision answer or session mismatch")
            status = await http.get(fixture_url, headers=fixture_headers)
            status.raise_for_status()
            if status.json() != {"discover": 1, "complete_run": 1}:
                raise RuntimeError("fixture tool count mismatch")
        finally:
            cleanup = await http.delete(
                url.removesuffix("/v1/chat/completions") + "/api/sessions/" + session,
                headers=headers,
                timeout=15,
            )
            cleanup.raise_for_status()
    return {"status": "passed", "image_fields_correct": True, "session_deleted": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["offline", "serve", "request"])
    parser.add_argument("--hermes-root", type=Path, default=Path("/opt/hermes-agent"))
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument(
        "--gateway-url", default="http://127.0.0.1:8658/v1/chat/completions"
    )
    parser.add_argument("--fixture-url", default="http://127.0.0.1:8767/fixture-status")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if args.mode == "offline":
        print(json.dumps(offline(args.hermes_root)))
    elif args.mode == "serve":
        import uvicorn

        if not 1024 <= args.port <= 65535:
            raise ValueError("invalid fixture port")
        app = fixture_app(os.environ["ENGAGEMATE_EXECUTION_MCP_KEY"])
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
    else:
        if not 1 <= args.timeout <= 300:
            raise ValueError("fixture timeout must be 1 to 300 seconds")
        print(
            json.dumps(
                asyncio.run(
                    request_fixture(
                        args.gateway_url,
                        timeout=args.timeout,
                        fixture_url=args.fixture_url,
                    )
                )
            )
        )


if __name__ == "__main__":
    main()
