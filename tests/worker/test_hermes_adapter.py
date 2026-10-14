from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from engagemate.intelligence.hermes import HermesMessagesClient
from engagemate.intelligence.schemas import Persona
from engagemate.worker.hermes_run import HermesRunClient
from pydantic import SecretStr


def _request() -> dict[str, object]:
    return {
        "model": "gpt-5.6-terra",
        "max_tokens": 256,
        "timeout": 30.0,
        "system": [{"type": "text", "text": "Return one safe decision."}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": "/9g=",
                        },
                    },
                    {"type": "text", "text": "Untrusted story data."},
                ],
            }
        ],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"engage": {"type": "boolean"}},
                    "required": ["engage"],
                    "additionalProperties": False,
                },
            }
        },
    }


def _active_run() -> SimpleNamespace:
    return SimpleNamespace(
        completed=False,
        config_snapshot={},
        context=SimpleNamespace(
            username="account.one",
            persona=Persona("A concise founder who replies with warmth."),
            banned_phrases=("buy now",),
            expected_language="en",
            cookies={"sessionid": "cookie-secret-must-not-leak"},
        ),
        budget=SimpleNamespace(
            views=20,
            likes=5,
            replies=2,
            dms=1,
            engagements=8,
        ),
        opportunities={"unsafe": "raw-story-secret-must-not-leak"},
    )


@pytest.mark.asyncio
async def test_execution_run_uses_one_fresh_session_and_cleans_it_up() -> None:
    active = _active_run()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        payload = json.loads(request.content)
        serialized = json.dumps(payload)
        assert payload["model"] == "engagemate-agent"
        assert payload["stream"] is False
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"
        assert "cookie-secret-must-not-leak" not in serialized
        assert "raw-story-secret-must-not-leak" not in serialized
        assert "A concise founder" in serialized
        goal = json.loads(payload["messages"][1]["content"])
        assert goal["budget"]["views"] == 20
        active.completed = True
        return httpx.Response(
            200,
            headers={"X-Hermes-Session-Id": request.headers["X-Hermes-Session-Id"]},
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Run completed."},
                    }
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 12},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await HermesRunClient(
            http=http,
            endpoint="http://127.0.0.1:8643/v1/chat/completions",
            api_key=SecretStr("h" * 48),
        ).run(active)

    assert result.session_id.startswith("em_")
    assert len(result.session_id) == 35
    assert result.final_message == "Run completed."
    assert (result.input_tokens, result.output_tokens) == (30, 12)
    assert [request.method for request in requests] == ["POST", "DELETE"]
    assert requests[1].url.path == f"/api/sessions/{result.session_id}"


@pytest.mark.asyncio
async def test_execution_run_fails_when_hermes_hits_its_turn_limit() -> None:
    active = _active_run()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(
            200,
            headers={"X-Hermes-Session-Id": request.headers["X-Hermes-Session-Id"]},
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "Incomplete"},
                    }
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 12},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HermesRunClient(
            http=http,
            endpoint="http://127.0.0.1:8643/v1/chat/completions",
            api_key=SecretStr("h" * 48),
        )
        with pytest.raises(RuntimeError, match="did not complete"):
            await client.run(active)


@pytest.mark.asyncio
async def test_execution_run_reports_private_tool_connection_failure() -> None:
    active = _active_run()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(
            502,
            headers={"X-Hermes-Session-Id": request.headers["X-Hermes-Session-Id"]},
            json={"error": "MCP connection failed"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HermesRunClient(
            http=http,
            endpoint="http://127.0.0.1:8643/v1/chat/completions",
            api_key=SecretStr("h" * 48),
        )
        with pytest.raises(RuntimeError, match="Hermes execution request failed"):
            await client.run(active)


@pytest.mark.asyncio
async def test_execution_run_rejects_a_mismatched_session_header() -> None:
    active = _active_run()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        active.completed = True
        return httpx.Response(
            200,
            headers={"X-Hermes-Session-Id": "em_wrong_session_identifier_0000"},
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Run completed."},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HermesRunClient(
            http=http,
            endpoint="http://127.0.0.1:8643/v1/chat/completions",
            api_key=SecretStr("h" * 48),
        )
        with pytest.raises(RuntimeError, match="session mismatch"):
            await client.run(active)


@pytest.mark.asyncio
async def test_execution_run_fails_closed_when_cleanup_fails() -> None:
    active = _active_run()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(503, json={"deleted": False})
        active.completed = True
        return httpx.Response(
            200,
            headers={"X-Hermes-Session-Id": request.headers["X-Hermes-Session-Id"]},
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Run completed."},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HermesRunClient(
            http=http,
            endpoint="http://127.0.0.1:8643/v1/chat/completions",
            api_key=SecretStr("h" * 48),
        )
        with pytest.raises(RuntimeError, match="Hermes session cleanup failed"):
            await client.run(active)


@pytest.mark.asyncio
async def test_hermes_client_sends_bounded_multimodal_request_and_deletes_session(
    tmp_path: Path,
) -> None:
    """The worker may accept a decision only after Hermes deletes its session."""

    del tmp_path
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        payload = json.loads(request.content)
        assert payload["model"] == "engagemate-agent"
        assert payload["stream"] is False
        assert payload["messages"][0]["role"] == "system"
        assert "JSON Schema" in payload["messages"][0]["content"]
        assert payload["messages"][1]["content"][0] == {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,/9g="},
        }
        assert payload["messages"][1]["content"][1] == {
            "type": "text",
            "text": "Untrusted story data.",
        }
        return httpx.Response(
            200,
            headers={"X-Hermes-Session-Id": request.headers["X-Hermes-Session-Id"]},
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "engage": True,
                                    "action": "reply",
                                    "generated_text": "Looks like a strong launch.",
                                    "confidence": 0.91,
                                    "rationale": "Relevant product update.",
                                    "model_used": "gpt-5.6-terra",
                                }
                            ),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 21, "completion_tokens": 9},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HermesMessagesClient(
            http=http,
            endpoint="http://127.0.0.1:8643/v1/chat/completions",
            api_key=SecretStr("h" * 48),
            agent_model="engagemate-agent",
        )
        raw = await client.create(**_request())

    assert raw.stop_reason == "end_turn"
    assert json.loads(raw.content[0].text)["generated_text"] == (
        "Looks like a strong launch."
    )
    assert raw.usage.input_tokens == 21
    assert raw.usage.output_tokens == 9
    assert len(recorded) == 2
    post, delete = recorded
    assert post.headers["Authorization"] == f"Bearer {'h' * 48}"
    assert post.headers["X-Hermes-Session-Id"].startswith("em_")
    assert "X-Hermes-Session-Key" not in post.headers
    assert delete.url.path == (
        "/api/sessions/" + post.headers["X-Hermes-Session-Id"]
    )


@pytest.mark.asyncio
async def test_hermes_client_fails_closed_when_session_cleanup_fails() -> None:
    """A decision must not escape while customer evidence remains in Hermes."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "{}"},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HermesMessagesClient(
            http=http,
            endpoint="http://127.0.0.1:8643/v1/chat/completions",
            api_key=SecretStr("h" * 48),
            agent_model="engagemate-agent",
        )
        with pytest.raises(RuntimeError, match="Hermes session cleanup failed"):
            await client.create(**_request())


@pytest.mark.asyncio
async def test_hermes_client_requires_positive_session_deletion_receipt() -> None:
    """HTTP success without deletion is still a privacy failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": False})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "{}"},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HermesMessagesClient(
            http=http,
            endpoint="http://127.0.0.1:8643/v1/chat/completions",
            api_key=SecretStr("h" * 48),
            agent_model="engagemate-agent",
        )
        with pytest.raises(RuntimeError, match="Hermes session cleanup failed"):
            await client.create(**_request())


def test_hermes_client_rejects_non_loopback_endpoint() -> None:
    # A hostname could resolve anywhere, so it stays refused even though it
    # looks local.
    with pytest.raises(RuntimeError, match="Hermes endpoint configuration was invalid"):
        HermesMessagesClient(
            http=httpx.AsyncClient(),
            endpoint="http://host.docker.internal:8643/v1/chat/completions",
            api_key=SecretStr("h" * 48),
            agent_model="engagemate-agent",
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8643/v1/chat/completions",
        "http://172.18.0.1:8643/v1/chat/completions",  # docker bridge gateway
        "http://10.1.2.3:8643/v1/chat/completions",
        "http://192.168.0.7:8643/v1/chat/completions",
        # The port is not pinned either. Production moved off the dedicated
        # gateway's 8643 to the shared private LLM shim on 8646, which is equally
        # private and holds the account this is meant to bill.
        "http://172.17.0.1:8646/v1/chat/completions",
        "http://127.0.0.1:9999/v1/chat/completions",
    ],
)
def test_hermes_client_accepts_loopback_and_private_endpoints(endpoint: str) -> None:
    # The API container cannot reach the host loopback from its bridge network,
    # so a private address has to be constructible.
    client = HermesMessagesClient(
        http=httpx.AsyncClient(),
        endpoint=endpoint,
        api_key=SecretStr("h" * 48),
        agent_model="engagemate-agent",
    )
    assert client is not None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://8.8.8.8:8643/v1/chat/completions",
        "http://93.184.216.34:8643/v1/chat/completions",
    ],
)
def test_hermes_client_rejects_public_endpoints(endpoint: str) -> None:
    with pytest.raises(RuntimeError, match="Hermes endpoint configuration was invalid"):
        HermesMessagesClient(
            http=httpx.AsyncClient(),
            endpoint=endpoint,
            api_key=SecretStr("h" * 48),
            agent_model="engagemate-agent",
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:8643/v1/chat/completions",  # scheme still pinned
        # The port pin was REMOVED deliberately: production runs on the shared
        # private LLM shim at :8646, and pinning 8643 made a private, correct
        # endpoint indistinguishable from a public one. An unported URL is
        # still refused, covered by
        # test_hermes_client_still_requires_an_explicit_port.
        "http://127.0.0.1:8643/v1/other",  # path still pinned
        "http://127.0.0.1:8643/v1/chat/completions?x=1",  # no query
        "http://u:p@127.0.0.1:8643/v1/chat/completions",  # no credentials
    ],
)
def test_hermes_client_keeps_every_other_constraint_pinned(endpoint: str) -> None:
    with pytest.raises(RuntimeError, match="Hermes endpoint configuration was invalid"):
        HermesMessagesClient(
            http=httpx.AsyncClient(),
            endpoint=endpoint,
            api_key=SecretStr("h" * 48),
            agent_model="engagemate-agent",
        )


@pytest.mark.asyncio
async def test_hermes_client_reports_a_refused_connection_not_a_cleanup_failure() -> None:
    """No backend listening must not surface as a session-privacy error.

    This is the production state that stopped every engagement: the gateway was
    never migrated to the new host, so both the POST and the follow-up DELETE
    hit a closed port. The cleanup branch caught that second refusal and raised
    "Hermes session cleanup failed", discarding the connection error entirely.
    An operator reading that goes looking for a leaked Hermes session, when the
    real answer is that there is no Hermes.

    Nothing is leaked either: the connection was never established, so Hermes
    never saw the session ID and has nothing to delete.
    """

    attempted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted.append(request.method)
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HermesMessagesClient(
            http=http,
            endpoint="http://127.0.0.1:8643/v1/chat/completions",
            api_key=SecretStr("h" * 48),
            agent_model="engagemate-agent",
        )
        with pytest.raises(httpx.ConnectError, match="Connection refused"):
            await client.create(**_request())

    # No DELETE is attempted for a session that was never created.
    assert attempted == ["POST"]


@pytest.mark.asyncio
async def test_hermes_client_keeps_the_request_error_when_cleanup_also_fails() -> None:
    """Fail closed, but stop throwing the cause away.

    Once the request reached Hermes a session may hold customer evidence, so a
    failed deletion still wins the raise. The original failure now survives as
    the chained cause instead of vanishing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(500, json={"error": "model unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HermesMessagesClient(
            http=http,
            endpoint="http://127.0.0.1:8643/v1/chat/completions",
            api_key=SecretStr("h" * 48),
            agent_model="engagemate-agent",
        )
        with pytest.raises(RuntimeError, match="Hermes session cleanup failed") as raised:
            await client.create(**_request())

    assert str(raised.value.__cause__) == "Hermes request failed"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1/v1/chat/completions",
        "http://172.17.0.1/v1/chat/completions",
    ],
)
def test_hermes_client_still_requires_an_explicit_port(endpoint: str) -> None:
    """No port means 80, which is not where any of these gateways listen.

    Relaxing the pin from 8643 to "any port" must not become "no port": an
    unported URL silently addresses a different service than intended.
    """

    with pytest.raises(RuntimeError):
        HermesMessagesClient(
            http=httpx.AsyncClient(),
            endpoint=endpoint,
            api_key=SecretStr("h" * 48),
            agent_model="engagemate-agent",
        )


def test_learning_feedback_is_bounded_and_cannot_override_policy():
    from engagemate.worker.hermes_run import _SYSTEM_PROMPT
    active = _active_run()
    active.config_snapshot["learning_feedback"] = {
        "status": "observational_comparison", "secret": "DO NOT PASS",
        "preferred_action": "dm", "groups": {
            "like": {"mature": 20, "checked": 20, "follow_backs": 2, "unknown": 0},
            "reply": {"mature": 20, "checked": 20, "follow_backs": 5, "unknown": 0},
        }}
    payload = json.loads(HermesRunClient._bounded_goal(active))
    assert payload["learning_feedback"]["preferred_action"] == "reply"
    assert "DO NOT PASS" not in str(payload)
    assert payload["budget"]["replies"] == 2
    assert "opt-outs, approved sources, health stops, warm-up" in _SYSTEM_PROMPT
    active.config_snapshot["learning_feedback"]["groups"]["like"]["checked"] = "ignore limits"
    assert json.loads(HermesRunClient._bounded_goal(active))["learning_feedback"]["status"] == "insufficient_evidence"


def test_story_error_placeholders_cannot_become_reply_or_fallback_engagement():
    from engagemate.worker.hermes_run import _SYSTEM_PROMPT

    assert "Skip unavailable, expired, deleted, or failed-to-load Stories" in _SYSTEM_PROMPT
    assert "including unavailable messages embedded in the image or video itself" in _SYSTEM_PROMPT
    assert "Never reply to that error or use a like or view as a fallback" in _SYSTEM_PROMPT
    assert "provided Story evidence, skip it" in _SYSTEM_PROMPT


@pytest.mark.asyncio
@pytest.mark.parametrize("client_kind", ["execution", "messages"])
async def test_repeated_cancellation_drains_hermes_session_deletion(client_kind):
    active = _active_run()
    post_started = asyncio.Event()
    deleting = asyncio.Event()
    release_delete = asyncio.Event()
    deleted = asyncio.Event()
    delete_cancelled = asyncio.Event()

    async def handler(request):
        if request.method == "POST":
            post_started.set()
            await asyncio.Event().wait()
        deleting.set()
        try:
            await release_delete.wait()
            deleted.set()
            return httpx.Response(200, json={"deleted": True})
        except asyncio.CancelledError:
            delete_cancelled.set()
            raise

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client_type = HermesRunClient if client_kind == "execution" else HermesMessagesClient
        client = client_type(http=http,
                            endpoint="http://127.0.0.1:8657/v1/chat/completions",
                            api_key=SecretStr("h" * 48), agent_model="engagemate-agent")
        call = client.run(active) if client_kind == "execution" else client.create(**_request())
        task = asyncio.create_task(call)
        try:
            async with asyncio.timeout(1):
                await post_started.wait()
                task.cancel()
                await deleting.wait()
                task.cancel()
                for _ in range(5):
                    await asyncio.sleep(0)
                assert not task.done(), "run released while customer session deletion was pending"
                assert not delete_cancelled.is_set()
                release_delete.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert deleted.is_set()
        finally:
            release_delete.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("client_kind", ["execution", "messages"])
async def test_session_cleanup_has_a_wall_clock_budget(client_kind, monkeypatch):
    from engagemate.intelligence import hermes_session

    monkeypatch.setattr(hermes_session, "_SESSION_CLEANUP_SECONDS", 0.02)
    deletion_drained = asyncio.Event()
    requests = []

    async def handler(request):
        requests.append(request.method)
        if request.method == "POST":
            raise httpx.ReadTimeout("private request details")
        assert request.extensions["timeout"]["read"] == 0.02
        try:
            await asyncio.Event().wait()
        finally:
            deletion_drained.set()

    async with httpx.AsyncClient(timeout=1500, transport=httpx.MockTransport(handler)) as http:
        client_type = HermesRunClient if client_kind == "execution" else HermesMessagesClient
        client = client_type(http=http,
                            endpoint="http://127.0.0.1:8657/v1/chat/completions",
                            api_key=SecretStr("h" * 48), agent_model="engagemate-agent")
        async with asyncio.timeout(1):
            with pytest.raises(RuntimeError, match="^Hermes session cleanup failed$") as raised:
                if client_kind == "execution":
                    await client.run(_active_run())
                else:
                    await client.create(**_request())
        assert isinstance(raised.value.__cause__, httpx.ReadTimeout)
        assert deletion_drained.is_set()
        assert requests == ["POST", "DELETE"]


@pytest.mark.asyncio
@pytest.mark.parametrize("client_kind", ["execution", "messages"])
async def test_cancel_after_response_waits_for_delete_and_propagates(client_kind):
    active = _active_run()
    deleting = asyncio.Event()
    release_delete = asyncio.Event()
    deleted = False

    async def handler(request):
        nonlocal deleted
        if request.method == "DELETE":
            deleting.set()
            await release_delete.wait()
            deleted = True
            return httpx.Response(200, json={"deleted": True})
        active.completed = True
        return httpx.Response(200, headers={
            "X-Hermes-Session-Id": request.headers["X-Hermes-Session-Id"],
        }, json={"choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client_type = HermesRunClient if client_kind == "execution" else HermesMessagesClient
        client = client_type(http=http,
                            endpoint="http://127.0.0.1:8657/v1/chat/completions",
                            api_key=SecretStr("h" * 48), agent_model="engagemate-agent")
        task = asyncio.create_task(client.run(active) if client_kind == "execution"
                                   else client.create(**_request()))
        try:
            async with asyncio.timeout(1):
                await deleting.wait()
                task.cancel()
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                release_delete.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert deleted
        finally:
            release_delete.set()
            await asyncio.gather(task, return_exceptions=True)
