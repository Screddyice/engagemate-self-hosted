import pytest

from engagemate.growth.metrics import (
    parse_profile_metrics,
    post_engagement_ratio,
    post_engagement_rate_bps,
)


def test_post_engagement_uses_twelve_valid_posts_and_basis_points() -> None:
    posts = [{"likesCount": 80, "commentsCount": 20}] * 12
    rate, sampled = post_engagement_rate_bps(posts, 1_000)
    assert (rate, sampled) == (1_000, 12)


def test_post_engagement_limits_the_sample_to_twelve_posts() -> None:
    posts = [{"likesCount": 100, "commentsCount": 0}] * 13
    rate, sampled = post_engagement_rate_bps(posts, 1_000)
    assert (rate, sampled) == (1_000, 12)


def test_post_engagement_collects_twelve_valid_posts_after_malformed_prefix() -> None:
    posts = [None] * 12 + [{"likesCount": 80, "commentsCount": 20}] * 12
    assert post_engagement_rate_bps(posts, 1_000) == (1_000, 12)


def test_post_engagement_returns_unavailable_for_zero_followers_or_missing_posts() -> None:
    assert post_engagement_rate_bps([], 0) == (None, 0)
    assert post_engagement_rate_bps(None, 1_000) == (None, 0)


def test_post_engagement_skips_malformed_post_rows() -> None:
    posts = [
        {"likesCount": 80, "commentsCount": 20},
        "not a post",
        None,
        {"likes": 20, "comments": 0},
    ]
    assert post_engagement_rate_bps(posts, 1_000) == (600, 2)


def test_post_engagement_excludes_rows_with_missing_or_invalid_counts() -> None:
    posts = [
        {"likesCount": 80, "commentsCount": 20},
        {"likesCount": 8},
        {"commentsCount": 2},
        {"likesCount": -1, "commentsCount": 2},
        {"likes": 20, "comments": 0},
    ]
    assert post_engagement_rate_bps(posts, 1_000) == (600, 2)


def test_post_engagement_ratio_preserves_fractional_basis_points() -> None:
    assert post_engagement_ratio([{"likesCount": 996, "commentsCount": 0}], 100_000) == 0.00996


def test_profile_metrics_use_alternate_hypercrawl_keys() -> None:
    metrics = parse_profile_metrics(
        {
            "followers": 1_000,
            "following": 50,
            "posts": 12,
            "postsData": [{"likes": 80, "comments": 20}],
        }
    )
    assert metrics.follower_count == 1_000
    assert metrics.following_count == 50
    assert metrics.media_count == 12
    assert metrics.engagement_rate_bps == 1_000
    assert metrics.posts_sampled == 1
    assert metrics.status == "complete"


def test_profile_metrics_distinguish_partial_from_unavailable() -> None:
    assert parse_profile_metrics({"followersCount": 100}).status == "partial"
    assert parse_profile_metrics({}).status == "unavailable"


def test_profile_metrics_treat_zero_followers_as_partial() -> None:
    metrics = parse_profile_metrics(
        {"followersCount": 0, "followingCount": 0, "postsCount": 0, "latestPosts": []}
    )
    assert metrics.engagement_rate_bps is None
    assert metrics.status == "partial"


@pytest.mark.parametrize("bad", [True, -1, 1.5, "100", 2**63])
def test_profile_metrics_reject_unbounded_or_non_integer_counts(bad: object) -> None:
    with pytest.raises(ValueError, match="profile metric"):
        parse_profile_metrics({"followersCount": bad})
