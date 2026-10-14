"""Validated public-profile metrics shared by onboarding and growth snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


_POSTGRES_INTEGER_MAX = 2_147_483_647


@dataclass(frozen=True, slots=True)
class ProfileMetrics:
    follower_count: int | None
    following_count: int | None
    media_count: int | None
    engagement_rate_bps: int | None
    posts_sampled: int
    status: Literal["complete", "partial", "unavailable"]


def _valid_profile_count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _POSTGRES_INTEGER_MAX:
        raise ValueError("profile metric count must be a bounded integer")
    return value


def _profile_count(profile: dict[object, object], *keys: str) -> int | None:
    for key in keys:
        value = profile.get(key)
        if value is not None:
            return _valid_profile_count(value)
    return None


def _post_count(value: object) -> int | None:
    if type(value) is int and 0 <= value <= _POSTGRES_INTEGER_MAX:
        return value
    return None


def _valid_post_totals(posts: object) -> list[int]:
    if not isinstance(posts, list):
        return []
    totals: list[int] = []
    for post in posts:
        if len(totals) == 12:
            break
        if not isinstance(post, dict):
            continue
        likes = _post_count(post.get("likesCount", post.get("likes")))
        comments = _post_count(post.get("commentsCount", post.get("comments")))
        if likes is None or comments is None:
            continue
        totals.append(likes + comments)
    return totals


def _post_engagement_ratio(posts: object, followers: int) -> tuple[float | None, int]:
    if type(followers) is not int or not 0 < followers <= _POSTGRES_INTEGER_MAX:
        return None, 0
    totals = _valid_post_totals(posts)
    if not totals:
        return None, 0
    return sum(totals) / len(totals) / followers, len(totals)


def post_engagement_ratio(posts: object, followers: int) -> float | None:
    """Calculate the exact average post engagement ratio for onboarding scores."""

    ratio, _ = _post_engagement_ratio(posts, followers)
    return ratio


def post_engagement_rate_bps(posts: object, followers: int) -> tuple[int | None, int]:
    """Calculate average post engagement as basis points from at most 12 posts."""

    ratio, posts_sampled = _post_engagement_ratio(posts, followers)
    if ratio is None:
        return None, 0
    return min(round(ratio * 10_000), _POSTGRES_INTEGER_MAX), posts_sampled


def parse_profile_metrics(profile: object) -> ProfileMetrics:
    """Extract bounded metric fields while preserving missing-data semantics."""

    if not isinstance(profile, dict):
        return ProfileMetrics(None, None, None, None, 0, "unavailable")

    followers = _profile_count(profile, "followersCount", "followers")
    following = _profile_count(profile, "followingCount", "following")
    media = _profile_count(profile, "postsCount", "mediaCount", "posts")
    posts = profile.get("latestPosts", profile.get("postsData"))
    if followers is None:
        return ProfileMetrics(None, following, media, None, 0, "unavailable")

    engagement_rate_bps, posts_sampled = post_engagement_rate_bps(posts, followers)
    status: Literal["complete", "partial", "unavailable"]
    if following is None or media is None or engagement_rate_bps is None:
        status = "partial"
    else:
        status = "complete"
    return ProfileMetrics(
        followers,
        following,
        media,
        engagement_rate_bps,
        posts_sampled,
        status,
    )
