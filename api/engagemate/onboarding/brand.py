"""Pure validation and scoring for brand onboarding discovery results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from engagemate.growth.metrics import post_engagement_ratio


_USERNAME = re.compile(r"^[a-z0-9._]{1,64}$")
# US markers. Boroughs and second-tier metros matter as much as the headline
# cities: the first real candidate this gate ever saw was "brooklynexampleroasters",
# whose bio says "three guys from Brooklyn who make great coffee" and which was
# rejected as having no US signal because Brooklyn was not listed.
_US_SIGNAL = re.compile(
    r"\b(?:usa|u\.s\.a?\.?|united states|america|american|"
    # NYC and its boroughs — a Brooklyn or Queens business rarely writes "New York".
    r"new york|nyc|brooklyn|manhattan|queens|bronx|staten island|"
    r"los angeles|austin|chicago|miami|atlanta|nashville|dallas|houston|"
    r"seattle|boston|denver|phoenix|san francisco|portland|orlando|"
    r"philadelphia|philly|san diego|las vegas|minneapolis|detroit|"
    r"charlotte|columbus|indianapolis|kansas city|st\.? louis|"
    r"new orleans|salt lake city|sacramento|san jose|tampa|pittsburgh|"
    r"california|texas|florida|colorado|oregon|washington|"
    r"\bla\b|\bsf\b|\bpdx\b|\batx\b|\bchi\b)\b",
    re.IGNORECASE,
)

# Handles carry no spaces, so \b almost never fires inside one: "nyc" in
# "examplecoffeenyc" is mid-word and the main pattern cannot see it. This
# list is therefore matched WITHOUT word boundaries -- which is only safe for
# tokens that are not substrings of ordinary English. "la", "sf" and "chi" are
# deliberately absent: they would match lager, sfogliatella and chicken, and a
# false US signal is worse than a missed one, because it ACCEPTS an account the
# operator is paying to engage.
_US_SIGNAL_IN_HANDLE = re.compile(
    r"(?:usa|nyc|brooklyn|manhattan|queens|bronx|philly|philadelphia|"
    r"losangeles|sandiego|sanfrancisco|newyork|austin|chicago|miami|atlanta|"
    r"nashville|dallas|houston|seattle|boston|denver|portland|pdx|atx)",
    re.IGNORECASE,
)


def canonicalize_reference_url(value: str) -> str:
    """Accept only an Instagram public profile URL and discard tracking data."""

    if not isinstance(value, str):
        raise ValueError("reference URL must be a string")
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"instagram.com", "www.instagram.com"}:
        raise ValueError("reference URL must be an HTTPS Instagram profile")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1 or not _USERNAME.fullmatch(parts[0].lower()):
        raise ValueError("reference URL must point to an Instagram profile")
    return urlunsplit(("https", "www.instagram.com", f"/{parts[0].lower()}/", "", ""))


MIN_REFERENCE_ACCOUNTS = 3
MAX_REFERENCE_ACCOUNTS = 5


def canonicalize_reference_urls(values: list[str]) -> list[str]:
    """Canonicalize three to five distinct Instagram audience sources.

    Duplicate submissions are harmless input noise, not a validation error.
    Keep the first occurrence so operator-entered order is preserved, then
    enforce the minimum and maximum against the usable unique accounts.
    """

    if not isinstance(values, list):
        raise ValueError("reference URLs must be a list")
    canonical: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = canonicalize_reference_url(value)
        if url not in seen:
            seen.add(url)
            canonical.append(url)
    if len(canonical) < MIN_REFERENCE_ACCOUNTS:
        raise ValueError(
            f"at least {MIN_REFERENCE_ACCOUNTS} reference accounts are required"
        )
    if len(canonical) > MAX_REFERENCE_ACCOUNTS:
        raise ValueError(
            f"at most {MAX_REFERENCE_ACCOUNTS} reference accounts are allowed"
        )
    return canonical


def reference_username(url: str) -> str:
    """Return the profile handle for a canonical reference URL."""

    return urlsplit(url).path.strip("/")


def align_profiles(
    reference_urls: list[str], profiles: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Pair each reference URL with its own scrape, whatever order it arrived in.

    A scraper may reorder results or drop an account it could not read, so
    position alone would attach one account's data to another's URL.
    """

    by_username: dict[str, dict[str, object]] = {}
    for profile in profiles:
        username = str(profile.get("username") or "").strip().lower()
        if username and username not in by_username:
            by_username[username] = profile
    return [by_username.get(reference_username(url), {}) for url in reference_urls]


def _number(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


@dataclass(frozen=True, slots=True)
class CandidateScore:
    username: str
    score: int
    accepted: bool
    us_signal: bool
    engagement_rate: float
    reasons: tuple[str, ...]


def score_candidate(profile: dict[str, object]) -> CandidateScore:
    """Score public candidates without claiming location or authenticity certainty."""

    username = str(profile.get("username") or "").strip().lower()
    followers = _number(profile.get("followersCount", profile.get("followers")))
    posts = _number(profile.get("postsCount", profile.get("posts")))
    biography = str(profile.get("biography") or profile.get("bio") or "")
    location = str(profile.get("location") or "")
    # Search the display name and handle too, not just the bio.
    #
    # `location` is dead weight on the HyperCrawl path: its discover profile
    # carries username / fullName / biography / counts / isPrivate / isVerified
    # and NO location at all, so half the original signal source was always the
    # empty string. Meanwhile "examplecoffeenyc" announces its city in the
    # handle and scored no US signal, because the handle was never read.
    full_name = str(profile.get("fullName") or profile.get("full_name") or "")
    us_signal = bool(
        _US_SIGNAL.search(f"{biography} {location} {full_name}")
        or _US_SIGNAL_IN_HANDLE.search(username)
    )
    engagement_rate = post_engagement_ratio(
        profile.get("latestPosts", profile.get("postsData")), followers
    ) or 0.0
    reasons: list[str] = []
    if followers < 500:
        reasons.append("fewer_than_500_followers")
    if profile.get("isPrivate") is True:
        reasons.append("private_profile")
    if profile.get("joinedRecently") is True:
        reasons.append("recently_joined")
    if not us_signal:
        reasons.append("no_public_us_location_signal")
    if posts < 6:
        reasons.append("low_post_count")

    score = 0
    score += 25 if followers >= 500 else 0
    score += 20 if followers >= 1_000 else 0
    score += 20 if us_signal else 0
    score += 20 if engagement_rate >= 0.01 else 10 if engagement_rate >= 0.003 else 0
    score += 15 if posts >= 6 and profile.get("joinedRecently") is not True else 0
    accepted = followers >= 500 and profile.get("isPrivate") is not True and us_signal and posts >= 6
    return CandidateScore(username, min(score, 100), accepted, us_signal, engagement_rate, tuple(reasons))
