"""Owner-run, loopback-only adapter from the execution protocol to stock Codex CLI.

No hosted account, custom cloud shim, shared profile, or persistent conversation.
Run this under the OS user who signed in with `codex login`.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
from pathlib import Path
import re
import signal
import tempfile

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv("deploy/.env.agent", override=False)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_SERIAL = asyncio.Lock()
_RUNS: dict[str, asyncio.Task] = {}
_SESSION = re.compile(r"^em_[A-Za-z0-9_-]{16,64}$")
_ALLOWED_IMAGES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def authorize(value: str | None) -> None:
    key = os.environ.get("HERMES_AGENT_API_KEY", "")
    if len(key) < 32 or not hmac.compare_digest(value or "", f"Bearer {key}"):
        raise HTTPException(401, "Unauthorized")


def command(*, scheduled: bool, directory: Path) -> tuple[list[str], dict[str, str]]:
    argv = [os.environ.get("CODEX_BIN", "codex"), "exec", "--ignore-user-config",
            "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
            "--json", "--color", "never", "-C", str(directory),
            "-c", "features.shell_tool=false", "-c", 'web_search="disabled"']
    if model := os.environ.get("CODEX_MODEL", "").strip():
        argv += ["--model", model]
    # Keep the owner's auth runtime, discard other application/provider keys.
    env = {k: v for k, v in os.environ.items() if k in
           {"PATH", "HOME", "USER", "LOGNAME", "SYSTEMROOT", "TMPDIR", "CODEX_HOME", "OPENAI_API_KEY"}}
    if scheduled:
        key = os.environ.get("ENGAGEMATE_EXECUTION_MCP_KEY", "")
        port = int(os.environ.get("EXECUTION_MCP_PORT", "8766"))
        if len(key) < 32 or not 1024 <= port <= 65535:
            raise ValueError("Execution MCP is not configured")
        env["ENGAGEMATE_EXECUTION_MCP_KEY"] = key
        argv += ["-c", f'mcp_servers.engagemate_execution.url="http://127.0.0.1:{port}/mcp"',
                 "-c", 'mcp_servers.engagemate_execution.bearer_token_env_var="ENGAGEMATE_EXECUTION_MCP_KEY"',
                 "-c", "mcp_servers.engagemate_execution.required=true",
                 "-c", 'mcp_servers.engagemate_execution.default_tools_approval_mode="approve"',
                 "-c", 'mcp_servers.engagemate_execution.enabled_tools=["instagram.discover","instagram.view","instagram.like","instagram.reply","instagram.complete_run"]',
                 "-c", "mcp_servers.engagemate_execution.tool_timeout_sec=1320"]
    return argv, env


def prompt_parts(body: object, directory: Path) -> tuple[str, list[str], bool]:
    if not isinstance(body, dict) or body.get("model") != "engagemate-agent" or body.get("stream") is not False:
        raise ValueError("Invalid request")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 20:
        raise ValueError("Invalid messages")
    text, images = [], []
    scheduled = False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
            raise ValueError("Invalid message")
        content = message.get("content")
        if isinstance(content, str):
            scheduled |= message["role"] == "system" and content.startswith("Run one scheduled EngageMate Instagram session.")
            text.append(f"{message['role']}:\n{content}")
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict): raise ValueError("Invalid content")
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    text.append(part["text"])
                elif part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=]+)", url)
                    if not match or len(images) >= 4: raise ValueError("Invalid image")
                    image = base64.b64decode(match[2], validate=True)
                    if not 0 < len(image) <= 20 * 1024 * 1024: raise ValueError("Invalid image")
                    path = directory / f"story-{len(images)}{_ALLOWED_IMAGES[match[1]]}"
                    path.write_bytes(image); path.chmod(0o600)
                    images.append(str(path))
                else: raise ValueError("Invalid content")
        else: raise ValueError("Invalid content")
    prompt = "\n\n".join(text)
    if not prompt or len(prompt) > 100_000: raise ValueError("Invalid prompt")
    return prompt, images, scheduled


async def run_codex(body: object) -> dict:
    with tempfile.TemporaryDirectory(prefix="engagemate-agent-") as tmp:
        directory = Path(tmp)
        prompt, images, scheduled = prompt_parts(body, directory)
        if scheduled:
            prompt = ("Use native MCP tool discovery if needed to load engagemate_execution. "
                      "Only its five account-scoped execution tools may perform actions. "
                      "The worker enforces the active run and all policy denials.\n\n" + prompt)
        argv, env = command(scheduled=scheduled, directory=directory)
        for path in images: argv += ["--image", path]
        argv += ["-"]  # stdin keeps prompts out of process listings
        proc = await asyncio.create_subprocess_exec(*argv, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            timeout = float(os.environ.get("CODEX_TIMEOUT_SECONDS", "1800"))
            if not 1 <= timeout <= 3600: raise ValueError("Invalid timeout")
            output, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
            if proc.returncode != 0 or len(output) > 5_000_000: raise RuntimeError("Codex run failed")
            final, usage, complete = None, None, False
            for line in output.splitlines():
                event = json.loads(line)
                if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                    final = event["item"].get("text")
                if event.get("type") == "turn.completed":
                    usage = event.get("usage"); complete = True
                if event.get("type") in {"turn.failed", "error"}: raise RuntimeError("Codex run failed")
            if not complete or not isinstance(final, str) or not final.strip() or not isinstance(usage, dict):
                raise RuntimeError("Codex response invalid")
            input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
            if any(type(n) is not int or n < 0 for n in [input_tokens, output_tokens]): raise RuntimeError("Codex usage invalid")
            return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": final}}],
                    "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens}}
        finally:
            if proc.returncode is None:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                await proc.wait()


@app.get("/health")
async def health(authorization: str | None = Header(default=None)):
    authorize(authorization)
    return {"runtime": "codex", "ephemeral": True}


@app.post("/v1/chat/completions")
async def completion(request: Request, authorization: str | None = Header(default=None),
                     x_hermes_session_id: str | None = Header(default=None)):
    authorize(authorization)
    if not x_hermes_session_id or not _SESSION.fullmatch(x_hermes_session_id): raise HTTPException(400, "Invalid session")
    # Bound body size before parsing (images have a separate bound).
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 30_000_000: raise HTTPException(413, "Request too large")
    if x_hermes_session_id in _RUNS: raise HTTPException(409, "Session already active")
    try:
        body = json.loads(data)
    except ValueError: raise HTTPException(400, "Invalid request") from None
    async def serialized():
        async with _SERIAL: return await run_codex(body)
    task = asyncio.create_task(serialized())
    _RUNS[x_hermes_session_id] = task
    try:
        result = await task
    except (ValueError, TypeError, KeyError): raise HTTPException(400, "Invalid request") from None
    except (RuntimeError, OSError, asyncio.TimeoutError): raise HTTPException(502, "Owner runtime failed") from None
    except asyncio.CancelledError: raise HTTPException(499, "Owner run cancelled") from None
    finally:
        if _RUNS.get(x_hermes_session_id) is task: _RUNS.pop(x_hermes_session_id, None)
    return JSONResponse(result, headers={"X-Hermes-Session-Id": x_hermes_session_id})


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, authorization: str | None = Header(default=None)):
    authorize(authorization)
    if not _SESSION.fullmatch(session_id): raise HTTPException(400, "Invalid session")
    task = _RUNS.get(session_id)
    if task is not None:
        task.cancel()
        try: await task
        except (asyncio.CancelledError, RuntimeError, OSError, ValueError): pass
    # A deletion only succeeds after active subprocess cleanup has finished.
    return {"deleted": True}
