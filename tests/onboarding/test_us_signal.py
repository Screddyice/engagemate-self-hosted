"""The US-location gate, which decides whether a candidate is engaged at all.

`accepted` requires `us_signal`, so a miss here is not a lower score -- the
candidate is dropped and never reaches the operator. The first real candidates
this gate ever saw were all dropped: HyperCrawl-backed discovery started
returning profiles, and every one of them, including a Brooklyn roastery with
27k followers whose bio says "three guys from Brooklyn", scored no US signal.

Three defects caused it, all fixed here:
  * Brooklyn (and Manhattan, Queens, Philadelphia, San Diego...) were not listed.
  * `location` was half the search text and HyperCrawl's profile has no such
    field, so it was always the empty string.
  * The handle was never read, so "examplecoffeenyc" announced its city in
    the one place nothing looked.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

import pytest

from engagemate.onboarding.brand import score_candidate


def _profile(username: str, biography: str = "", full_name: str = "") -> dict:
    # Followers and posts are set well clear of their thresholds so these tests
    # isolate the US signal and nothing else.
    return {
        "username": username,
        "biography": biography,
        "fullName": full_name,
        "followersCount": 5_000,
        "postsCount": 50,
    }


@pytest.mark.parametrize(
    "username,biography",
    [
        # The real profile that exposed the gap.
        ("brooklynexampleroasters", "We're three guys from Brooklyn who make great coffee"),
        ("bk_roast", "Roasting in Manhattan since 2011"),
        ("queenscoffee", "Queens, all day"),
        ("phillyroast", "Philadelphia small batch"),
        ("sdbeans", "San Diego coffee"),
        ("plainname", "Proudly American"),
    ],
)
def test_a_us_marker_anywhere_in_the_text_is_accepted(username, biography):
    score = score_candidate(_profile(username, biography))
    assert score.us_signal is True
    assert score.accepted is True


def test_the_handle_alone_can_carry_the_signal():
    # No spaces in a handle, so the word-boundary pattern cannot see "nyc" here.
    score = score_candidate(_profile("examplecoffeenyc", "specialty coffee"))
    assert score.us_signal is True


def test_the_display_name_alone_can_carry_the_signal():
    score = score_candidate(_profile("roastco", "great beans", full_name="Roast Co Austin"))
    assert score.us_signal is True


@pytest.mark.parametrize(
    "username,biography",
    [
        ("someukroaster", "Coffee in Manchester UK"),
        ("berlinbeans", "Kreuzberg roastery"),
        # Substring traps. These are why the handle list omits la / sf / chi:
        # a FALSE us_signal accepts an account the operator then pays to engage,
        # which is worse than missing one.
        ("lagerlovers", "German beer since 1890"),
        ("chickenshop", "fried chicken"),
        ("sfogliatella_bakery", "Napoli pastries"),
        ("blackboard", "menus"),
    ],
)
def test_a_non_us_account_is_not_accepted(username, biography):
    score = score_candidate(_profile(username, biography))
    assert score.us_signal is False
    assert score.accepted is False


def test_the_gate_still_requires_reach_and_activity():
    # A US signal alone must not accept a tiny or inactive account.
    thin = {"username": "brooklyntiny", "biography": "Brooklyn", "followersCount": 100, "postsCount": 50}
    assert score_candidate(thin).accepted is False
    quiet = {"username": "brooklynquiet", "biography": "Brooklyn", "followersCount": 5_000, "postsCount": 2}
    assert score_candidate(quiet).accepted is False
    private = {
        "username": "brooklynshy", "biography": "Brooklyn",
        "followersCount": 5_000, "postsCount": 50, "isPrivate": True,
    }
    assert score_candidate(private).accepted is False
