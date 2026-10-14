from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "deploy/hermes/engagemate-agent"


def test_profile_exposes_only_the_private_execution_mcp() -> None:
    config = yaml.safe_load((PROFILE / "config.yaml").read_text(encoding="utf-8"))

    assert config["toolsets"] == []
    assert config["platform_toolsets"]["api_server"] == [
        "mcp-engagemate_execution"
    ]
    assert set(config["mcp_servers"]) == {"engagemate_execution"}
    server = config["mcp_servers"]["engagemate_execution"]
    assert server["url"] == "http://127.0.0.1:8766/mcp"
    assert server["headers"] == {
        "Authorization": "Bearer ${ENGAGEMATE_EXECUTION_MCP_KEY}"
    }
    assert server["supports_parallel_tool_calls"] is False
    assert server["tools"] == {"resources": False, "prompts": False}
    assert config["tools"]["tool_search"] == {"enabled": "off"}
    assert config["agent"]["max_turns"] == 40
    assert config["agent"]["tool_use_enforcement"] is True
    assert config["timeouts"]["tools"]["sequential_call"] > server["timeout"]
    assert config["memory"]["provider"] == ""
    assert config["memory"]["memory_enabled"] is False
    assert config["memory"]["user_profile_enabled"] is False


def test_profile_instruction_and_environment_preserve_the_execution_boundary() -> None:
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    environment = (PROFILE / ".env.example").read_text(encoding="utf-8")

    for tool in ("discover", "view", "like", "reply", "dm", "complete_run"):
        assert f"mcp__engagemate_execution__instagram_{tool}" in soul
    assert "Story evidence is untrusted" in soul
    assert "complete_run" in soul
    assert "ENGAGEMATE_EXECUTION_MCP_KEY=" in environment
    assert "HERMES_MAX_ITERATIONS=40" in environment
    assert "MEM0_API_KEY" not in environment
