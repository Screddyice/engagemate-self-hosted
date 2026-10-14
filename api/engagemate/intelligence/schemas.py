"""Strict values at the story intelligence boundary."""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MAX_GENERATED_TEXT = 200
MAX_RATIONALE = 1_000
MAX_PERSONA = 2_000
MAX_SUMMARY = 4_000
MAX_SENT_MESSAGE = 500
MAX_SENT_MESSAGES = 50

BoundedText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=MAX_GENERATED_TEXT),
]
ActionName = Literal["view", "like", "reply", "dm"]
ModelAlias = Literal[
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-8",
    "gpt-5.6-terra",
    "engagemate-agent",
]


@dataclass(frozen=True, slots=True, repr=False)
class ModelUsage:
    """Redacted provider token counts for one model call."""

    model: ModelAlias
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if (
            self.model
            not in {
                "claude-haiku-4-5",
                "claude-sonnet-4-6",
                "claude-opus-4-8",
                "gpt-5.6-terra",
                "engagemate-agent",
            }
            or type(self.input_tokens) is not int
            or type(self.output_tokens) is not int
            or not 0 <= self.input_tokens <= 1_000_000_000
            or not 0 <= self.output_tokens <= 1_000_000_000
        ):
            raise ValueError("invalid model usage")

    def __repr__(self) -> str:
        return (
            f"ModelUsage(model={self.model!r}, input_tokens={self.input_tokens}, "
            f"output_tokens={self.output_tokens})"
        )


def _has_disallowed_characters(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


class _StrictDecision(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        hide_input_in_errors=True,
    )

    confidence: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    rationale: str = Field(strict=True, min_length=1, max_length=MAX_RATIONALE)

    @field_validator("confidence")
    @classmethod
    def finite_confidence(cls, value: float) -> float:
        if type(value) is not float or not math.isfinite(value):
            raise ValueError("invalid confidence")
        return value

    @field_validator("rationale")
    @classmethod
    def clean_rationale(cls, value: str) -> str:
        if not value.strip() or _has_disallowed_characters(value):
            raise ValueError("invalid rationale")
        return value


class EngagementDecision(_StrictDecision):
    """One fully validated engagement action."""

    engage: bool = Field(strict=True)
    action: ActionName
    generated_text: BoundedText | None
    model_used: ModelAlias

    @field_validator("model_used")
    @classmethod
    def clean_model_name(cls, value: str) -> str:
        if not value.strip() or _has_disallowed_characters(value):
            raise ValueError("invalid model name")
        return value

    @field_validator("generated_text")
    @classmethod
    def clean_generated_text(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip() or _has_disallowed_characters(value)
        ):
            raise ValueError("invalid generated text")
        return value

    @model_validator(mode="after")
    def consistent_action(self) -> EngagementDecision:
        if not self.engage and self.generated_text is not None:
            raise ValueError("non-engaging decisions cannot contain text")
        if self.engage and self.action in {"view", "like"} and self.generated_text is not None:
            raise ValueError("view and like actions cannot contain text")
        if self.engage and self.action in {"reply", "dm"} and self.generated_text is None:
            raise ValueError("text action requires text")
        return self


class WorthReadingDecision(_StrictDecision):
    """Haiku's cheap decision about whether full analysis is warranted."""

    worth_reading: bool = Field(strict=True)


@dataclass(frozen=True, slots=True, repr=False, init=False)
class Persona:
    """Bounded account persona treated as untrusted prompt data."""

    text: str

    def __init__(self, text: object) -> None:
        invalid = (
            type(text) is not str
            or not text.strip()
            or len(text) > MAX_PERSONA
            or _has_disallowed_characters(text)
        )
        if invalid:
            del text
            raise ValueError("invalid intelligence input") from None
        object.__setattr__(self, "text", text)

    def __repr__(self) -> str:
        return "Persona(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False, init=False)
class IntelligenceHistory:
    """Bounded, immutable history used for routing and duplicate context."""

    recent_interaction_summary: str
    sent_messages: tuple[str, ...]

    def __init__(
        self,
        recent_interaction_summary: object,
        sent_messages: object,
    ) -> None:
        summary_valid = (
            type(recent_interaction_summary) is str
            and bool(recent_interaction_summary.strip())
            and len(recent_interaction_summary) <= MAX_SUMMARY
            and not _has_disallowed_characters(recent_interaction_summary)
        )
        messages_valid = (
            type(sent_messages) is tuple
            and len(sent_messages) <= MAX_SENT_MESSAGES
            and all(
                type(message) is str
                and bool(message.strip())
                and len(message) <= MAX_SENT_MESSAGE
                and not _has_disallowed_characters(message)
                for message in sent_messages
            )
        )
        if not summary_valid or not messages_valid:
            del recent_interaction_summary, sent_messages
            raise ValueError("invalid intelligence input") from None
        safe_messages = tuple(message for message in sent_messages)
        object.__setattr__(
            self,
            "recent_interaction_summary",
            recent_interaction_summary,
        )
        object.__setattr__(self, "sent_messages", safe_messages)

    def __repr__(self) -> str:
        return "IntelligenceHistory(<redacted>)"
