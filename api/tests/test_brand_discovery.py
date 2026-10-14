import pytest

from engagemate.onboarding.brand import (
    MAX_REFERENCE_ACCOUNTS,
    align_profiles,
    canonicalize_reference_url,
    canonicalize_reference_urls,
    score_candidate,
)


def test_reference_urls_canonicalize_and_drop_repeats() -> None:
    assert canonicalize_reference_urls(
        [
            "https://www.instagram.com/First/",
            "https://instagram.com/first/?utm_source=x",
            "https://www.instagram.com/second/",
            "https://www.instagram.com/third/",
        ]
    ) == [
        "https://www.instagram.com/first/",
        "https://www.instagram.com/second/",
        "https://www.instagram.com/third/",
    ]


def test_reference_urls_reject_empty_and_more_than_five() -> None:
    with pytest.raises(ValueError):
        canonicalize_reference_urls([])
    too_many = [
        f"https://www.instagram.com/creator{index}/"
        for index in range(MAX_REFERENCE_ACCOUNTS + 1)
    ]
    with pytest.raises(ValueError):
        canonicalize_reference_urls(too_many)


def test_reference_urls_allow_exactly_five() -> None:
    five = [
        f"https://www.instagram.com/creator{index}/"
        for index in range(MAX_REFERENCE_ACCOUNTS)
    ]
    assert len(canonicalize_reference_urls(five)) == MAX_REFERENCE_ACCOUNTS


def test_align_profiles_matches_by_username_not_arrival_order() -> None:
    urls = [
        "https://www.instagram.com/first/",
        "https://www.instagram.com/second/",
        "https://www.instagram.com/third/",
    ]
    aligned = align_profiles(
        urls,
        [
            {"username": "second", "followersCount": 2},
            {"username": "first", "followersCount": 1},
        ],
    )
    assert [profile.get("followersCount") for profile in aligned] == [1, 2, None]


def test_reference_url_is_canonicalized_to_public_profile() -> None:
    assert (
        canonicalize_reference_url(
            "https://www.instagram.com/Creator.Name/?utm_source=profile"
        )
        == "https://www.instagram.com/creator.name/"
    )


def test_reference_url_rejects_non_instagram_and_non_profile_paths() -> None:
    for value in (
        "https://example.com/creator",
        "https://instagram.com/p/abc123/",
        "javascript:alert(1)",
    ):
        try:
            canonicalize_reference_url(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid reference URL: {value}")


def test_candidate_score_requires_public_us_signal_and_minimum_followers() -> None:
    candidate = {
        "username": "creator",
        "followersCount": 2_000,
        "postsCount": 40,
        "isPrivate": False,
        "joinedRecently": False,
        "biography": "Austin, TX creator",
        "latestPosts": [
            {"likesCount": 110, "commentsCount": 12},
            {"likesCount": 90, "commentsCount": 8},
        ],
    }
    scored = score_candidate(candidate)
    assert scored.accepted is True
    assert scored.us_signal is True
    assert scored.engagement_rate == 0.055
    assert scored.score == 100


def test_candidate_score_rejects_private_or_small_accounts() -> None:
    private = score_candidate(
        {"username": "private", "followersCount": 10_000, "isPrivate": True}
    )
    small = score_candidate(
        {"username": "small", "followersCount": 499, "isPrivate": False}
    )
    assert private.accepted is False
    assert small.accepted is False


@pytest.mark.parametrize(
    ("engagement", "expected_score"),
    [(299, 80), (996, 90)],
)
def test_candidate_score_uses_exact_rate_below_engagement_thresholds(
    engagement: int, expected_score: int
) -> None:
    scored = score_candidate(
        {
            "username": "threshold",
            "followersCount": 100_000,
            "postsCount": 6,
            "biography": "Austin creator",
            "latestPosts": [{"likesCount": engagement, "commentsCount": 0}],
        }
    )
    assert scored.engagement_rate == engagement / 100_000
    assert scored.score == expected_score
