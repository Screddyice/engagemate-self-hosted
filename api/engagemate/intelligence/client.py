"""Provider-neutral story intelligence with deterministic model routing."""

from __future__ import annotations

import base64
import json
import logging
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel

from engagemate.config import Settings
from engagemate.intelligence.prompts import (
    FULL_INSTRUCTIONS,
    HAIKU_INSTRUCTIONS,
    SYSTEM_INSTRUCTIONS,
    untrusted_envelope,
)
from engagemate.intelligence.schemas import (
    EngagementDecision,
    IntelligenceHistory,
    ModelUsage,
    Persona,
    WorthReadingDecision,
)
from engagemate.worker.story_types import (
    StoryContent,
    StoryMedia,
    StoryMediaType,
    StoryOpportunity,
)

_ALLOWED_IMAGE_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
_ALLOWED_TIERS = frozenset({"vip", "broad", "discovery"})
_MAX_STORY_TEXT = 4_000
_MAX_MEDIA_BYTES = 20 * 1024 * 1024
_REQUEST_TIMEOUT_SECONDS = 30.0
_IDENTIFIER = re.compile(r"^[^\s\x00-\x1f\x7f]{1,256}$")
_HANDLE = re.compile(r"^[a-z0-9_](?:[a-z0-9._]{0,28}[a-z0-9_])?$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_HAIKU_MODEL = "claude-haiku-4-5"
# Every reply is written by the strongest model. Nothing steps down a tier.
_BEST_MODEL = "claude-opus-4-8"
_CODEX_MODEL = "gpt-5.6-terra"

# Decisions carry customer evidence, so nothing from a prompt, a response, or
# an exception message is ever logged -- only the exception's class name, which
# is what separates "no backend is listening" (ConnectError) from "the key was
# refused" (HTTPStatusError) from "the model answered off-schema"
# (ValidationError). Without it every one of those looks identical downstream:
# the runner records the single word "intelligence_failed" and stops.
_LOGGER = logging.getLogger(__name__)


class IntelligenceError(RuntimeError):
    """Bounded error safe for persistence and logs."""


class MessagesClient(Protocol):
    async def create(self, **kwargs: object) -> object: ...


DecisionModel = TypeVar("DecisionModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class _CallResult:
    value: BaseModel | None
    usage: ModelUsage | None = None


@dataclass(frozen=True, slots=True)
class _ValidatedInputs:
    opportunity: StoryOpportunity
    persona: Persona
    history: IntelligenceHistory
    media: tuple[StoryMedia, ...]


def _raise_failure(message: str) -> None:
    raise IntelligenceError(message) from None


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _safe_text(value: object, maximum: int, *, allow_empty: bool = True) -> str:
    if (
        type(value) is not str
        or len(value) > maximum
        or (not allow_empty and not value.strip())
        or _contains_control(value)
    ):
        raise ValueError
    return value


def _copied_media(media: object) -> StoryMedia:
    if (
        type(media) is not StoryMedia
        or type(media.mime_type) is not str
        or media.mime_type not in _ALLOWED_IMAGE_TYPES
        or type(media.data) is not bytes
        or not media.data
        or len(media.data) > _MAX_MEDIA_BYTES
    ):
        raise ValueError
    return StoryMedia(
        mime_type=media.mime_type,
        data=memoryview(media.data).tobytes(),
    )


def _copied_content(value: object) -> tuple[StoryContent, tuple[StoryMedia, ...]]:
    if (
        type(value) is not StoryContent
        or (
            value.caption_text is not None
            and type(value.caption_text) is not str
        )
        or (
            value.sticker_text is not None
            and type(value.sticker_text) is not str
        )
    ):
        raise ValueError
    for text in (value.caption_text, value.sticker_text):
        if text is not None:
            _safe_text(text, _MAX_STORY_TEXT)
    if value.media_type is StoryMediaType.IMAGE:
        if type(value.video_frames) is not tuple or value.video_frames:
            raise ValueError
        if value.image is None:
            raise ValueError
        image = _copied_media(value.image)
        media = (image,)
        content = StoryContent(
            media_type=StoryMediaType.IMAGE,
            caption_text=value.caption_text,
            sticker_text=value.sticker_text,
            image=image,
            video_frames=(),
        )
    elif value.media_type is StoryMediaType.VIDEO:
        if (
            value.image is not None
            or type(value.video_frames) is not tuple
            or not 1 <= len(value.video_frames) <= 4
        ):
            raise ValueError
        media = tuple(_copied_media(frame) for frame in value.video_frames)
        content = StoryContent(
            media_type=StoryMediaType.VIDEO,
            caption_text=value.caption_text,
            sticker_text=value.sticker_text,
            image=None,
            video_frames=media,
        )
    else:
        raise ValueError
    return content, media


def _copied_opportunity(value: object) -> tuple[StoryOpportunity, tuple[StoryMedia, ...]]:
    if (
        type(value) is not StoryOpportunity
        or type(value.target_id) is not UUID
        or type(value.owner_id) is not str
        or _IDENTIFIER.fullmatch(value.owner_id) is None
        or type(value.target_handle) is not str
        or _HANDLE.fullmatch(value.target_handle) is None
        or type(value.tier) is not str
        or value.tier not in _ALLOWED_TIERS
        or (
            value.media_id is not None
            and (
                type(value.media_id) is not str
                or _IDENTIFIER.fullmatch(value.media_id) is None
            )
        )
        or type(value.taken_at) is not datetime
        or type(value.fingerprint) is not str
        or _FINGERPRINT.fullmatch(value.fingerprint) is None
    ):
        raise ValueError
    taken_at = value.taken_at.astimezone(UTC)
    taken_at.timestamp()
    content, media = _copied_content(value.content)
    copied = StoryOpportunity(
        target_id=UUID(str(value.target_id)),
        owner_id=value.owner_id.encode("utf-8").decode("utf-8"),
        target_handle=value.target_handle.encode("utf-8").decode("utf-8"),
        tier=value.tier.encode("ascii").decode("ascii"),
        media_id=(
            None
            if value.media_id is None
            else value.media_id.encode("utf-8").decode("utf-8")
        ),
        taken_at=datetime.fromtimestamp(taken_at.timestamp(), tz=UTC),
        fingerprint=value.fingerprint.encode("ascii").decode("ascii"),
        content=content,
    )
    return copied, media


def _copied_history(value: object) -> IntelligenceHistory | None:
    if type(value) is not IntelligenceHistory:
        return None
    summary = value.recent_interaction_summary
    messages = value.sent_messages
    if (
        type(summary) is not str
        or not summary.strip()
        or len(summary) > 4_000
        or _contains_control(summary)
        or type(messages) is not tuple
        or len(messages) > 50
    ):
        return None
    for index in range(len(messages)):
        message = messages[index]
        if (
            type(message) is not str
            or not message.strip()
            or len(message) > 500
            or _contains_control(message)
        ):
            return None
    safe_messages = tuple(
        messages[index].encode("utf-8").decode("utf-8")
        for index in range(len(messages))
    )
    return IntelligenceHistory(
        summary.encode("utf-8").decode("utf-8"),
        safe_messages,
    )


def _copied_inputs(
    opportunity: object,
    persona: object,
    history: object,
) -> _ValidatedInputs | None:
    try:
        if (
            type(persona) is not Persona
        ):
            return None
        safe_persona = Persona(persona.text)
        safe_history = _copied_history(history)
        if safe_history is None:
            return None
        safe_opportunity, media = _copied_opportunity(opportunity)
        return _ValidatedInputs(
            opportunity=safe_opportunity,
            persona=safe_persona,
            history=safe_history,
            media=media,
        )
    except Exception:  # noqa: BLE001 - all malformed values fail closed
        return None


def _image_block(media: StoryMedia) -> dict[str, object]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media.mime_type,
            "data": base64.b64encode(media.data).decode("ascii"),
        },
    }


def _story_block(opportunity: StoryOpportunity) -> str:
    content = opportunity.content
    data = {
        "target_handle": opportunity.target_handle,
        "tier": opportunity.tier,
        "caption_text": content.caption_text,
        "sticker_text": content.sticker_text,
    }
    return untrusted_envelope("untrusted_story", data)


def _history_block(history: IntelligenceHistory) -> str:
    data = {
        "recent_interaction_summary": history.recent_interaction_summary,
        "last_sent_messages": history.sent_messages,
    }
    return untrusted_envelope("untrusted_history", data)


def _request(
    *,
    model: str,
    schema: type[BaseModel],
    persona: Persona,
    opportunity: StoryOpportunity,
    media: tuple[StoryMedia, ...],
    history: IntelligenceHistory,
    instruction: str,
    max_tokens: int,
) -> dict[str, object]:
    user_content = [_image_block(item) for item in media]
    user_content.append({"type": "text", "text": _story_block(opportunity)})
    user_content.append({"type": "text", "text": _history_block(history)})
    user_content.append({"type": "text", "text": instruction})
    return {
        "model": model,
        "max_tokens": max_tokens,
        "timeout": _REQUEST_TIMEOUT_SECONDS,
        "system": [
            {"type": "text", "text": SYSTEM_INSTRUCTIONS},
            {
                "type": "text",
                "text": untrusted_envelope(
                    "untrusted_persona",
                    {"persona": persona.text},
                ),
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": [{"role": "user", "content": user_content}],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": schema.model_json_schema(),
            }
        },
    }


async def _safe_call(
    messages: MessagesClient,
    request: Mapping[str, object],
    schema: type[DecisionModel],
) -> _CallResult:
    try:
        raw = await messages.create(**dict(request))
        raw_usage = getattr(raw, "usage", None)
        model = request.get("model")
        usage = None
        try:
            usage = ModelUsage(
                model,  # type: ignore[arg-type]
                getattr(raw_usage, "input_tokens", None),
                getattr(raw_usage, "output_tokens", None),
            )
        except (TypeError, ValueError):
            usage = None
        if (
            getattr(raw, "stop_reason", None) != "end_turn"
            or type(getattr(raw, "content", None)) is not list
            or len(raw.content) != 1
            or getattr(raw.content[0], "type", None) != "text"
            or type(getattr(raw.content[0], "text", None)) is not str
        ):
            _LOGGER.warning(
                "intelligence call returned an unusable response shape "
                "model=%s stop_reason=%r",
                model,
                getattr(raw, "stop_reason", None),
            )
            return _CallResult(None, usage)
        decoded = json.loads(raw.content[0].text)
        return _CallResult(schema.model_validate(decoded), usage)
    # The SDK boundary may raise provider, transport, or fake-client exceptions.
    except Exception as error:  # noqa: BLE001
        _LOGGER.warning(
            "intelligence call failed model=%s error=%s",
            request.get("model"),
            type(error).__name__,
        )
        return _CallResult(None)


class IntelligenceClient:
    """Route story evidence through the configured intelligence provider."""

    def __init__(
        self,
        messages: MessagesClient,
        *,
        triage_model: str,
        model: str,
    ) -> None:
        """One model writes every reply.

        There is no per-target model tier. Inference runs on a flat-rate
        subscription, so a cheaper model on some targets would buy nothing and
        cost reply quality. `triage_model` only answers the yes/no "is this
        worth replying to" gate; `model` writes everything a target sees.
        """

        valid_anthropic_models = (
            type(triage_model) is str
            and triage_model == _HAIKU_MODEL
            and type(model) is str
            and model == _BEST_MODEL
        )
        valid_codex_models = triage_model == _CODEX_MODEL and model == _CODEX_MODEL
        if not valid_anthropic_models and not valid_codex_models:
            del triage_model, model
            _raise_failure("invalid intelligence model configuration")
        self._messages = messages
        self._triage_model = triage_model
        self._model = model
        self._pending_usage: list[ModelUsage] = []

    def drain_usage(self) -> tuple[ModelUsage, ...]:
        usage = tuple(self._pending_usage)
        self._pending_usage.clear()
        return usage

    @classmethod
    def from_settings(cls, settings: Settings) -> IntelligenceClient:
        if settings.intelligence_provider == "hermes_agent":
            from engagemate.intelligence.hermes import HermesMessagesClient

            key = settings.hermes_agent_api_key
            if key is None or not key.get_secret_value().strip():
                _raise_failure("Hermes agent API key is required")
            http = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.hermes_agent_timeout_seconds),
            )
            messages = HermesMessagesClient(
                http=http,
                endpoint=settings.hermes_agent_url,
                api_key=key,
                agent_model=settings.hermes_agent_model,
            )
            return cls(
                messages,
                triage_model=_CODEX_MODEL,
                model=_CODEX_MODEL,
            )
        key = settings.anthropic_api_key
        if key is None or not key.get_secret_value().strip():
            _raise_failure("Anthropic API key is required for intelligence worker")
        initialization_failed = False
        try:
            import anthropic

            sdk = anthropic.AsyncAnthropic(
                api_key=key.get_secret_value(),
                max_retries=1,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        # Keep optional SDK import and construction failures behind one safe error.
        except Exception:  # noqa: BLE001
            initialization_failed = True
        if initialization_failed:
            _raise_failure("intelligence client initialization failed")
        return cls(
            sdk.messages,
            triage_model=settings.triage_model,
            model=settings.decision_model,
        )

    async def decide(
        self,
        opportunity: StoryOpportunity,
        persona: Persona,
        history: IntelligenceHistory,
    ) -> EngagementDecision:
        validated = _copied_inputs(
            opportunity,
            persona,
            history,
        )
        if validated is None:
            opportunity = None  # type: ignore[assignment]
            persona = None  # type: ignore[assignment]
            history = None  # type: ignore[assignment]
            _raise_failure("invalid intelligence input")
        item = validated.opportunity
        safe_persona = validated.persona
        safe_history = validated.history
        media = validated.media
        del opportunity, persona, history, validated

        haiku = await _safe_call(
            self._messages,
            _request(
                model=self._triage_model,
                schema=WorthReadingDecision,
                persona=safe_persona,
                opportunity=item,
                media=media,
                history=safe_history,
                instruction=HAIKU_INSTRUCTIONS,
                max_tokens=256,
            ),
            WorthReadingDecision,
        )
        if haiku.usage is not None:
            self._pending_usage.append(haiku.usage)
        if type(haiku.value) is not WorthReadingDecision:
            _raise_failure("intelligence decision failed")
        if not haiku.value.worth_reading:
            return EngagementDecision(
                engage=False,
                action="like",
                generated_text=None,
                confidence=haiku.value.confidence,
                rationale=haiku.value.rationale,
                model_used=self._triage_model,
            )

        full = await _safe_call(
            self._messages,
            _request(
                model=self._model,
                schema=EngagementDecision,
                persona=safe_persona,
                opportunity=item,
                media=media,
                history=safe_history,
                instruction=FULL_INSTRUCTIONS,
                max_tokens=1_024,
            ),
            EngagementDecision,
        )
        if full.usage is not None:
            self._pending_usage.append(full.usage)
        if type(full.value) is not EngagementDecision:
            _raise_failure("intelligence decision failed")
        return full.value.model_copy(update={"model_used": self._model})
