import json
import uuid

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from engagemate.control.routes.onboarding import BrandAnalyzeRequest
from engagemate.db.brand_profiles import _candidate_evidence
from engagemate.onboarding.brand import canonicalize_reference_urls
from engagemate.onboarding.candidates import select_balanced_candidates
from engagemate.onboarding.hypercrawl import HyperCrawlInstagramClient


def instagram(handle: str) -> str:
    return f"https://www.instagram.com/{handle}/"


def test_reference_contract_requires_three_unique_accounts() -> None:
    with pytest.raises(ValueError, match="at least 3 reference accounts"):
        canonicalize_reference_urls([instagram("one"), instagram("two")])


def test_reference_contract_deduplicates_canonical_duplicates() -> None:
    assert canonicalize_reference_urls(
        [
            "https://instagram.com/Creator/",
            "https://www.instagram.com/creator/?utm_source=test",
            instagram("another"),
            instagram("third"),
        ]
    ) == [instagram("creator"), instagram("another"), instagram("third")]


def test_reference_contract_accepts_three_to_five_accounts() -> None:
    assert len(
        canonicalize_reference_urls(
            [instagram("alpha"), instagram("beta"), instagram("gamma")]
        )
    ) == 3
    assert len(
        canonicalize_reference_urls([instagram(str(index)) for index in range(5)])
    ) == 5


def profile(username: str, followers: int = 2_000) -> dict[str, object]:
    return {
        "username": username,
        "followersCount": followers,
        "postsCount": 12,
        "biography": "Creator in Austin, Texas",
        "latestPosts": [{"likesCount": 40, "commentsCount": 4}],
    }


def test_balanced_selection_gives_each_productive_source_a_turn() -> None:
    groups = [
        ("alpha", [profile(f"alpha{index}") for index in range(6)]),
        ("beta", [profile("beta1")]),
        ("gamma", [profile("gamma1")]),
    ]

    selected = select_balanced_candidates(
        groups,
        reference_usernames={"alpha", "beta", "gamma"},
        limit=3,
    )

    assert {row["username"] for row in selected} == {
        "alpha0",
        "beta1",
        "gamma1",
    }


def test_selection_deduplicates_and_records_all_sources() -> None:
    duplicate = profile("shared")

    selected = select_balanced_candidates(
        [
            ("alpha", [duplicate]),
            ("beta", [duplicate]),
            ("gamma", [profile("other")]),
        ],
        reference_usernames={"alpha", "beta", "gamma"},
        limit=3,
    )

    shared = next(row for row in selected if row["username"] == "shared")
    assert shared["sources"] == ["alpha", "beta"]
    assert [row["username"] for row in selected].count("shared") == 1


def test_weak_source_does_not_block_backfill() -> None:
    selected = select_balanced_candidates(
        [
            ("alpha", [profile("bot", followers=12)]),
            ("beta", [profile("beta1"), profile("beta2")]),
            ("gamma", [profile("gamma1")]),
        ],
        reference_usernames={"alpha", "beta", "gamma"},
        limit=3,
    )

    assert [row["username"] for row in selected] == ["beta1", "gamma1", "beta2"]


async def test_hypercrawl_discovers_every_reference() -> None:
    references = [instagram("alpha"), instagram("beta"), instagram("gamma")]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        reference = body["referenceUrl"]
        seen.append(reference)
        source = reference.rstrip("/").rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "profile": {"username": source},
                "candidates": [profile(f"{source}_candidate")],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HyperCrawlInstagramClient(
            http=http,
            base_url="https://hypercrawl.test",
            rest_token=SecretStr("test-token"),
            timeout_seconds=15,
        )
        profiles, groups = await client.discover_many(references, ["founders"])

    assert seen == references
    assert [row["username"] for row in profiles] == ["alpha", "beta", "gamma"]
    assert [source for source, _ in groups] == ["alpha", "beta", "gamma"]
    assert [rows[0]["username"] for _, rows in groups] == [
        "alpha_candidate",
        "beta_candidate",
        "gamma_candidate",
    ]


def test_api_schema_rejects_fewer_than_three_references() -> None:
    with pytest.raises(ValidationError):
        BrandAnalyzeRequest(
            ig_account_id=uuid.uuid4(),
            reference_urls=[instagram("one"), instagram("two")],
            reference_profiles=[
                {
                    "username": username,
                    "fullName": username,
                    "biography": "",
                    "followersCount": 0,
                    "followingCount": 0,
                    "postsCount": 0,
                    "isPrivate": False,
                    "isVerified": False,
                    "latestPosts": [],
                }
                for username in ("one", "two", "three")
            ],
            answers={},
        )


def test_candidate_evidence_keeps_source_attribution() -> None:
    evidence = _candidate_evidence(
        {"sources": ["beta", "alpha", "alpha"], "reasons": [], "profile": {}}
    )

    assert evidence["sources"] == ["alpha", "beta"]
