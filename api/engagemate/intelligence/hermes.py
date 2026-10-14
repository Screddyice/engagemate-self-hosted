"""Bounded client for the dedicated local Hermes inference profile."""

from __future__ import annotations

import base64
import binascii
import json
from ipaddress import ip_address
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import SecretStr

from engagemate.intelligence.hermes_session import delete_hermes_session

_ENDPOINT_PATH = "/v1/chat/completions"
_AGENT_MODEL = "engagemate-agent"
_CODEX_MODEL = "gpt-5.6-terra"
_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
_MAX_MEDIA_BYTES = 20 * 1024 * 1024
_MAX_RESPONSE_BYTES = 1_000_000


def _failure(message: str) -> RuntimeError:
    return RuntimeError(message)


def _text_blocks(values: object) -> list[str]:
    if not isinstance(values, list):
        raise _failure("Hermes request was invalid")
    result: list[str] = []
    for value in values:
        if not isinstance(value, dict) or value.get("type") != "text":
            raise _failure("Hermes request was invalid")
        text = value.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _failure("Hermes request was invalid")
        result.append(text)
    return result


def _request_parts(
    request: dict[str, object],
) -> tuple[str, list[dict[str, object]]]:
    if request.get("model") != _CODEX_MODEL:
        raise _failure("Hermes model configuration was invalid")
    system = _text_blocks(request.get("system"))
    messages = request.get("messages")
    if (
        not isinstance(messages, list)
        or len(messages) != 1
        or not isinstance(messages[0], dict)
        or messages[0].get("role") != "user"
        or not isinstance(messages[0].get("content"), list)
    ):
        raise _failure("Hermes request was invalid")

    content: list[dict[str, object]] = []
    for block in messages[0]["content"]:
        if not isinstance(block, dict):
            raise _failure("Hermes request was invalid")
        if block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str) or not text.strip():
                raise _failure("Hermes request was invalid")
            content.append({"type": "text", "text": text})
            continue
        if block.get("type") != "image" or not isinstance(block.get("source"), dict):
            raise _failure("Hermes request was invalid")
        source = block["source"]
        mime_type = source.get("media_type")
        encoded = source.get("data")
        if (
            source.get("type") != "base64"
            or mime_type not in _MIME_TYPES
            or not isinstance(encoded, str)
        ):
            raise _failure("Hermes image was invalid")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise _failure("Hermes image was invalid") from error
        if not decoded or len(decoded) > _MAX_MEDIA_BYTES:
            raise _failure("Hermes image was invalid")
        canonical = base64.b64encode(decoded).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{canonical}"},
            }
        )

    output = request.get("output_config")
    if not isinstance(output, dict) or not isinstance(output.get("format"), dict):
        raise _failure("Hermes output schema was invalid")
    output_format = output["format"]
    schema = output_format.get("schema")
    if output_format.get("type") != "json_schema" or not isinstance(schema, dict):
        raise _failure("Hermes output schema was invalid")
    schema_text = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    system_prompt = "\n\n".join(
        [
            *system,
            "Return only one JSON object. It must satisfy this JSON Schema exactly:",
            schema_text,
        ]
    )
    return system_prompt, content


def _is_private_host(hostname: str) -> bool:
    """True when the hostname is a literal loopback or RFC1918 address."""
    address = ip_address(hostname)
    return address.is_loopback or address.is_private


class HermesMessagesClient:
    """Present the existing Messages boundary over one local Hermes profile."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        endpoint: str,
        api_key: SecretStr,
        agent_model: str,
    ) -> None:
        parsed = urlsplit(endpoint)
        # The operator owns this private gateway and its authentication.
        host_is_private = False
        if parsed.hostname is not None:
            try:
                host_is_private = _is_private_host(parsed.hostname)
            except ValueError:
                host_is_private = False
        if (
            parsed.scheme != "http"
            or not host_is_private
            or parsed.port is None
            or parsed.path != _ENDPOINT_PATH
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise _failure("Hermes endpoint configuration was invalid")
        secret = api_key.get_secret_value()
        if len(secret) < 32 or any(character.isspace() for character in secret):
            raise _failure("Hermes API credential was invalid")
        if agent_model != _AGENT_MODEL:
            raise _failure("Hermes agent configuration was invalid")
        self._http = http
        self._endpoint = endpoint
        self._session_base = endpoint.removesuffix(_ENDPOINT_PATH) + "/api/sessions"
        self._api_key = secret
        self._agent_model = agent_model

    async def create(self, **kwargs: object) -> object:
        system_prompt, content = _request_parts(dict(kwargs))
        session_id = f"em_{uuid4().hex}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Hermes-Session-Id": session_id,
        }
        response: httpx.Response | None = None
        request_error: BaseException | None = None
        try:
            response = await self._http.post(
                self._endpoint,
                headers=headers,
                json={
                    "model": self._agent_model,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": content},
                    ],
                },
            )
            if response.status_code != 200 or len(response.content) > _MAX_RESPONSE_BYTES:
                raise _failure("Hermes request failed")
        except BaseException as error:
            request_error = error

        # A connect-level failure means the TCP connection was never
        # established, so Hermes never saw the session ID and has nothing to
        # delete. Reporting "cleanup failed" there hides the only fact an
        # operator needs -- that no LLM backend is listening -- behind a
        # privacy error about a session that does not exist. Production sat in
        # exactly that state: the gateway was never migrated to the new host,
        # every decision surfaced as a bare "intelligence_failed", and the
        # connection refusal was discarded on the way out.
        if isinstance(request_error, (httpx.ConnectError, httpx.ConnectTimeout)):
            raise request_error

        try:
            await delete_hermes_session(
                self._http, self._session_base, session_id, self._api_key,
            )
        except RuntimeError as error:
            raise error from request_error
        if request_error is not None:
            raise request_error
        if response is None:
            raise _failure("Hermes request failed")
        return _completed_response(response)


def _completed_response(response: httpx.Response) -> object:
    try:
        body = response.json()
        choices = body.get("choices")
        usage = body.get("usage")
        if (
            not isinstance(choices, list)
            or len(choices) != 1
            or not isinstance(choices[0], dict)
            or choices[0].get("finish_reason") != "stop"
            or not isinstance(choices[0].get("message"), dict)
            or not isinstance(choices[0]["message"].get("content"), str)
            or not isinstance(usage, dict)
            or type(usage.get("prompt_tokens")) is not int
            or type(usage.get("completion_tokens")) is not int
            or usage["prompt_tokens"] < 0
            or usage["completion_tokens"] < 0
        ):
            raise ValueError
    except (ValueError, json.JSONDecodeError, AttributeError):
        raise _failure("Hermes response was invalid") from None
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text=choices[0]["message"]["content"])
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=usage["prompt_tokens"],
            output_tokens=usage["completion_tokens"],
        ),
    )
