"""Readiness reconnects tools before model admission and fails closed."""

from __future__ import annotations

import copy
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from tests.worker.test_hermes_story_vision_plugin import PLUGIN, plugin

SESSION = "em_0123456789abcdefghijklmnopqrstuv"
KEY = "T9x_2AbCdEfGhJkLmNpQrStUvWxYz0123456789-"
CONFIG = yaml.safe_load((PLUGIN.parent.parent / "config.yaml").read_text())


class Native:
    """Controlled native boundary; no tool invocation or model provider exists."""

    def __init__(self, *, cached=False, connected=True, names=None):
        self._lock = threading.Lock()
        self._servers = {plugin._SERVER: object()} if cached else {}
        self.connected = connected
        self.names = set(plugin._EXECUTION_TOOLS if names is None else names)
        self.events = []

    def register_mcp_servers(self, servers):
        assert set(servers) == {plugin._SERVER}
        assert servers[plugin._SERVER]["headers"] == {"Authorization": f"Bearer {KEY}"}
        self.events.append("register")

    def _signal_reconnect_and_wait(self, name, server, **kwargs):
        assert name == plugin._SERVER and server is self._servers[name]
        assert 0 <= kwargs["timeout"] <= plugin._ADMISSION_SECONDS
        self.events.append("fresh_session")
        return self.connected

    def get_mcp_status(self):
        return [{"name": plugin._SERVER, "connected": self.connected, "tools": len(self.names)}]

    def refresh_agent_mcp_tools(self, agent, **kwargs):
        assert kwargs == {
            "enabled_override": ["mcp-engagemate_execution"],
            "disabled_override": [], "quiet_mode": True,
        }
        self.events.append("refresh")
        agent.enabled_toolsets = kwargs["enabled_override"]
        agent.disabled_toolsets = kwargs["disabled_override"]
        agent.tools = []
        for name in self.names:
            expected = plugin._EXECUTION_REQUIRED_ARGS[name]
            parameters = {
                "type": "object",
                "properties": {argument: {"type": "string"} for argument in expected},
            }
            if expected:
                parameters["required"] = sorted(expected)
            agent.tools.append({"function": {"name": name, "parameters": parameters}})
        agent.valid_tool_names = set(self.names)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("ENGAGEMATE_EXECUTION_MCP_KEY", KEY)
    monkeypatch.setattr(plugin, "_ADMISSION_SECONDS", 0.08)
    interrupts = []
    agent = SimpleNamespace(
        session_id=SESSION, tools=[], valid_tool_names=set(), enabled_toolsets=[],
        interrupt=lambda *args, **kwargs: interrupts.append((args, kwargs)),
    )
    parent = ModuleType("agent.subagent_lifecycle")
    parent.get_active_subagent_parent = lambda: agent
    config = ModuleType("hermes_cli.config")
    config.load_config = lambda: copy.deepcopy(CONFIG)
    native = Native()
    tools = ModuleType("tools")
    tools.mcp_tool = native
    monkeypatch.setitem(sys.modules, "agent.subagent_lifecycle", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setitem(sys.modules, "tools", tools)
    return SimpleNamespace(agent=agent, interrupts=interrupts, native=native, config=config, tools=tools)


@pytest.mark.parametrize("cached", [False, True])
def test_gateway_cold_start_and_worker_restart_refresh_before_model(world, cached):
    world.native._servers = {plugin._SERVER: object()} if cached else {}
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert world.agent._engagemate_execution_admission is True
    assert world.agent.valid_tool_names == plugin._EXECUTION_TOOLS
    assert world.interrupts == []
    assert world.native.events == (["register", "fresh_session", "refresh"] if cached else ["register", "refresh"])
    # Both native hooks fire on a fresh session; admission must run only once.
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert world.native.events.count("register") == 1


@pytest.mark.parametrize("failure", ["offline", "register", "status", "refresh", "missing", "extra", "valid_names", "timeout"])
def test_readiness_failures_hard_cancel_with_zero_model_or_action_calls(world, failure):
    def error(*args, **kwargs):
        raise RuntimeError("private exception must not escape")

    if failure == "offline":
        world.native.connected = False
    elif failure in {"register", "status", "refresh"}:
        method = {"register": "register_mcp_servers", "status": "get_mcp_status", "refresh": "refresh_agent_mcp_tools"}[failure]
        setattr(world.native, method, error)
    elif failure == "missing":
        world.native.names.remove(next(iter(world.native.names)))
    elif failure == "extra":
        world.native.names.add("mcp__another_company__send")
    elif failure == "valid_names":
        original = world.native.refresh_agent_mcp_tools
        def mismatch(agent, **kwargs):
            original(agent, **kwargs)
            agent.valid_tool_names = set()
        world.native.refresh_agent_mcp_tools = mismatch
    else:
        world.native.register_mcp_servers = lambda _: time.sleep(0.15)
    started = time.monotonic()
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert time.monotonic() - started < 0.5
    assert world.agent._engagemate_execution_admission is False
    assert world.interrupts == [((plugin._READY_ERROR,), {"hard_cancel": True})]
    # A second native hook cannot retry admission or start a provider turn.
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert len(world.interrupts) == 2


@pytest.mark.parametrize("fault", ["other_server", "url", "headers", "parallel", "disabled", "lazy", "command", "key", "selection", "search", "utility", "disabled_toolset"])
def test_invalid_profile_never_connects_to_any_server(world, monkeypatch, fault):
    config = copy.deepcopy(CONFIG)
    server = config["mcp_servers"][plugin._SERVER]
    if fault == "other_server":
        config["mcp_servers"]["other_server"] = {"url": "https://example.test/mcp"}
    elif fault == "url":
        server["url"] = "http://127.0.0.1:8766/mcp?alternate=1"
    elif fault == "headers":
        server["headers"] = {"Authorization": "Bearer another-key"}
    elif fault == "parallel":
        server["supports_parallel_tool_calls"] = True
    elif fault in {"disabled", "lazy"}:
        server["enabled" if fault == "disabled" else "lazy"] = fault == "lazy"
    elif fault == "command":
        server["command"] = "foreign-process"
    elif fault == "selection":
        config["platform_toolsets"]["api_server"].append("terminal")
    elif fault == "search":
        config["tools"]["tool_search"]["enabled"] = "auto"
    elif fault == "utility":
        server["tools"]["resources"] = True
    elif fault == "disabled_toolset":
        config["agent"]["disabled_toolsets"] = ["mcp-engagemate_execution"]
    else:
        monkeypatch.delenv("ENGAGEMATE_EXECUTION_MCP_KEY")
    world.config.load_config = lambda: config
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert world.native.events == []
    assert world.interrupts == [((plugin._READY_ERROR,), {"hard_cancel": True})]


@pytest.mark.parametrize("stale_selection", [[], None, ["terminal"], ["mcp-other_server"]])
def test_validated_profile_replaces_stale_agent_selection(world, stale_selection):
    world.agent.enabled_toolsets = stale_selection
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert world.agent.enabled_toolsets == ["mcp-engagemate_execution"]
    assert world.agent.disabled_toolsets == []
    assert world.agent.valid_tool_names == plugin._EXECUTION_TOOLS
    assert world.interrupts == []


@pytest.mark.parametrize("session_id,platform", [("other-session", "api_server"), (SESSION, "telegram"), (SESSION, "")])
def test_other_sessions_and_platforms_are_untouched(world, session_id, platform):
    plugin._admit_execution(session_id=session_id, platform=platform)
    assert world.native.events == [] and world.interrupts == []


def test_mismatched_bound_agent_is_stopped_without_connecting(world):
    world.agent.session_id = "em_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert world.native.events == []
    assert world.interrupts == [((plugin._READY_ERROR,), {"hard_cancel": True})]


def test_native_expanded_header_is_the_same_authenticated_endpoint(world):
    expanded = copy.deepcopy(CONFIG)
    expanded["mcp_servers"][plugin._SERVER]["headers"] = {"Authorization": f"Bearer {KEY}"}
    # Native load_config merges extra default fields into tools.tool_search.
    expanded["tools"]["tool_search"].update(threshold_pct=5, listing_max_tokens=10000)
    world.config.load_config = lambda: expanded
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert world.agent._engagemate_execution_admission is True
    assert world.interrupts == []


def test_admission_diagnostics_do_not_include_native_exception_or_key(world, caplog):
    def error(*args, **kwargs):
        raise RuntimeError(KEY + " private provider response")
    world.native.refresh_agent_mcp_tools = error
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert "stage=admission_exception" in caplog.text
    assert KEY not in caplog.text and "private provider response" not in caplog.text
    assert world.interrupts == [((plugin._READY_ERROR,), {"hard_cancel": True})]


@pytest.mark.parametrize(
    "mutation",
    ["missing_parameters", "missing_reply_argument", "extra_required_argument"],
)
def test_invalid_execution_schema_hard_cancels_before_model(world, mutation):
    original = world.native.refresh_agent_mcp_tools

    def malformed(agent, **kwargs):
        original(agent, **kwargs)
        reply = next(
            tool for tool in agent.tools
            if tool["function"]["name"].endswith("instagram_reply")
        )
        if mutation == "missing_parameters":
            reply["function"].pop("parameters")
        elif mutation == "missing_reply_argument":
            reply["function"]["parameters"]["required"].remove("generated_text")
            reply["function"]["parameters"]["properties"].pop("generated_text")
        else:
            reply["function"]["parameters"]["required"].append("unexpected")

    world.native.refresh_agent_mcp_tools = malformed
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert world.agent._engagemate_execution_admission is False
    assert world.interrupts == [((plugin._READY_ERROR,), {"hard_cancel": True})]


def test_admission_clears_only_execution_schema_cache(world, monkeypatch):
    cleared = []
    cache = ModuleType("tools.mcp_schema_cache")
    cache.clear_cache_entry = cleared.append
    world.tools.mcp_schema_cache = cache
    monkeypatch.setitem(sys.modules, "tools.mcp_schema_cache", cache)
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert cleared == [plugin._SERVER]


@pytest.mark.parametrize("stage", ["start", "join"])
def test_thread_lifecycle_errors_cannot_fail_open(world, monkeypatch, stage):
    def error(*args, **kwargs):
        raise RuntimeError("thread lifecycle failure")
    monkeypatch.setattr(threading.Thread, stage, error)
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert world.agent._engagemate_execution_admission is False
    assert world.interrupts == [((plugin._READY_ERROR,), {"hard_cancel": True})]


def test_false_marker_reasserts_native_interrupt_without_retry(world):
    world.agent._engagemate_execution_admission = False
    plugin._admit_execution(session_id=SESSION, platform="api_server")
    assert world.native.events == []
    assert world.interrupts == [((plugin._READY_ERROR,), {"hard_cancel": True})]
