from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from engagemate.onboarding import analysis


def _complete_analysis() -> dict[str, object]:
    return {
        "voice_summary": "Warm, direct, and human",
        "audience_summary": "Founders who care about travel and AI",
        "brand_identity": "A founder sharing the work and the journey",
        "engagement_style": "Specific, positive replies without hype",
        "content_pillars": ["founder life", "AI", "travel"],
        "conversation_topics": ["building companies", "AI tools", "travel"],
        "search_terms": ["founder travel creator", "AI founder"],
        "us_audience_signals": [],
        "next_question": "Which part of that direction feels most like you?",
    }


@pytest.mark.asyncio
async def test_analyze_brand_retries_one_off_schema_hermes_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing required field must not strand a user on the Brand step."""

    calls: list[dict[str, object]] = []

    class FakeHermesMessagesClient:
        def __init__(self, **_: object) -> None:
            pass

        async def create(self, **request: object) -> object:
            calls.append(request)
            user_text = request["messages"][0]["content"][0]["text"]  # type: ignore[index]
            payload = _complete_analysis()
            if "REPAIR REQUIRED" not in user_text:
                payload.pop("voice_summary")
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(payload))],
            )

    monkeypatch.setattr(analysis, "HermesMessagesClient", FakeHermesMessagesClient)

    async with httpx.AsyncClient() as http:
        result = await analysis.analyze_brand(
            http=http,
            endpoint="http://127.0.0.1:8643/v1/chat/completions",
            api_key=object(),
            model="engagemate-agent",
            reference_urls=["https://www.instagram.com/example/"],
            answers={
                "brand": "Founder and travel content",
                "audience": "Founders who travel",
                "interests": "AI and building companies",
                "voice": "Human and positive",
                "goals": "Reach the right people",
            },
            scraped_profiles=[{"username": "example"}],
        )

    assert result.voice_summary == "Warm, direct, and human"
    assert len(calls) == 2
    repair_text = calls[1]["messages"][0]["content"][0]["text"]  # type: ignore[index]
    assert "REPAIR REQUIRED" in repair_text
    assert all(field in repair_text for field in analysis.BrandAnalysis.model_fields)
