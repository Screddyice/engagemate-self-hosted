"""Exercise the owner adapter with a disposable stock-CLI protocol fixture."""
import asyncio
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

from engagemate.intelligence import codex_gateway as gateway

@pytest.fixture
def client(monkeypatch,tmp_path):
    monkeypatch.setenv("HERMES_AGENT_API_KEY","h"*48)
    monkeypatch.setenv("ENGAGEMATE_EXECUTION_MCP_KEY","e"*48)
    program=tmp_path/"codex-fixture"
    program.write_text(f'''#!{sys.executable}
import sys,json
assert "--ephemeral" in sys.argv and "--ignore-user-config" in sys.argv
assert "read-only" in sys.argv and "features.shell_tool=false" in sys.argv
assert len(sys.stdin.read()) > 0
print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":"fixture completed"}}}}))
print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":5,"output_tokens":2}}}}))
''')
    program.chmod(0o700);monkeypatch.setenv("CODEX_BIN",str(program))
    return TestClient(gateway.app)

def headers(): return {"Authorization":"Bearer "+"h"*48,"X-Hermes-Session-Id":"em_"+"a"*32}

def test_gateway_rejects_untrusted_and_malformed_requests(client):
    assert client.get("/health").status_code==401
    assert client.post("/v1/chat/completions",headers=headers(),json={"model":"other"}).status_code==400
    assert client.get("/health",headers=headers()).json()=={"runtime":"codex","ephemeral":True}

def test_completion_echoes_worker_session_and_deletion_is_authenticated(client):
    response=client.post("/v1/chat/completions",headers=headers(),json={"model":"engagemate-agent","stream":False,"messages":[{"role":"system","content":"Run one scheduled EngageMate Instagram session. Synthetic fixture only."}]})
    assert response.status_code==200,response.text
    assert response.headers["X-Hermes-Session-Id"]==headers()["X-Hermes-Session-Id"]
    assert response.json()["usage"]=={"prompt_tokens":5,"completion_tokens":2}
    assert client.delete("/api/sessions/"+headers()["X-Hermes-Session-Id"]).status_code==401
    assert client.delete("/api/sessions/"+headers()["X-Hermes-Session-Id"],headers=headers()).json()=={"deleted":True}

def test_only_execution_token_reaches_scheduled_process(monkeypatch,tmp_path):
    monkeypatch.setenv("ENGAGEMATE_EXECUTION_MCP_KEY","e"*48)
    monkeypatch.setenv("INTERNAL_API_KEY","should-not-reach-agent")
    args,env=gateway.command(scheduled=True,directory=tmp_path)
    assert "INTERNAL_API_KEY" not in env
    assert "e"*48 not in " ".join(args)
    assert env["ENGAGEMATE_EXECUTION_MCP_KEY"]=="e"*48
    assert 'mcp_servers.engagemate_execution.required=true' in args
    assert 'mcp_servers.engagemate_execution.default_tools_approval_mode="approve"' in args
    assert any('enabled_tools=' in a and 'instagram.complete_run' in a for a in args)
    args,env=gateway.command(scheduled=False,directory=tmp_path)
    assert not any("mcp_servers" in a for a in args)
    assert "ENGAGEMATE_EXECUTION_MCP_KEY" not in env

@pytest.mark.asyncio
async def test_timeout_stops_process_without_replaying_request(monkeypatch,tmp_path):
    program=tmp_path/"blocked";program.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n");program.chmod(0o700)
    monkeypatch.setenv("CODEX_BIN",str(program));monkeypatch.setenv("CODEX_TIMEOUT_SECONDS","1")
    with pytest.raises(asyncio.TimeoutError):
        await gateway.run_codex({"model":"engagemate-agent","stream":False,"messages":[{"role":"user","content":"synthetic"}]})

@pytest.mark.asyncio
async def test_delete_waits_for_active_run_cleanup(monkeypatch):
    import httpx
    monkeypatch.setenv("HERMES_AGENT_API_KEY","h"*48)
    monkeypatch.setattr(gateway,"_SERIAL",asyncio.Lock())
    entered,cleaned=asyncio.Event(),asyncio.Event()
    async def blocked(_):
        entered.set()
        try: await asyncio.sleep(30)
        finally: cleaned.set()
    monkeypatch.setattr(gateway,"run_codex",blocked)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url="http://fixture") as client:
        pending=asyncio.create_task(client.post("/v1/chat/completions",headers=headers(),json={}))
        await entered.wait()
        deleted=await client.delete("/api/sessions/"+headers()["X-Hermes-Session-Id"],headers=headers())
        assert deleted.json()=={"deleted":True} and cleaned.is_set()
        assert (await pending).status_code==499
        assert headers()["X-Hermes-Session-Id"] not in gateway._RUNS
