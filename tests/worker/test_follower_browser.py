from unittest.mock import AsyncMock, MagicMock

import pytest
from engagemate.worker.follower_browser import (
    FollowerBrowserError,
    PlaywrightFollowerBrowser,
    parse_followers,
    parse_profile,
    parse_public_profile,
)

NOW = 2_000_000_000


def profile(**changes):
    node = {"pk": "123", "id": "123", "username": "Prospect",
            "is_private": False, "latest_reel_media": NOW - 60,
            "media_count": 50, "follower_count": 1600,
            "friendship_status": {"following": False, "followed_by": False}}
    node.update(changes)
    return node


def test_exact_full_profile_ignores_partial_and_other_users():
    result = parse_profile({"data": [profile(), {"id": "123", "username": "Prospect", "is_private": False, "friendship_status": {"following": True}}, profile(username="other", pk="456", id="456")]}, "prospect", now=NOW)
    assert result == {"owner_id": "123", "handle": "prospect", "private": False,
                      "following": False, "followed_by": False, "has_story": True}


@pytest.mark.parametrize("reel,expected", [(0, False), (NOW - 86399, True), (NOW - 86400, False), (NOW + 1, None), ("123", None), (True, None), (-1, None), (float("nan"), None), (None, None)])
def test_story_age_requires_real_fresh_timestamp(reel, expected):
    assert parse_profile({"data": profile(latest_reel_media=reel)}, "prospect", now=NOW)["has_story"] is expected


def test_relationships_are_strict_and_missing_is_unknown():
    result = parse_profile({"user": profile(is_private=0, friendship_status={"following": "false"})}, "prospect", now=NOW)
    assert result["private"] is None
    assert result["following"] is None
    assert result["followed_by"] is None


@pytest.mark.parametrize("changes", [{"id": "456"}, {"pk": True, "id": True}, {"pk": "x", "id": "x"}, {"friendship_status": None}, {"username": "other"}])
def test_invalid_identity_or_partial_is_not_a_profile(changes):
    assert parse_profile({"data": profile(**changes)}, "prospect", now=NOW) is None


def test_conflicting_profiles_fail_closed():
    with pytest.raises(FollowerBrowserError, match="profile_ambiguous"):
        parse_profile({"data": [profile(), profile(is_private=True)]}, "prospect", now=NOW)


def followers(**changes):
    payload = {"status": "ok", "users": [{"pk": "123", "username": "Prospect"}], "has_more": False}
    payload.update(changes)
    return payload


def test_follower_list_only_uses_top_level_users():
    result = parse_followers(followers(suggestions=[{"pk": "666", "username": "suggestion"}], should_limit_list_of_followers=True))
    assert result == {"candidates": [{"owner_id": "123", "handle": "prospect"}], "next_cursor": None, "exhausted": True, "limited": True}


def test_paginated_followers_require_verified_token():
    result = parse_followers(followers(has_more=True, next_max_id="50|opaque"))
    assert result["next_cursor"] == "50|opaque"
    assert result["exhausted"] is False


@pytest.mark.parametrize("changes", [{"has_more": None}, {"has_more": 0}, {"has_more": True}, {"has_more": False, "next_max_id": "stale"}, {"has_more": True, "next_max_id": "x" * 2049}, {"has_more": True, "next_max_id": 7}, {"has_more": True, "next_max_id": "x\n"}])
def test_unreadable_pagination_is_not_exhaustion(changes):
    with pytest.raises(FollowerBrowserError, match="pagination_unreadable"):
        parse_followers(followers(**changes))


@pytest.mark.parametrize("users", [None, {}, [1], [{"pk": "123", "username": "../stories"}], [{"pk": "123", "id": "456", "username": "prospect"}]])
def test_malformed_followers_never_become_empty(users):
    with pytest.raises(FollowerBrowserError, match="followers_unreadable"):
        parse_followers(followers(users=users))


@pytest.mark.parametrize("payload,reason", [({"message": "login_required"}, "authentication_required"), ({"challenge": {"url": "hidden"}}, "checkpoint_required"), ({"message": "Please wait a few minutes"}, "rate_limited"), ({"status": "fail"}, "response_failed"), ({"errors": [{}]}, "response_failed"), ([], "response_unreadable")])
def test_response_error_codes(payload, reason):
    with pytest.raises(FollowerBrowserError, match=reason):
        parse_followers(payload)


@pytest.mark.asyncio
async def test_private_unfollowed_source_is_explicitly_inaccessible():
    page = MagicMock()
    browser = PlaywrightFollowerBrowser(page)
    browser.inspect = AsyncMock(return_value={"owner_id": "123", "private": True, "following": False})
    assert await browser.followers("prospect") == {"candidates": [], "next_cursor": None, "exhausted": True, "reason": "source_private"}
    page.get_by_text.assert_not_called()
    page.context.request.get.assert_not_called()


@pytest.mark.asyncio
async def test_profile_listener_and_tasks_are_cleaned_after_timeout():
    page = MagicMock()
    page.url = "https://www.instagram.com/prospect/"
    page.goto = AsyncMock(return_value=None)
    page.locator.return_value.count = AsyncMock(return_value=0)
    browser = PlaywrightFollowerBrowser(page, timeout_ms=100)
    with pytest.raises(FollowerBrowserError, match="profile_unreadable"):
        await browser.inspect("prospect")
    page.remove_listener.assert_called_once_with("response", page.on.call_args.args[1])
    assert page.goto.call_args.args[0] == "https://www.instagram.com/prospect/"


@pytest.mark.asyncio
async def test_no_story_navigation_from_malicious_handle():
    page = MagicMock()
    browser = PlaywrightFollowerBrowser(page)
    with pytest.raises(FollowerBrowserError, match="invalid_handle"):
        await browser.inspect("../stories/prospect")
    page.goto.assert_not_called()


@pytest.mark.asyncio
async def test_reserved_story_route_cannot_be_a_profile():
    page = MagicMock()
    browser = PlaywrightFollowerBrowser(page)
    with pytest.raises(FollowerBrowserError, match="invalid_handle"):
        await browser.inspect("stories")
    page.goto.assert_not_called()


@pytest.mark.asyncio
async def test_profile_capture_accepts_non_json_content_type_and_cleans_listener():
    page = MagicMock()
    page.url = "https://www.instagram.com/prospect/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    response = MagicMock()
    response.url = "https://www.instagram.com/api/graphql"
    response.status = 200
    response.headers = {"content-type": "text/javascript"}
    response.json = AsyncMock(return_value={"data": profile()})

    async def navigate(*args, **kwargs):
        page.on.call_args.args[1](response)

    page.goto = navigate
    browser = PlaywrightFollowerBrowser(page, clock=lambda: NOW, timeout_ms=100)
    result = await browser.inspect("prospect")
    assert result["owner_id"] == "123"
    assert result["has_story"] is True
    page.remove_listener.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,reason", [
    ({"message": "csrf_failed"}, "response_failed"),
    ({"status": "ok", "data": profile()}, "response_failed"),
    ({"message": "login_required"}, "authentication_required"),
    ({"require_login": True}, "authentication_required"),
    ({"challenge": {"url": "hidden"}}, "checkpoint_required"),
    ({"message": "Please wait a few minutes"}, "rate_limited"),
    (None, "response_failed"),
])
async def test_forbidden_read_needs_explicit_account_restriction(payload, reason):
    response = MagicMock(status=403, json=AsyncMock(return_value=payload))
    with pytest.raises(FollowerBrowserError) as error:
        await PlaywrightFollowerBrowser._read_payload(response)
    assert error.value.reason == reason


@pytest.mark.asyncio
async def test_non_json_forbidden_read_is_not_a_login_failure():
    response = MagicMock(status=403, json=AsyncMock(side_effect=ValueError("HTML refusal")))
    with pytest.raises(FollowerBrowserError, match="^response_failed$"):
        await PlaywrightFollowerBrowser._read_payload(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"message": "login_required"}, {"require_login": True}])
async def test_explicit_forbidden_login_stops_other_successful_profile(payload):
    page = MagicMock(url="https://www.instagram.com/prospect/")
    page.locator.return_value.count = AsyncMock(return_value=0)
    failed = MagicMock(url="https://www.instagram.com/api/graphql", status=403,
                       json=AsyncMock(return_value=payload))
    valid = MagicMock(url=failed.url, status=200, json=AsyncMock(return_value={"data": profile()}))

    async def navigate(*args, **kwargs):
        page.on.call_args.args[1](failed)
        page.on.call_args.args[1](valid)

    page.goto = navigate
    with pytest.raises(FollowerBrowserError, match="^authentication_required$"):
        await PlaywrightFollowerBrowser(page, timeout_ms=100).inspect("prospect")
    page.remove_listener.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,reason", [
    ("/accounts/login/", "authentication_required"),
    ("/challenge/", "checkpoint_required"),
    ("/prospect/", "response_failed"),
])
async def test_navigation_forbidden_preserves_explicit_page_restriction(path, reason):
    page = MagicMock(url="https://www.instagram.com" + path)
    page.goto = AsyncMock(return_value=MagicMock(status=403))
    page.locator.return_value.count = AsyncMock(return_value=0)
    with pytest.raises(FollowerBrowserError, match="^" + reason + "$"):
        await PlaywrightFollowerBrowser(page, timeout_ms=100).inspect("prospect")
    page.remove_listener.assert_called_once()


@pytest.mark.asyncio
async def test_checkpoint_returns_typed_reason_and_removes_listener():
    page = MagicMock()
    page.url = "https://www.instagram.com/challenge/"
    page.goto = AsyncMock(return_value=None)
    browser = PlaywrightFollowerBrowser(page, timeout_ms=100)
    with pytest.raises(FollowerBrowserError, match="checkpoint_required"):
        await browser.inspect("prospect")
    page.remove_listener.assert_called_once()


@pytest.mark.asyncio
async def test_partial_feed_profile_cannot_win_response_race():
    page = MagicMock()
    page.url = "https://www.instagram.com/prospect/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    partial = MagicMock()
    partial.url = "https://www.instagram.com/graphql/query"
    partial.status = 200
    partial.json = AsyncMock(return_value={"data": {"id": "123", "username": "prospect", "is_private": False,
                                                  "friendship_status": {"following": True}}})
    full = MagicMock()
    full.url = "https://www.instagram.com/api/graphql"
    full.status = 200
    full.json = AsyncMock(return_value={"data": profile()})

    async def navigate(*args, **kwargs):
        callback = page.on.call_args.args[1]
        callback(partial)
        callback(full)

    page.goto = navigate
    browser = PlaywrightFollowerBrowser(page, clock=lambda: NOW, timeout_ms=100)
    result = await browser.inspect("prospect")
    assert result["following"] is False
    assert result["followed_by"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload", [(200, {"errors": [{"message": "notification unavailable"}]}),
                                          (500, {"status": "fail"}), (200, []),
                                          (403, {"message": "csrf_failed"}),
                                          (403, {"data": profile()})])
async def test_unrelated_graphql_failure_does_not_fail_matching_profile(status, payload):
    page = MagicMock()
    page.url = "https://www.instagram.com/prospect/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    failed = MagicMock()
    failed.url = "https://www.instagram.com/api/graphql"
    failed.status = status
    failed.json = AsyncMock(return_value=payload)
    full = MagicMock()
    full.url = "https://www.instagram.com/api/graphql"
    full.status = 200
    full.json = AsyncMock(return_value={"data": profile()})

    async def navigate(*args, **kwargs):
        callback = page.on.call_args.args[1]
        callback(failed)
        callback(full)

    page.goto = navigate
    browser = PlaywrightFollowerBrowser(page, clock=lambda: NOW, timeout_ms=100)
    assert (await browser.inspect("prospect"))["owner_id"] == "123"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload", [(429, {}), (200, {"message": "Please wait a few minutes"}),
                                          (403, {"message": "Please wait a few minutes"})])
async def test_global_rate_limit_stops_profile_capture(status, payload):
    page = MagicMock()
    page.url = "https://www.instagram.com/prospect/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    limited = MagicMock()
    limited.url = "https://www.instagram.com/api/graphql"
    limited.status = status
    limited.json = AsyncMock(return_value=payload)
    full = MagicMock()
    full.url = "https://www.instagram.com/api/graphql"
    full.status = 200
    full.json = AsyncMock(return_value={"data": profile()})

    async def navigate(*args, **kwargs):
        callback = page.on.call_args.args[1]
        callback(limited)
        callback(full)

    page.goto = navigate
    browser = PlaywrightFollowerBrowser(page, clock=lambda: NOW, timeout_ms=100)
    with pytest.raises(FollowerBrowserError, match="rate_limited"):
        await browser.inspect("prospect")
    page.remove_listener.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload", [
    (200, {"errors": [{"message": "profile unavailable"}]}),
    (403, {"data": profile()}),
    (403, {"message": "csrf_failed"}),
])
async def test_failed_target_query_without_profile_times_out_closed(status, payload):
    page = MagicMock()
    page.url = "https://www.instagram.com/prospect/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    failed = MagicMock()
    failed.url = "https://www.instagram.com/api/graphql"
    failed.status = status
    failed.json = AsyncMock(return_value=payload)

    async def navigate(*args, **kwargs):
        page.on.call_args.args[1](failed)

    page.goto = navigate
    browser = PlaywrightFollowerBrowser(page, clock=lambda: NOW, timeout_ms=100)
    with pytest.raises(FollowerBrowserError, match="profile_unreadable"):
        await browser.inspect("prospect")
    page.remove_listener.assert_called_once()


def public_node(**changes):
    node = profile(full_name="Public Name", biography="About this creator", following_count=120,
                   is_verified=False, private_notes="must not leave browser memory")
    node.update(changes)
    return node


def test_public_mapper_returns_exact_whitelist_and_real_metrics():
    assert parse_public_profile({"data": public_node()}, "prospect", owner_id="123") == {
        "username": "prospect", "fullName": "Public Name", "biography": "About this creator",
        "followersCount": 1600, "followingCount": 120, "postsCount": 50,
        "isPrivate": False, "isVerified": False, "latestPosts": [],
    }


@pytest.mark.parametrize("owner,handle", [("456", "prospect"), ("123", "other")])
def test_public_mapper_requires_expected_identity(owner, handle):
    with pytest.raises(FollowerBrowserError, match="profile_unreadable"):
        parse_public_profile({"data": public_node()}, handle, owner_id=owner)


@pytest.mark.parametrize("key", ["follower_count", "following_count", "media_count", "is_verified", "is_private", "full_name", "biography"])
def test_missing_public_fields_are_not_invented(key):
    node = public_node()
    del node[key]
    with pytest.raises(FollowerBrowserError, match="profile_unreadable"):
        parse_public_profile({"data": node}, "prospect")


@pytest.mark.parametrize("value", [None, True, "123", 1.0, -1, 2_147_483_648])
@pytest.mark.parametrize("key", ["follower_count", "following_count", "media_count"])
def test_public_counts_are_strict_bounded_integers(key, value):
    with pytest.raises(FollowerBrowserError, match="profile_unreadable"):
        parse_public_profile({"data": public_node(**{key: value})}, "prospect")


@pytest.mark.parametrize("key,value", [("is_private", 0), ("is_verified", "false"), ("full_name", {}), ("biography", [])])
def test_public_text_and_flags_do_not_coerce_shapes(key, value):
    with pytest.raises(FollowerBrowserError, match="profile_unreadable"):
        parse_public_profile({"data": public_node(**{key: value})}, "prospect")


def test_public_text_is_sanitized_and_bounded_for_ui():
    result = parse_public_profile({"data": public_node(full_name="A\x00\x1b\x7f\x85\u202eB", biography="Line 1\nLine 2\tvalue\r")}, "prospect")
    assert result["fullName"] == "AB"
    assert result["biography"] == "Line 1\nLine 2\tvalue\r"
    result = parse_public_profile({"data": public_node(full_name="🙂" * 300, biography="x" * 2400)}, "prospect")
    assert result["fullName"] == "🙂" * 128
    assert len(result["biography"]) == 2200


def test_public_mapper_ignores_partial_other_users_and_rejects_conflicting_counts():
    payload = {"data": [public_node(), {"username": "prospect", "id": "123", "follower_count": 1}, public_node(username="other", pk="456", id="456")]}
    assert parse_public_profile(payload, "prospect")["followersCount"] == 1600
    with pytest.raises(FollowerBrowserError, match="profile_unreadable"):
        parse_public_profile({"data": [public_node(), public_node(following_count=300)]}, "prospect")


@pytest.mark.asyncio
async def test_public_profile_reuses_inspect_page_response_without_second_request():
    page = MagicMock()
    page.url = "https://www.instagram.com/prospect/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    response = MagicMock()
    response.url = "https://www.instagram.com/api/graphql"
    response.status = 200
    response.json = AsyncMock(return_value={"data": public_node()})

    async def navigate(*args, **kwargs):
        page.on.call_args.args[1](response)

    page.goto = AsyncMock(side_effect=navigate)
    browser = PlaywrightFollowerBrowser(page, clock=lambda: NOW, timeout_ms=100)
    result = await browser.public_profile("prospect")
    assert result["followersCount"] == 1600
    assert result["username"] == "prospect"
    page.goto.assert_awaited_once()
    page.context.request.get.assert_not_called()
    response.json.assert_awaited_once()
    result["latestPosts"].append({"injected": True})
    assert browser._latest_public_profile[2]["latestPosts"] == []


@pytest.mark.asyncio
async def test_public_profile_cannot_reuse_stale_metrics_after_missing_fields():
    page = MagicMock()
    page.url = "https://www.instagram.com/prospect/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    response = MagicMock()
    response.url = "https://www.instagram.com/api/graphql"
    response.status = 200
    response.json = AsyncMock(side_effect=[{"data": public_node()}, {"data": profile()}])

    async def navigate(*args, **kwargs):
        page.on.call_args.args[1](response)

    page.goto = navigate
    browser = PlaywrightFollowerBrowser(page, clock=lambda: NOW, timeout_ms=100)
    assert (await browser.public_profile("prospect"))["followersCount"] == 1600
    with pytest.raises(FollowerBrowserError, match="profile_unreadable"):
        await browser.public_profile("prospect")
    assert browser._latest_public_profile is None
