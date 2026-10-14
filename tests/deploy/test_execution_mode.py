from __future__ import annotations

from pathlib import Path

import yaml
import pytest
from pydantic import ValidationError

from engagemate.config import Settings


ROOT = Path(__file__).resolve().parents[2]


def test_execution_defaults_disabled_and_loopback_only() -> None:
    assert Settings.model_fields["instagram_execution_mode"].default == "disabled"
    assert Settings.model_fields["execution_mcp_host"].default == "127.0.0.1"
    assert Settings.model_fields["execution_mcp_port"].default == 8766


def test_execution_settings_reject_public_bind_and_weak_key() -> None:
    common = {
        "DATABASE_URL": "postgresql://localhost/test",
        "SESSION_ENCRYPTION_KEY": "s" * 43,
        "INTERNAL_API_KEY": "internal-test-key",
        "_env_file": None,
    }
    with pytest.raises(ValidationError):
        Settings(**common, EXECUTION_MCP_HOST="0.0.0.0")
    with pytest.raises(ValidationError):
        Settings(**common, ENGAGEMATE_EXECUTION_MCP_KEY="weak")


def test_worker_template_starts_disabled_with_private_mcp_and_artifacts() -> None:
    environment = (ROOT / "deploy/.env.worker.example").read_text(encoding="utf-8")

    assert "INSTAGRAM_EXECUTION_MODE=disabled" in environment
    assert "HERMES_AGENT_URL=http://127.0.0.1:8657/v1/chat/completions" in environment
    assert "EXECUTION_MCP_HOST=127.0.0.1" in environment
    assert "EXECUTION_MCP_PORT=8766" in environment
    assert "ENGAGEMATE_EXECUTION_MCP_KEY=" in environment
    assert "ARTIFACT_DIR=/srv/artifacts" in environment


def test_compose_pins_the_worker_to_one_replica() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    assert compose["services"]["worker"]["deploy"]["replicas"] == 1


