"""Deterministic safety checks for generated engagement text."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from engagemate.domain.models import Action
from engagemate.intelligence.schemas import EngagementDecision

_MAX_TEXT = 200
_MAX_RECENT = 50
_MAX_RECENT_TEXT = 500
_MAX_PHRASES = 100
_MAX_PHRASE = 100
_MAX_FEATURES = 512
_MAX_HOSTNAME_CANDIDATES = 64
_MAX_HOSTNAME_LENGTH = 253
_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_OBFUSCATED_SCHEME_PATTERN = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_HOSTNAME_CANDIDATE = re.compile(
    r"(?<![\w-])[\w-]+(?:\.[\w-]+)+(?![\w-])",
    re.UNICODE,
)
_SPACED_DOT = re.compile(
    r"(?P<left>[\w-]+)(?P<before>\s*)\.(?P<after>\s*)(?P<right>[\w-]+)",
    re.UNICODE,
)
_ASCII_SUFFIX = re.compile(r"^[a-z]{2,63}$")
_SPACED_DOT_SUFFIXES = frozenset(
    {
        "ai",
        "app",
        "biz",
        "cloud",
        "co",
        "com",
        "dev",
        "edu",
        "gov",
        "in",
        "info",
        "io",
        "ly",
        "me",
        "net",
        "online",
        "org",
        "shop",
        "site",
        "space",
        "store",
        "systems",
        "tech",
        "travel",
        "uk",
        "us",
        "xyz",
    }
)
_LINK_INTENT_PREFIX = re.compile(
    r"(?:"
    r"\b(?:open|visit|browse|use|read|check\s+out|go\s+to)\s+"
    r"(?:the\s+)?(?:website|site|link)?\s*|"
    r"\b(?:website|site|link)\s*[:=-]?\s*"
    r")$",
    re.IGNORECASE,
)
_TERMINAL_PUNCTUATION = re.compile(r"""^[.!;,)\]}'"]{0,4}\s*$""")
_TERMINAL_LINK_TAIL = re.compile(
    r"^\s+(?:now|today)[.!?,;]{0,3}\s*$",
    re.IGNORECASE,
)
_CTA_PHRASES = (
    r"follow|dm|direct message|message|tag|contact|check out|reach out(?: to)?|"
    r"connect with|visit|find|hit up|go to"
)
_CTA_HANDLE_PATTERN = re.compile(
    rf"(?:\b(?:{_CTA_PHRASES})\b.{{0,48}}@[\w.]+|"
    rf"@[\w.]+.{{0,48}}\b(?:{_CTA_PHRASES})\b)",
    re.IGNORECASE,
)


class GateReason(StrEnum):
    ALLOWED = "allowed"
    NON_ENGAGING = "non_engaging"
    MALFORMED = "malformed"
    LINK = "link"
    HANDLE_CTA = "handle_cta"
    BANNED_PHRASE = "banned_phrase"
    TOO_LONG = "too_long"
    CONTROL = "control"
    LANGUAGE = "language"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class GateResult:
    allowed: bool
    reason: GateReason
    downgrade_to: Action | None


class LanguageMatcher(Protocol):
    def matches(self, text: str, expected_language: str) -> bool: ...


class BasicLanguageMatcher:
    """Small deterministic default for launch's configured English output."""

    def matches(self, text: str, expected_language: str) -> bool:
        if expected_language != "en":
            return False
        letters = [character for character in text if character.isalpha()]
        if not letters:
            return False
        latin = sum("LATIN" in unicodedata.name(character, "") for character in letters)
        return latin / len(letters) >= 0.8


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters = [
        character if character.isalnum() or character == "@" else " "
        for character in normalized
    ]
    return " ".join("".join(characters).split())


def _tokens(value: str) -> frozenset[str]:
    return frozenset(_TOKEN_PATTERN.findall(_normalize(value))[:_MAX_FEATURES])


def _ngrams(value: str, width: int = 3) -> Counter[str]:
    compact = _normalize(value).replace(" ", "")
    if len(compact) < width:
        return Counter({compact: 1}) if compact else Counter()
    return Counter(compact[index : index + width] for index in range(len(compact) - 2))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    common = set(left) & set(right)
    numerator = sum(left[key] * right[key] for key in common)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 1.0 if left == right else 0.0
    return numerator / (left_norm * right_norm)


def _copied_decision(value: object) -> EngagementDecision | None:
    if type(value) is not EngagementDecision:
        return None
    try:
        return EngagementDecision.model_validate(
            {
                "engage": value.engage,
                "action": value.action,
                "generated_text": value.generated_text,
                "confidence": value.confidence,
                "rationale": value.rationale,
                "model_used": value.model_used,
            }
        )
    except Exception:  # noqa: BLE001 - tampered model instances fail closed
        return None


def _link_text(value: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFKC", value).translate(
        str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})
    )
    normalized = re.sub(
        r"\s*(?:\[\s*(?:\.|dot)\s*\]|\(\s*(?:\.|dot)\s*\))\s*",
        ".",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\s+dot\s+", ".", normalized, flags=re.IGNORECASE)
    for _ in range(8):
        collapsed = _SPACED_DOT.sub(_collapse_spaced_dot, normalized)
        if collapsed == normalized:
            break
        normalized = collapsed
    folded = normalized.casefold()
    return folded, re.sub(r"\s+", "", folded)


def _collapse_spaced_dot(match: re.Match[str]) -> str:
    if not match.group("before") and not match.group("after"):
        return match.group(0)
    right = match.group("right")
    if match.group("after"):
        prefix = match.string[max(0, match.start() - 64) : match.start()]
        has_intent = _LINK_INTENT_PREFIX.search(prefix) is not None
        if (
            not _strong_spaced_suffix(right)
            or not _has_link_right_boundary(match, has_intent)
        ):
            return match.group(0)
    return f"{match.group('left')}.{right}"


def _has_link_right_boundary(match: re.Match[str], has_intent: bool) -> bool:
    tail = match.string[match.end() :]
    if tail.startswith(("/", "?", "#", ":")):
        return True
    if len(tail) > 64:
        return False
    if _TERMINAL_PUNCTUATION.fullmatch(tail) is not None:
        return True
    return has_intent and _TERMINAL_LINK_TAIL.fullmatch(tail) is not None


def _strong_spaced_suffix(value: str) -> bool:
    folded = value.casefold()
    if folded in _SPACED_DOT_SUFFIXES:
        return True
    if not 1 <= len(folded) <= 63:
        return False
    try:
        encoded = folded.encode("idna").decode("ascii").lower()
        if (
            not encoded.startswith("xn--")
            or len(encoded) > 63
            or encoded.startswith("-")
            or encoded.endswith("-")
        ):
            return False
        decoded = encoded.encode("ascii").decode("idna")
        return (
            decoded != encoded
            and decoded.encode("idna").decode("ascii").lower() == encoded
        )
    except UnicodeError:
        return False


def _valid_idna_hostname(candidate: str) -> bool:
    if not 1 <= len(candidate) <= _MAX_HOSTNAME_LENGTH:
        return False
    labels = candidate.split(".")
    if len(labels) < 2 or any(not label or "_" in label for label in labels):
        return False
    encoded_labels: list[str] = []
    try:
        for label in labels:
            encoded = label.encode("idna").decode("ascii").lower()
            if (
                not 1 <= len(encoded) <= 63
                or encoded.startswith("-")
                or encoded.endswith("-")
            ):
                return False
            if encoded.startswith("xn--"):
                decoded = encoded.encode("ascii").decode("idna")
                if (
                    decoded == encoded
                    or decoded.encode("idna").decode("ascii").lower() != encoded
                ):
                    return False
            encoded_labels.append(encoded)
    except UnicodeError:
        return False
    suffix = encoded_labels[-1]
    if _ASCII_SUFFIX.fullmatch(suffix) is not None:
        return True
    return suffix.startswith("xn--")


def _contains_hostname(value: str) -> bool:
    for count, match in enumerate(_HOSTNAME_CANDIDATE.finditer(value), start=1):
        if count > _MAX_HOSTNAME_CANDIDATES:
            return True
        if _valid_idna_hostname(match.group(0)):
            return True
    return False


class TextGate:
    """Evaluate generated text without network access or mutation."""

    def __init__(
        self,
        *,
        banned_phrases: tuple[str, ...] = (),
        expected_language: str = "en",
        language_matcher: LanguageMatcher | None = None,
        token_jaccard_threshold: float = 0.8,
        ngram_cosine_threshold: float = 0.9,
    ) -> None:
        if (
            type(banned_phrases) is not tuple
            or len(banned_phrases) > _MAX_PHRASES
            or any(
                type(phrase) is not str
                or not phrase.strip()
                or len(phrase) > _MAX_PHRASE
                or any(
                    unicodedata.category(character).startswith("C")
                    for character in phrase
                )
                or not _normalize(phrase)
                for phrase in banned_phrases
            )
            or type(expected_language) is not str
            or not expected_language.strip()
            or len(expected_language) > 16
            or any(
                unicodedata.category(character).startswith("C")
                for character in expected_language
            )
            or type(token_jaccard_threshold) is not float
            or not 0 <= token_jaccard_threshold <= 1
            or type(ngram_cosine_threshold) is not float
            or not 0 <= ngram_cosine_threshold <= 1
        ):
            raise ValueError("invalid text gate configuration")
        self._banned_phrases = tuple(_normalize(phrase) for phrase in banned_phrases)
        self._expected_language = expected_language
        self._language_matcher = language_matcher or BasicLanguageMatcher()
        self._token_threshold = token_jaccard_threshold
        self._ngram_threshold = ngram_cosine_threshold

    def evaluate(
        self,
        decision: EngagementDecision,
        recent_messages: tuple[str, ...],
    ) -> GateResult:
        safe_decision = _copied_decision(decision)
        decision = None  # type: ignore[assignment]
        if safe_decision is None:
            return GateResult(False, GateReason.MALFORMED, None)
        downgrade = Action.LIKE if safe_decision.engage else None
        if type(recent_messages) is not tuple:
            return GateResult(False, GateReason.MALFORMED, downgrade)
        recent_tail = recent_messages[-_MAX_RECENT:]
        if any(
            type(message) is not str
            or len(message) > _MAX_RECENT_TEXT
            or any(
                unicodedata.category(character).startswith("C")
                for character in message
            )
            for message in recent_tail
        ):
            return GateResult(False, GateReason.MALFORMED, downgrade)
        if not safe_decision.engage:
            return GateResult(False, GateReason.NON_ENGAGING, None)
        if safe_decision.action == "like":
            return GateResult(True, GateReason.ALLOWED, None)

        text = safe_decision.generated_text
        if type(text) is not str:
            return GateResult(False, GateReason.MALFORMED, downgrade)
        if len(text) > _MAX_TEXT:
            return GateResult(False, GateReason.TOO_LONG, downgrade)
        if any(unicodedata.category(character).startswith("C") for character in text):
            return GateResult(False, GateReason.CONTROL, downgrade)

        link_text, compact_link_text = _link_text(text)
        if _contains_hostname(link_text) or _OBFUSCATED_SCHEME_PATTERN.search(
            compact_link_text
        ):
            return GateResult(False, GateReason.LINK, downgrade)
        if _CTA_HANDLE_PATTERN.search(link_text):
            return GateResult(False, GateReason.HANDLE_CTA, downgrade)

        normalized = _normalize(text)
        if any(phrase in normalized for phrase in self._banned_phrases):
            return GateResult(False, GateReason.BANNED_PHRASE, downgrade)
        try:
            matched = self._language_matcher.matches(text, self._expected_language)
            if type(matched) is not bool or not matched:
                return GateResult(False, GateReason.LANGUAGE, downgrade)
        # An injected matcher is an untrusted, narrow dependency.
        except Exception:  # noqa: BLE001
            return GateResult(False, GateReason.LANGUAGE, downgrade)

        current_tokens = _tokens(text)
        current_ngrams = _ngrams(text)
        for recent in recent_tail:
            bounded_recent = recent
            if (
                _jaccard(current_tokens, _tokens(bounded_recent))
                >= self._token_threshold
                or _cosine(current_ngrams, _ngrams(bounded_recent))
                >= self._ngram_threshold
            ):
                return GateResult(False, GateReason.DUPLICATE, downgrade)
        return GateResult(True, GateReason.ALLOWED, None)
