import importlib.util
from pathlib import Path
import shutil

import pytest
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location("owner_setup", ROOT/"scripts/setup.py")
setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)

def test_setup_creates_private_distinct_keys_and_refuses_overwrite(tmp_path):
    for p in ["app/.env.local.example","deploy/.env.api.example","deploy/.env.worker.example","deploy/.env.scheduler.example","deploy/.env.web.example","deploy/.env.agent.example","deploy/hermes/engagemate-agent/.env.example"]:
        dest=tmp_path/p;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/p,dest)
    setup.generate(tmp_path,"codex")
    web=dotenv_values(tmp_path/"app/.env.local");worker=dotenv_values(tmp_path/"deploy/.env.worker");agent=dotenv_values(tmp_path/"deploy/.env.agent")
    from engagemate.security.session_cipher import SessionCipher
    from engagemate.config import Settings
    api=dotenv_values(tmp_path/"deploy/.env.api")
    SessionCipher.validate_key(api["SESSION_ENCRYPTION_KEY"])
    Settings(_env_file=None, **{k:v for k,v in api.items() if v})
    assert len(web["OWNER_ACCESS_TOKEN"]) >= 32
    assert len({web["OWNER_ACCESS_TOKEN"],web["INTERNAL_API_KEY"],worker["HERMES_AGENT_API_KEY"],worker["ENGAGEMATE_EXECUTION_MCP_KEY"]})==4
    assert agent["HERMES_AGENT_API_KEY"]==worker["HERMES_AGENT_API_KEY"]
    assert worker["INSTAGRAM_EXECUTION_MODE"]=="disabled"
    assert "CLERK_SECRET_KEY" not in web and "STRIPE_SECRET_KEY" not in web
    assert (tmp_path/"app/.env.local").stat().st_mode & 0o777 == 0o600
    before=(tmp_path/"app/.env.local").read_bytes()
    with pytest.raises(ValueError,match="Configuration exists"):setup.generate(tmp_path,"hermes")
    assert (tmp_path/"app/.env.local").read_bytes()==before
