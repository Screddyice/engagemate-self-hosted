from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from engagemate.domain.models import Action
from engagemate.intelligence.schemas import EngagementDecision
from engagemate.intelligence.text_gate import GateReason, TextGate


def decision(
    text: str | None,
    *,
    engage: bool = True,
    action: str = "reply",
) -> EngagementDecision:
    return EngagementDecision(
        engage=engage,
        action=action,
        generated_text=text,
        confidence=0.9,
        rationale="Safe and relevant.",
        model_used="claude-sonnet-4-6",
    )


def unvalidated_decision(text: str) -> EngagementDecision:
    return EngagementDecision.model_construct(
        engage=True,
        action="reply",
        generated_text=text,
        confidence=0.9,
        rationale="Safe and relevant.",
        model_used="claude-sonnet-4-6",
    )


class Matcher:
    def __init__(self, result: object = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def matches(self, text: str, expected_language: str) -> bool:
        self.calls.append((text, expected_language))
        if self.error:
            raise self.error
        return self.result


@pytest.mark.parametrize(
    "text",
    [
        "Visit https://example.com now",
        "Visit HTTP : / / EXAMPLE.COM now",
        "See www.example.org",
        "Try example.co.uk today",
        "Open example.io/path",
        "Read example.info, then reply",
        "Avoid evil.in today",
        "Open example.us",
        "Read example.travel",
        "See ｅｘａｍｐｌｅ．ｔｒａｖｅｌ",
        "Use example [.] cloud",
        "Use example dot systems",
        "Use example . space",
    ],
)
def test_gate_rejects_links_and_obfuscated_schemes(text: str) -> None:
    result = TextGate().evaluate(decision(text), ())

    assert result.allowed is False
    assert result.reason is GateReason.LINK
    assert result.downgrade_to is Action.LIKE


@pytest.mark.parametrize(
    "text",
    [
        "Open example .com",
        "Open example. com",
        "Open example . com",
        "Open example。com",
        "Open example｡com",
        "Open 例子.com",
        "Open 例子 .com",
        "Open example .公司",
        "Open EXAMPLE .COM",
        "Open xn--fsqu00a.xn--fiqs8s",
    ],
)
def test_gate_rejects_unicode_idna_and_independently_spaced_domains(
    text: str,
) -> None:
    result = TextGate(language_matcher=Matcher(True)).evaluate(decision(text), ())

    assert result.reason is GateReason.LINK
    assert result.downgrade_to is Action.LIKE


@pytest.mark.parametrize(
    "text",
    [
        "Great work. this looks amazing",
        "Congrats. well deserved",
        "Loved this. absolutely beautiful",
    ],
)
def test_gate_allows_normal_sentences_with_lowercase_words_after_periods(
    text: str,
) -> None:
    result = TextGate(language_matcher=Matcher(True)).evaluate(decision(text), ())

    assert result.allowed is True
    assert result.reason is GateReason.ALLOWED


@pytest.mark.parametrize(
    "text",
    [
        "Count me in. in every time",
        "You inspire me. me too",
        "Proud of us. us together",
        "Love to travel. travel opens minds",
        "Shopping online. online works well",
        "Great shop. shop local",
        "Built with AI. ai can help",
        "Beautiful space. space feels calm",
    ],
)
def test_gate_allows_sentence_words_that_match_launch_suffixes(text: str) -> None:
    result = TextGate(language_matcher=Matcher(True)).evaluate(decision(text), ())

    assert result.allowed is True
    assert result.reason is GateReason.ALLOWED


@pytest.mark.parametrize(
    "text",
    [
        "Open example. com",
        "Open example . com",
        "Visit example. travel",
        "Browse example. ai",
        "Check out example. shop",
        "Go to example. online",
    ],
)
def test_gate_uses_explicit_intent_for_ambiguous_spaced_domains(text: str) -> None:
    result = TextGate(language_matcher=Matcher(True)).evaluate(decision(text), ())

    assert result.reason is GateReason.LINK


@pytest.mark.parametrize(
    "text",
    [
        "Read this. in my opinion it is excellent",
        "Use care. in this situation it helps",
        "Visit often. in summer it is beautiful",
        "Go to school. in time you will learn",
        "Browse posts. online conversations help",
        "Check out art. ai can help creators",
        "Open minds. in every conversation",
        "Read books. me too",
    ],
)
def test_gate_allows_intent_verbs_when_suffix_word_continues_as_prose(
    text: str,
) -> None:
    result = TextGate(language_matcher=Matcher(True)).evaluate(decision(text), ())

    assert result.allowed is True
    assert result.reason is GateReason.ALLOWED


@pytest.mark.parametrize(
    "text",
    [
        "Open example. com/path",
        "Visit example. com?source=story",
        "Use example. com#details",
        "Browse example. com:443",
        "Open example. com now",
        "Visit example. travel today!",
    ],
)
def test_gate_rejects_ambiguous_domains_with_url_or_terminal_tail(
    text: str,
) -> None:
    result = TextGate(language_matcher=Matcher(True)).evaluate(decision(text), ())

    assert result.reason is GateReason.LINK


@pytest.mark.parametrize(
    "text",
    [
        "Follow @target for updates",
        "DM @target to learn more",
        "@target please follow us",
        "Message @target today",
        "Check out @target",
        "Reach out to @target",
        "Connect with @target",
        "Visit @target",
        "Find @target",
        "Hit up @target",
        "Ｃｈｅｃｋ　ｏｕｔ　＠target",
    ],
)
def test_gate_rejects_handles_used_in_calls_to_action(text: str) -> None:
    result = TextGate().evaluate(decision(text), ())

    assert result.reason is GateReason.HANDLE_CTA


def test_gate_allows_conversational_handle_mentions_without_a_call_to_action() -> None:
    result = TextGate().evaluate(
        decision("Loved chatting with @target yesterday"), ()
    )

    assert result.allowed is True


def test_gate_normalizes_unicode_case_punctuation_and_whitespace_for_phrases() -> None:
    gate = TextGate(banned_phrases=("buy now",))

    result = gate.evaluate(decision("ＢＵＹ,　ＮＯＷ"), ())

    assert result.reason is GateReason.BANNED_PHRASE


def test_gate_enforces_unicode_code_point_limit_and_controls() -> None:
    assert (
        TextGate().evaluate(unvalidated_decision("🙂" * 201), ()).reason
        is GateReason.MALFORMED
    )
    assert (
        TextGate().evaluate(unvalidated_decision("hello\u200bworld"), ()).reason
        is GateReason.MALFORMED
    )


def test_gate_uses_injected_language_matcher_and_fails_closed() -> None:
    mismatch = Matcher(False)
    failed = Matcher(error=RuntimeError("detector secret"))

    mismatch_result = TextGate(
        language_matcher=mismatch, expected_language="en"
    ).evaluate(decision("Bonjour tout le monde"), ())
    failed_result = TextGate(language_matcher=failed).evaluate(
        decision("Hello there"), ()
    )

    assert mismatch_result.reason is GateReason.LANGUAGE
    assert failed_result.reason is GateReason.LANGUAGE
    assert mismatch.calls == [("Bonjour tout le monde", "en")]


@pytest.mark.parametrize("matcher_result", [None, 1, 0, object(), "yes"])
def test_gate_requires_exact_boolean_matcher_result(matcher_result: object) -> None:
    result = TextGate(language_matcher=Matcher(matcher_result)).evaluate(
        decision("Hello there"), ()
    )

    assert result.reason is GateReason.LANGUAGE
    assert result.downgrade_to is Action.LIKE


@pytest.mark.parametrize(
    "recent",
    [
        ("love this product launch so much",),
        ("I really love this product launch!",),
    ],
)
def test_gate_rejects_near_duplicates_by_token_or_ngram_similarity(
    recent: tuple[str, ...],
) -> None:
    result = TextGate(
        token_jaccard_threshold=0.70,
        ngram_cosine_threshold=0.65,
    ).evaluate(decision("I love this product launch so much"), recent)

    assert result.reason is GateReason.DUPLICATE


def test_gate_compares_only_latest_fifty_sent_messages() -> None:
    duplicate = "A unique old message about solar kites"
    recent = (duplicate,) + tuple(f"recent message number {index}" for index in range(50))

    result = TextGate().evaluate(decision(duplicate), recent)

    assert result.allowed is True


def test_similarity_threshold_edges_are_inclusive_and_repeatable() -> None:
    gate = TextGate(token_jaccard_threshold=0.5, ngram_cosine_threshold=1.0)
    current = decision("alpha beta gamma")
    recent = ("alpha beta delta",)

    first = gate.evaluate(current, recent)
    second = gate.evaluate(current, recent)

    assert first == second
    assert first.reason is GateReason.DUPLICATE


def test_nonengaging_and_malformed_values_are_never_upgraded() -> None:
    nonengaging = decision(None, engage=False, action="like")

    result = TextGate().evaluate(nonengaging, ())
    malformed = TextGate().evaluate(object(), ())  # type: ignore[arg-type]

    assert result.allowed is False
    assert result.downgrade_to is None
    assert malformed.reason is GateReason.MALFORMED
    assert malformed.downgrade_to is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("engage", 1),
        ("action", "share"),
        ("generated_text", object()),
        ("confidence", True),
        ("rationale", object()),
        ("model_used", "claude-unknown"),
    ],
)
def test_tampered_decisions_fail_malformed_without_downgrade(
    field: str, value: object
) -> None:
    tampered = decision("Hello there")
    object.__setattr__(tampered, field, value)

    result = TextGate().evaluate(tampered, ())

    assert result.reason is GateReason.MALFORMED
    assert result.downgrade_to is None


def test_valid_like_and_original_text_pass_without_mutation() -> None:
    like = decision(None, action="like")
    reply = decision("Thoughtful update. Congratulations!")
    gate = TextGate()

    like_result = gate.evaluate(like, ())
    reply_result = gate.evaluate(reply, ())

    assert like_result.allowed is True
    assert reply_result.allowed is True
    assert like.generated_text is None
    assert reply.generated_text == "Thoughtful update. Congratulations!"
    with pytest.raises(FrozenInstanceError):
        like_result.allowed = False  # type: ignore[misc]


@pytest.mark.parametrize(
    "recent",
    [
        object(),
        ["valid"],
        ["valid", object()],
    ],
)
def test_malformed_recent_messages_fail_closed_without_values(recent: object) -> None:
    result = TextGate().evaluate(decision("Original response"), recent)  # type: ignore[arg-type]

    assert result.reason is GateReason.MALFORMED
    assert result.downgrade_to is Action.LIKE


def test_gate_ignores_malformed_and_huge_messages_older_than_latest_fifty() -> None:
    recent = (object(), "x" * 50_000) + tuple(
        f"safe recent message {index}" for index in range(50)
    )

    result = TextGate().evaluate(decision("Original response"), recent)  # type: ignore[arg-type]

    assert result.allowed is True


@pytest.mark.parametrize("bad_tail", [object(), "x" * 501])
def test_gate_rejects_malformed_values_inside_latest_fifty(
    bad_tail: object,
) -> None:
    recent = tuple(f"old {index}" for index in range(50)) + (bad_tail,)

    result = TextGate().evaluate(decision("Original response"), recent)  # type: ignore[arg-type]

    assert result.reason is GateReason.MALFORMED
    assert result.downgrade_to is Action.LIKE


@pytest.mark.parametrize(
    "kwargs",
    [
        {"banned_phrases": ("safe\u200bphrase",)},
        {"banned_phrases": ("!!!",)},
        {"expected_language": "en\nsecret"},
    ],
)
def test_malformed_gate_configuration_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="^invalid text gate configuration$"):
        TextGate(**kwargs)  # type: ignore[arg-type]
