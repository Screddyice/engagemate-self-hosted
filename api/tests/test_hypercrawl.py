import httpx
import pytest
from pydantic import SecretStr

from engagemate.onboarding.hypercrawl import (
    HyperCrawlBlockedError,
    HyperCrawlError,
    HyperCrawlInstagramClient,
)


@pytest.mark.anyio
async def test_hypercrawl_client_calls_internal_rest_capability():
    seen = {}

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen["request"] = request
            return httpx.Response(200, json={"profile": {"username": "reference"}, "candidates": [{"username": "creator"}]}, request=request)

    async with httpx.AsyncClient(transport=Transport()) as http:
        client = HyperCrawlInstagramClient(http=http, base_url="http://hypercrawl:9967", rest_token=SecretStr("rest-token"), timeout_seconds=10)
        profile, candidates = await client.discover("https://www.instagram.com/reference/", ["founders"])
    assert profile["username"] == "reference"
    assert candidates[0]["username"] == "creator"
    assert seen["request"].url.path == "/instagram/discover"
    assert seen["request"].headers["authorization"] == "Bearer rest-token"


class _Router(httpx.AsyncBaseTransport):
    """Serve a canned response per path and record what was asked for."""

    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.seen: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.seen.append(request)
        response = self.routes[request.url.path]
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
        )


def _client(transport, http):
    return HyperCrawlInstagramClient(
        http=http, base_url="http://hypercrawl:9967", rest_token=SecretStr("rest-token"), timeout_seconds=10
    )


@pytest.mark.anyio
async def test_crawl_returns_markdown_and_authenticates():
    t = _Router({"/crawl": httpx.Response(200, json={"url": "u", "markdown": "# hi"})})
    async with httpx.AsyncClient(transport=t) as http:
        assert await _client(t, http).crawl("https://example.com") == "# hi"
    assert t.seen[0].url.path == "/crawl"
    assert t.seen[0].headers["authorization"] == "Bearer rest-token"


@pytest.mark.anyio
async def test_acquire_carries_the_tier_and_cost():
    t = _Router({"/acquire": httpx.Response(200, json={"markdown": "body", "via": "tier3-dom-agent", "costUsd": 0.42, "blocked": False})})
    async with httpx.AsyncClient(transport=t) as http:
        page = await _client(t, http).acquire("https://example.com", intent="data")
    assert page.via == "tier3-dom-agent"
    assert page.cost_usd == 0.42
    assert page.markdown == "body"


@pytest.mark.anyio
async def test_acquire_raises_when_blocked_instead_of_returning_degraded_output():
    """The whole point. A blocked page still ships markdown; returning it
    silently is how an authwall reads as a successful fetch."""

    t = _Router({"/acquire": httpx.Response(200, json={"markdown": "Log in to continue", "via": "tier1-fetch", "costUsd": 0.0, "blocked": True})})
    async with httpx.AsyncClient(transport=t) as http:
        with pytest.raises(HyperCrawlBlockedError) as caught:
            await _client(t, http).acquire("https://instagram.com/x/")
    # The tier is named, so an operator can tell "cheap fetch refused" from
    # "the paid browser run also refused".
    assert "tier1-fetch" in str(caught.value)


@pytest.mark.anyio
async def test_acquire_rejects_a_response_carrying_neither_data_nor_markdown():
    t = _Router({"/acquire": httpx.Response(200, json={"via": "tier1", "costUsd": 0.0, "blocked": False})})
    async with httpx.AsyncClient(transport=t) as http:
        with pytest.raises(HyperCrawlError):
            await _client(t, http).acquire("https://example.com")


@pytest.mark.anyio
async def test_acquire_accepts_structured_data_with_no_markdown():
    t = _Router({"/acquire": httpx.Response(200, json={"data": {"title": "T"}, "via": "tier2", "costUsd": 0.01, "blocked": False})})
    async with httpx.AsyncClient(transport=t) as http:
        page = await _client(t, http).acquire("https://example.com", intent="data")
    assert page.data == {"title": "T"}
    assert page.markdown is None


@pytest.mark.anyio
async def test_search_returns_rows_and_drops_non_objects():
    t = _Router({"/search": httpx.Response(200, json={"results": [{"url": "a"}, "junk", {"url": "b"}]})})
    async with httpx.AsyncClient(transport=t) as http:
        rows = await _client(t, http).search("query", provider="serp")
    assert [r["url"] for r in rows] == ["a", "b"]


@pytest.mark.anyio
async def test_a_non_200_surfaces_the_facade_error_text():
    t = _Router({"/acquire": httpx.Response(401, json={"error": "missing API key"})})
    async with httpx.AsyncClient(transport=t) as http:
        with pytest.raises(HyperCrawlError) as caught:
            await _client(t, http).acquire("https://example.com")
    assert "401" in str(caught.value) and "missing API key" in str(caught.value)


@pytest.mark.anyio
async def test_a_transport_failure_becomes_a_hypercrawl_error():
    class Dead(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=Dead()) as http:
        client = HyperCrawlInstagramClient(
            http=http, base_url="http://hypercrawl:9967", rest_token=None, timeout_seconds=1
        )
        with pytest.raises(HyperCrawlError):
            await client.crawl("https://example.com")


@pytest.mark.anyio
async def test_candidate_profiles_searches_then_fetches_each_profile():
    """Closes the gap where /instagram/discover always returns candidates: []."""

    t = _Router({
        "/search": httpx.Response(200, json={"results": [
            {"url": "https://www.instagram.com/alpha/"},
            {"url": "https://instagram.com/beta"},
            {"url": "https://example.com/not-instagram"},
            {"url": "https://www.instagram.com/p/AbC123/"},      # a post, not a profile
            {"url": "https://www.instagram.com/alpha/"},          # duplicate
        ]}),
        "/instagram/discover": httpx.Response(200, json={"profile": {"username": "x", "followersCount": 900}, "candidates": []}),
    })
    async with httpx.AsyncClient(transport=t) as http:
        rows = await _client(t, http).candidate_profiles(["indie", "coffee"])
    # alpha and beta only: the non-instagram host, the /p/ post URL and the
    # duplicate are all dropped BEFORE any paid profile fetch.
    fetched = [r for r in t.seen if r.url.path == "/instagram/discover"]
    assert len(fetched) == 2
    assert len(rows) == 2


@pytest.mark.anyio
async def test_candidate_profiles_respects_its_cap_because_each_one_costs_a_browser_run():
    results = [{"url": f"https://www.instagram.com/user{i}/"} for i in range(50)]
    t = _Router({
        "/search": httpx.Response(200, json={"results": results}),
        "/instagram/discover": httpx.Response(200, json={"profile": {"username": "u"}, "candidates": []}),
    })
    async with httpx.AsyncClient(transport=t) as http:
        await _client(t, http).candidate_profiles(["term"], limit=5)
    assert len([r for r in t.seen if r.url.path == "/instagram/discover"]) == 5


@pytest.mark.anyio
async def test_candidate_profiles_excludes_the_reference_accounts():
    t = _Router({
        "/search": httpx.Response(200, json={"results": [
            {"url": "https://www.instagram.com/reference/"},
            {"url": "https://www.instagram.com/other/"},
        ]}),
        "/instagram/discover": httpx.Response(200, json={"profile": {"username": "other"}, "candidates": []}),
    })
    async with httpx.AsyncClient(transport=t) as http:
        await _client(t, http).candidate_profiles(["t"], exclude=frozenset({"reference"}))
    assert len([r for r in t.seen if r.url.path == "/instagram/discover"]) == 1


@pytest.mark.anyio
async def test_one_unreachable_candidate_thins_the_list_rather_than_failing_onboarding():
    class Flaky(httpx.AsyncBaseTransport):
        def __init__(self):
            self.n = 0

        async def handle_async_request(self, request):
            if request.url.path == "/search":
                return httpx.Response(200, json={"results": [
                    {"url": "https://www.instagram.com/good/"},
                    {"url": "https://www.instagram.com/bad/"},
                ]}, request=request)
            self.n += 1
            if self.n == 1:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"profile": {"username": "good"}, "candidates": []}, request=request)

    async with httpx.AsyncClient(transport=Flaky()) as http:
        client = HyperCrawlInstagramClient(
            http=http, base_url="http://hypercrawl:9967", rest_token=None, timeout_seconds=5
        )
        rows = await client.candidate_profiles(["t"])
    assert len(rows) == 1


@pytest.mark.anyio
async def test_candidate_profiles_spends_nothing_when_search_finds_no_profiles():
    t = _Router({"/search": httpx.Response(200, json={"results": [{"url": "https://example.com/x"}]})})
    async with httpx.AsyncClient(transport=t) as http:
        assert await _client(t, http).candidate_profiles(["t"]) == []
    assert not [r for r in t.seen if r.url.path == "/instagram/discover"]


@pytest.mark.anyio
@pytest.mark.parametrize("response", [
    httpx.Response(500, json={"error": "internal error"}),
    httpx.Response(200, json={"profile": {}, "candidates": []}),
])
async def test_no_readable_candidates_reports_failure_instead_of_empty_success(response):
    t = _Router({
        "/search": httpx.Response(200, json={"results": [
            {"url": "https://www.instagram.com/alpha/"},
            {"url": "https://www.instagram.com/beta/"},
        ]}),
        "/instagram/discover": response,
    })
    async with httpx.AsyncClient(transport=t) as http:
        with pytest.raises(HyperCrawlError, match="could not read any candidate profiles"):
            await _client(t, http).candidate_profiles(["coffee"])
