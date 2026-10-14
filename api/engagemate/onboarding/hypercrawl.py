"""Client for the internal HyperCrawl REST capability.

Started as Instagram discovery only. HyperCrawl's `/v1` facade also exposes the
generic acquisition ladder (`/v1/acquire`), a plain fetch (`/v1/crawl`) and SERP
search (`/v1/search`), and those are wired here so EngageMate reaches them
through one authenticated client rather than growing a second HTTP path.

WHY `acquire` RAISES ON `blocked` — read before "simplifying" it. HyperCrawl's
ladder escalates through cheap tiers first and returns whatever the last tier
produced, with `blocked` reporting whether the page actually refused it. A
blocked result still carries `markdown`, so treating the response as success
yields a plausible-looking payload that is really a login wall. That failure has
already bitten this system twice: the story tray reported "this target posted
nothing today" for every unreadable page, and HyperCrawl's own ladder returned
tier-1 markdown for authwalled Instagram and looked like it was working. Both
were silent. So a blocked acquisition is an exception here, never a return
value, and the tier that served is carried on the result so a caller can tell a
cheap fetch from a paid browser run.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

from engagemate.onboarding.brand import reference_username


class DiscoveryError(RuntimeError):
    """A bounded public-scrape failure, safe to expose to the web tier.

    Was DiscoveryError, in a module that no longer exists. HyperCrawl fully
    replaced Apify; the name outlived the vendor and every caller that handled a
    discovery failure had to import from `onboarding.apify` to do it.
    """


# Bounded on purpose: each candidate costs one browser run through a paid
# residential proxy (see candidate_profiles). Raising this raises the bill.
_CANDIDATE_LIMIT = 12

_INSTAGRAM_PROFILE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?$"
)

# Paths Instagram serves under the same shape as a profile but which are not
# accounts. Fetching them burns proxy budget and returns nothing scoreable.
_NON_PROFILE_PATHS = frozenset(
    {"p", "reel", "reels", "explore", "stories", "accounts", "about", "developer", "directory"}
)


class HyperCrawlError(DiscoveryError):
    """A HyperCrawl call failed.

    Subclasses DiscoveryError so a caller that handles "retrieval did not work"
    catches every route with one clause, while code that cares specifically
    about a generic-capability failure can still narrow to this.
    """


class HyperCrawlBlockedError(HyperCrawlError):
    """The page refused the acquisition.

    Separate from a transport failure because the remedy differs: a blocked page
    needs an authenticated session or a different egress, not a retry.
    """


@dataclass(frozen=True)
class AcquiredPage:
    """One `/v1/acquire` result.

    `via` names the tier that served (a cheap fetch vs a browser run through a
    paid proxy) and `cost_usd` is what it spent. Both are carried rather than
    dropped so a caller can attribute spend and tell degraded output from good
    output -- the ladder returning cheap markdown for a page that needed a
    browser is exactly the failure that used to pass unnoticed.
    """

    url: str
    via: str
    cost_usd: float
    data: Any | None = None
    markdown: str | None = None


class HyperCrawlInstagramClient:
    def __init__(self, *, http: httpx.AsyncClient, base_url: str, rest_token: SecretStr | None, timeout_seconds: float) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._url = f"{self._base}/instagram/discover"
        self._token = rest_token.get_secret_value() if rest_token else None
        self._timeout = timeout_seconds

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        return headers

    async def _post(self, path: str, payload: dict[str, Any], what: str) -> dict[str, Any]:
        """POST one facade route and return its decoded object.

        Every failure mode becomes a HyperCrawlError naming `what`, because the
        alternative -- letting an httpx error, a non-200, or a malformed body
        each surface differently -- is how a caller ends up handling only the
        shape it happened to see first.
        """

        try:
            response = await self._http.post(
                f"{self._base}{path}",
                headers=self._headers(),
                json=payload,
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            raise HyperCrawlError(f"HyperCrawl {what} is unavailable") from error
        if response.status_code != 200:
            detail = ""
            try:
                body = response.json()
                if isinstance(body, dict) and isinstance(body.get("error"), str):
                    detail = f": {body['error']}"
            except ValueError:
                detail = ""
            raise HyperCrawlError(f"HyperCrawl {what} failed ({response.status_code}){detail}")
        try:
            body = response.json()
        except ValueError as error:
            raise HyperCrawlError(f"HyperCrawl returned invalid {what} data") from error
        if not isinstance(body, dict):
            raise HyperCrawlError(f"HyperCrawl returned invalid {what} data")
        return body

    async def crawl(self, url: str, fmt: str = "markdown") -> str:
        """Fetch one page as markdown (or html). No escalation ladder."""

        body = await self._post("/crawl", {"url": url, "format": fmt}, "crawl")
        content = body.get("markdown") if fmt == "markdown" else body.get("html")
        if not isinstance(content, str) or not content:
            raise HyperCrawlError(f"HyperCrawl crawl returned no {fmt} for {url}")
        return content

    async def acquire(self, url: str, intent: str | None = None) -> AcquiredPage:
        """Run the escalation ladder against one URL.

        Raises HyperCrawlBlockedError when the page refused, rather than
        returning the degraded payload the ladder still produces. See the module
        docstring for why that is not negotiable.
        """

        payload: dict[str, Any] = {"url": url}
        if intent is not None:
            payload["intent"] = intent
        body = await self._post("/acquire", payload, "acquire")
        if body.get("blocked") is True:
            raise HyperCrawlBlockedError(
                f"HyperCrawl was blocked acquiring {url}"
                f" (tier {body.get('via') or 'unknown'})"
            )
        markdown = body.get("markdown")
        data = body.get("data")
        if data is None and not isinstance(markdown, str):
            raise HyperCrawlError(f"HyperCrawl acquire returned neither data nor markdown for {url}")
        raw_cost = body.get("costUsd")
        return AcquiredPage(
            url=url,
            via=str(body.get("via") or "unknown"),
            cost_usd=float(raw_cost) if isinstance(raw_cost, (int, float)) else 0.0,
            data=data,
            markdown=markdown if isinstance(markdown, str) else None,
        )

    async def candidate_profiles(
        self,
        search_terms: list[str],
        limit: int = _CANDIDATE_LIMIT,
        exclude: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """Find candidate Instagram profiles for the given terms.

        WHY THIS EXISTS. `/v1/instagram/discover` returns `candidates: []`,
        always -- the provider exposes a profile and its posts but no Instagram
        *search*. That is an honest answer from HyperCrawl, but it made
        onboarding silently produce zero candidates whenever HyperCrawl was
        configured, while the deprecated Apify path still returned some. Nothing
        errored; the list was just empty.

        So candidates are assembled here instead: SERP search for profile URLs,
        then one profile fetch each. The second half is why `limit` is small and
        explicit. `score_candidate` needs followers, posts, biography and
        privacy -- none of which a search result carries -- so every candidate
        costs one real browser run through a paid residential proxy. This is the
        one place in the client that spends money proportional to its argument,
        and the cap is the guard.

        Partial failures thin the list. If none of the discovered profiles can
        be read, raise so onboarding records a failed discovery and offers a retry.
        """

        if limit <= 0 or not search_terms:
            return []
        rows = await self.search(" ".join(search_terms) + " site:instagram.com")
        handles: list[str] = []
        for row in rows:
            url = row.get("url")
            if not isinstance(url, str):
                continue
            match = _INSTAGRAM_PROFILE.match(url.strip())
            if not match:
                continue
            handle = match.group(1).lower()
            # Instagram serves these paths as profiles but they are not people.
            if handle in _NON_PROFILE_PATHS or handle in exclude or handle in handles:
                continue
            handles.append(handle)
            if len(handles) >= limit:
                break
        if not handles:
            return []
        fetched = await asyncio.gather(
            *(self.discover(f"https://www.instagram.com/{h}/", [], 1) for h in handles),
            return_exceptions=True,
        )
        profiles: list[dict[str, Any]] = []
        for result in fetched:
            if isinstance(result, BaseException):
                continue
            profile, _ = result
            if isinstance(profile, dict) and profile.get("username"):
                profiles.append(profile)
        if not profiles:
            raise HyperCrawlError("HyperCrawl could not read any candidate profiles")
        return profiles

    async def search(self, query: str, provider: str | None = None) -> list[dict[str, Any]]:
        """SERP search. Returns the result rows, or [] when there are none."""

        payload: dict[str, Any] = {"query": query}
        if provider is not None:
            payload["provider"] = provider
        body = await self._post("/search", payload, "search")
        results = body.get("results")
        if not isinstance(results, list):
            raise HyperCrawlError("HyperCrawl returned invalid search data")
        return [row for row in results if isinstance(row, dict)]

    async def profiles(self, reference_urls: list[str]) -> list[dict[str, Any]]:
        """Retrieve each example account, skipping the candidate search.

        Concurrent, not sequential. Each of these is a real browser page load
        through a residential proxy at roughly seven seconds, and onboarding
        accepts up to five reference accounts, so awaiting them one at a time
        added half a minute to a request that a person is sitting and watching.
        They are independent fetches; nothing ordered them.
        """

        results = await asyncio.gather(
            *(self.discover(url, [], 1) for url in reference_urls)
        )
        return [profile for profile, _ in results]

    async def discover_many(
        self,
        reference_urls: list[str],
        search_terms: list[str],
        limit: int = 25,
    ) -> tuple[
        list[dict[str, Any]],
        list[tuple[str, list[dict[str, Any]]]],
    ]:
        """Discover candidates for every audience source as one operation.

        DEPRECATED for onboarding, and no longer on that path. Its `groups` are
        always empty -- /instagram/discover has no Instagram search and returns
        `candidates: []` by design -- while its `profiles` half re-fetches every
        reference account the caller has usually just fetched. In the onboarding
        route that meant one redundant paid browser run per reference, for a
        list that could not contain anything. Use `profiles()` for the accounts
        and `candidate_profiles()` for the candidates.
        """

        results = await asyncio.gather(
            *(
                self.discover(reference_url, search_terms, limit)
                for reference_url in reference_urls
            )
        )
        profiles = [profile for profile, _ in results]
        groups = [
            (reference_username(reference_url), candidates)
            for reference_url, (_, candidates) in zip(reference_urls, results)
        ]
        return profiles, groups

    async def discover(self, reference_url: str, search_terms: list[str], limit: int = 25) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        headers = {"content-type": "application/json"}
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        try:
            response = await self._http.post(
                self._url,
                headers=headers,
                json={"referenceUrl": reference_url, "searchTerms": search_terms, "limit": limit},
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            raise DiscoveryError("HyperCrawl Instagram discovery is unavailable") from error
        if response.status_code != 200:
            raise DiscoveryError("HyperCrawl Instagram discovery failed")
        try:
            body = response.json()
        except ValueError as error:
            raise DiscoveryError("HyperCrawl returned invalid discovery data") from error
        profile = body.get("profile") if isinstance(body, dict) else None
        candidates = body.get("candidates") if isinstance(body, dict) else None
        if not isinstance(profile, dict) or not isinstance(candidates, list):
            raise DiscoveryError("HyperCrawl returned invalid discovery data")
        return profile, [row for row in candidates if isinstance(row, dict)]
