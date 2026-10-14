"""Real SDK transport preserves authentication and cancellation on restart."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

import httpx2
import pytest
import uvicorn
from sse_starlette.sse import AppStatus
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from engagemate.worker.active_run import ActiveRunRegistry
from engagemate.worker.execution_mcp_server import build_execution_mcp_app
from tests.worker.test_execution_mcp_tools import EXPECTED, KEY, _active_run


@asynccontextmanager
async def serve(registry, *, port=0):
    # These servers model separate worker processes. Uvicorn's SSE dependency
    # retains process-global shutdown state when several fixtures share Python.
    AppStatus.should_exit = False
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(128)
    listener.setblocking(False)
    address = listener.getsockname()[1]
    app = build_execution_mcp_app(registry, KEY)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=address, access_log=False,
        log_level="warning", log_config=None, timeout_graceful_shutdown=1,
    ))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("fixture server did not start")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{address}/mcp", address
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), 3)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            listener.close()


@pytest.mark.asyncio
async def test_fresh_sdk_connection_replaces_invalidated_worker_session(caplog):
    registry = ActiveRunRegistry()
    first = serve(registry)
    url, port = await first.__aenter__()
    first_open = True
    second = None
    try:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {KEY}"}) as http:
            async with streamable_http_client(url, http_client=http) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    assert {tool.name for tool in (await session.list_tools()).tools} == EXPECTED
                    # A live SDK connection holds the old session. After restart
                    # it must fail safely; the admission hook opens a fresh one.
                    await first.__aexit__(None, None, None)
                    first_open = False
                    fresh_registry = ActiveRunRegistry()
                    second = serve(fresh_registry, port=port)
                    same_url, _ = await second.__aenter__()
                    assert same_url == url
                    with pytest.raises(Exception):
                        await session.list_tools()
            async with streamable_http_client(url, http_client=http) as streams:
                async with ClientSession(*streams) as fresh:
                    await fresh.initialize()
                    assert {tool.name for tool in (await fresh.list_tools()).tools} == EXPECTED
                    denied = await fresh.call_tool("instagram.like", {
                        "opportunity_id": "11111111-1111-4111-8111-111111111111",
                        "idempotency_key": "restart_denied_0001",
                    })
                    assert denied.is_error
                    active = _active_run(mode="disabled")
                    with fresh_registry.activate(active):
                        found = await fresh.call_tool("instagram.discover", {"limit": 1})
                        assert len(found.structured_content["opportunities"]) == 1
                        assert active.executor.calls == [] and not active.completed
    finally:
        if first_open:
            await first.__aexit__(None, None, None)
        if second is not None:
            await second.__aexit__(None, None, None)
    assert "ASGI callable returned without" not in caplog.text
    assert "Cancel 0 running task" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", [None, "Bearer invalid-key"])
async def test_json_transport_preserves_auth_before_tools(auth):
    async with serve(ActiveRunRegistry()) as (url, _):
        async with httpx2.AsyncClient() as http:
            headers = {"Accept": "application/json, text/event-stream", "Mcp-Session-Id": "obsolete-session"}
            if auth is not None:
                headers["Authorization"] = auth
            response = await http.post(url, headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
            })
            assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_owner", ["sdk", "run"])
async def test_cancellation_keeps_registry_and_server_shutdown_bounded(caplog, cancel_owner):
    registry = ActiveRunRegistry()
    run = _active_run(mode="disabled")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_discovery(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    run.discovery.find = slow_discovery
    async with serve(registry) as (url, _):
        with registry.activate(run):
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {KEY}"}) as http:
                async with streamable_http_client(url, http_client=http) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        call = asyncio.create_task(session.call_tool("instagram.discover", {"limit": 1}))
                        await asyncio.wait_for(started.wait(), 2)
                        if cancel_owner == "run":
                            assert run.tool_tasks
                            await run.stop_tools()
                        call.cancel()
                        await asyncio.gather(call, return_exceptions=True)
                        await asyncio.wait_for(cancelled.wait(), 2)
                        # The SDK cancellation notification finishes the pending
                        # request asynchronously; drain a harmless request before
                        # closing the receiving session and its channel.
                        assert {tool.name for tool in (await session.list_tools()).tools} == EXPECTED
                        await asyncio.sleep(0.05)
            assert not run.completed and run.executor.calls == []
    assert registry._active is None
    assert "ASGI callable returned without" not in caplog.text
