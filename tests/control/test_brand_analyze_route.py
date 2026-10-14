"""`POST /brand/analyze` end to end: HTTP in, rows in Postgres, candidates out.

Everything below this route had been exercised in isolation -- the HyperCrawl
client, the SERP escalation, scoring, the balancer -- but the ROUTE itself never
had a test, so "the pipeline works" and "onboarding works" were different
claims. This closes that gap.

Real: the FastAPI app, the route body, HyperCrawlInstagramClient, score_candidate,
select_balanced_candidates, and a real Postgres it actually writes to.
Faked: only the network. One transport answers both HyperCrawl and Hermes, so
the client code under test builds the requests and parses the replies for real.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.background import BackgroundTasks

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "api"))

from engagemate.config import Settings
from engagemate.control.dependencies import current_user_id
from engagemate.db.brand_profiles import BrandProfileRepository
from engagemate.db.onboarding_launch import OnboardingLaunchRepository
from engagemate.db.pool import create_pool
from main import create_app


def _settings(database_url: str, *, hypercrawl: bool) -> Settings:
    extra = (
        {
            "HYPERCRAWL_URL": "http://hypercrawl.test/v1",
            "HYPERCRAWL_REST_TOKEN": "hc_" + "0" * 48,
        }
        if hypercrawl
        else {}
    )
    return Settings(
        DATABASE_URL=database_url,
        SESSION_ENCRYPTION_KEY=base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="),
        INTERNAL_API_KEY="internal-test-key",
        # The endpoint guard requires a literal loopback/private IP, not a
        # hostname -- a hostname can be repointed after validation.
        HERMES_AGENT_URL="http://127.0.0.1:8646/v1/chat/completions",
        HERMES_AGENT_API_KEY="k" * 40,
        _env_file=None,
        **extra,
    )


SCHEMA_SQL = """
CREATE TABLE users (
    id uuid PRIMARY KEY,
    plan text NOT NULL DEFAULT 'starter',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ig_accounts (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username text NOT NULL,
    health_status text NOT NULL DEFAULT 'active',
    warmup_started_at timestamptz,
    paused_at timestamptz,
    pause_reason text,
    UNIQUE (id, user_id)
);
CREATE TABLE ig_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    captured_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz
);
CREATE TABLE proxies (
    id uuid PRIMARY KEY,
    health_status text NOT NULL DEFAULT 'active'
);
INSERT INTO proxies (id) VALUES ('00000000-0000-4000-8000-000000000001');
CREATE TABLE proxy_bindings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    proxy_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-000000000001'
        REFERENCES proxies(id),
    bound_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz
);
CREATE TABLE agent_configs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    enabled boolean NOT NULL DEFAULT false,
    timezone text NOT NULL DEFAULT 'UTC',
    schedule_hour integer NOT NULL DEFAULT 9,
    daily_like_limit integer NOT NULL DEFAULT 0,
    daily_reply_limit integer NOT NULL DEFAULT 0,
    daily_dm_limit integer NOT NULL DEFAULT 0,
    daily_view_limit integer NOT NULL DEFAULT 0,
    persona text,
    launched_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ig_account_id)
);
CREATE TABLE brand_profiles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid,
    reference_url text NOT NULL,
    reference_username text NOT NULL,
    onboarding_answers jsonb NOT NULL DEFAULT '{}'::jsonb,
    follower_audience_approved_at timestamptz,
    analysis jsonb,
    status text NOT NULL DEFAULT 'complete',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX brand_profiles_user_account_idx
    ON brand_profiles (user_id, ig_account_id);
CREATE TABLE brand_reference_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_profile_id uuid NOT NULL REFERENCES brand_profiles(id) ON DELETE CASCADE,
    username text NOT NULL,
    profile_url text NOT NULL,
    position integer NOT NULL,
    scraped jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX bra_profile_username_idx ON brand_reference_accounts (brand_profile_id, username);
CREATE UNIQUE INDEX bra_profile_position_idx ON brand_reference_accounts (brand_profile_id, position);
CREATE TABLE discovery_candidates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_profile_id uuid NOT NULL REFERENCES brand_profiles(id) ON DELETE CASCADE,
    username text NOT NULL,
    ig_user_id text DEFAULT 'fixture-target-id',
    profile_url text NOT NULL,
    follower_count integer,
    score integer NOT NULL,
    engagement_rate_bps integer NOT NULL DEFAULT 0,
    us_signal boolean NOT NULL DEFAULT false,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'suggested',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX discovery_candidates_profile_username_idx
    ON discovery_candidates (brand_profile_id, username);
CREATE TABLE targets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    username text NOT NULL,
    ig_user_id text DEFAULT 'fixture-target-id',
    tier text NOT NULL,
    source text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ig_account_id, username)
);
"""


def _profile(username: str, followers: int, bio: str) -> dict:
    """A HyperCrawl discover profile, in the shape the live service returns.

    Both count spellings are present because the real payload carries both, and
    scoring reads the plural form. No `location` key -- the live profile has
    none, which is exactly why the US signal now reads the handle and fullName.
    """
    return {
        "username": username,
        "id": str(int.from_bytes(username.encode(), "big")),
        "fullName": username,
        "biography": bio,
        "followerCount": followers,
        "followersCount": followers,
        "followers": followers,
        "postCount": 80,
        "postsCount": 80,
        "posts": 80,
        "isPrivate": False,
        "isVerified": False,
    }


@pytest.fixture(scope="module")
def database_url() -> str:
    database = f"engagemate_brand_{uuid.uuid4().hex[:12]}"
    subprocess.run(["createdb", database], check=True, capture_output=True)
    url = os.environ.get("TEST_DATABASE_URL_TEMPLATE", "postgresql:///%s") % database
    try:
        import psycopg

        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(SCHEMA_SQL)
        yield url
    finally:
        subprocess.run(["dropdb", "--if-exists", "--force", database], check=False, capture_output=True)


@pytest.fixture()
def harness(database_url: str, monkeypatch):
    """Wire the app with a transport that answers HyperCrawl and Hermes."""

    calls: dict[str, object] = {
        "discover": 0,
        "search": 0,
        "hermes": 0,
        "hermes_timeout": False,
        "candidate_failure": False,
        "profile_failure": False,
        "client_timeouts": [],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # HermesMessagesClient mints a session per decision and DELETEs it
        # afterwards, and requires an affirmative 200 {"deleted": true}: "no
        # error" and "I deleted it" are different claims, and only the second
        # is safe when the session may hold customer data.
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True, "id": path.rsplit("/", 1)[-1]})
        if path.endswith("/instagram/discover"):
            calls["discover"] += 1
            if calls["profile_failure"]:
                return httpx.Response(500, json={"error": "internal error"})
            body = json.loads(request.content)
            ref = body["referenceUrl"].rstrip("/").rsplit("/", 1)[-1]
            # The reference account itself, and each searched candidate, come
            # back through this same route -- exactly as in production.
            return httpx.Response(
                200,
                json={
                    "profile": _profile(ref, 40_000, f"{ref} roasting in Brooklyn"),
                    "candidates": [],
                },
            )
        if path.endswith("/search"):
            calls["search"] += 1
            if calls["candidate_failure"]:
                return httpx.Response(503, json={"detail": "search unavailable"})
            return httpx.Response(
                200,
                json={
                    "query": "q",
                    "provider": "brave",
                    "results": [
                        {"url": "https://www.instagram.com/brooklynexampleroasters/"},
                        {"url": "https://www.instagram.com/examplecoffeenyc/"},
                        {"url": "https://elsewhere.test/not-instagram"},
                    ],
                },
            )
        # Hermes: the brand analysis that produces the search terms.
        calls["hermes"] += 1
        if calls["hermes_timeout"]:
            raise httpx.ReadTimeout("model response exceeded the client timeout")
        # Must satisfy BrandAnalysis exactly (extra="forbid", strict=True).
        analysis = {
            "voice_summary": "Warm and unfussy",
            "audience_summary": "US specialty coffee drinkers",
            "brand_identity": "Small-batch roaster",
            "engagement_style": "Reply in the comments, never DM first",
            "content_pillars": ["origin stories", "brew guides"],
            "conversation_topics": ["pour over", "single origin"],
            "search_terms": ["specialty coffee roaster"],
            "us_audience_signals": ["Brooklyn"],
            "next_question": "Which city do you ship from?",
        }
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(analysis)}, "finish_reason": "stop"}],
                # Required by the client: it validates usage is present with
                # non-negative int token counts before trusting the body.
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        calls["client_timeouts"].append(kwargs.get("timeout"))
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    # The route constructs its own AsyncClient, so the transport is swapped at
    # the module the route imports it through -- the route body, the HyperCrawl
    # client and the Hermes client all still run for real.
    monkeypatch.setattr("engagemate.control.routes.onboarding.httpx.AsyncClient", patched)
    monkeypatch.setattr("engagemate.onboarding.analysis.httpx.AsyncClient", patched, raising=False)

    settings = _settings(database_url, hypercrawl=True)
    pool = create_pool(database_url, min_size=1, max_size=4)
    user_id = uuid.uuid4()
    with pool.connection() as connection:
        connection.execute("UPDATE proxies SET health_status = 'active'")
        connection.execute("INSERT INTO users (id, plan) VALUES (%s, 'agency')", (user_id,))
        account_id = uuid.uuid4()
        connection.execute(
            "INSERT INTO ig_accounts (id, user_id, username) VALUES (%s, %s, 'owner')",
            (account_id, user_id),
        )
        connection.execute(
            "INSERT INTO ig_sessions (user_id, ig_account_id) VALUES (%s, %s)",
            (user_id, account_id),
        )
        connection.execute(
            "INSERT INTO proxy_bindings (user_id, ig_account_id) VALUES (%s, %s)",
            (user_id, account_id),
        )
        connection.execute(
            "INSERT INTO agent_configs (user_id, ig_account_id) VALUES (%s, %s)",
            (user_id, account_id),
        )

    app = create_app(settings=settings, pool=pool)
    app.dependency_overrides[current_user_id] = lambda: user_id
    with TestClient(app) as client:
        yield client, calls, pool, user_id, account_id
    pool.close()


def _payload(account_id: uuid.UUID) -> dict:
    usernames = ["examplebrand", "secondbrand", "thirdbrand"]
    return {
        "ig_account_id": str(account_id),
        "audience_mode": "manual",
        "reference_urls": [
            f"https://www.instagram.com/{username}/" for username in usernames
        ],
        "reference_profiles": [
            {
                "username": username,
                "fullName": username.title(),
                "biography": f"{username} roasting in Brooklyn",
                "followersCount": 40_000,
                "followingCount": 200,
                "postsCount": 80,
                "isPrivate": False,
                "isVerified": False,
                "latestPosts": [],
            }
            for username in usernames
        ],
        # REQUIRED_ANSWERS: brand, audience, interests, voice, goals.
        "answers": {
            "brand": "We roast coffee in small batches",
            "audience": "US coffee drinkers",
            "interests": "specialty coffee and roasting",
            "voice": "warm and unfussy",
            "goals": "grow engaged US followers",
        },
    }


def _profile_path(account_id: uuid.UUID, *, details: bool = True) -> str:
    suffix = "" if details else "&details=false"
    return f"/v1/onboarding/brand/profile?ig_account_id={account_id}{suffix}"


def _seed_approved_profile(connection, user_id: uuid.UUID, account_id: uuid.UUID) -> None:
    """Create launch evidence without creating a target."""

    profile_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO brand_profiles (
            id, user_id, ig_account_id, reference_url, reference_username
        ) VALUES (
            %s, %s, %s, 'https://www.instagram.com/source/', 'source'
        )
        """,
        (profile_id, user_id, account_id),
    )
    connection.execute(
        """
        INSERT INTO discovery_candidates (
            brand_profile_id, username, profile_url, score, status
        ) VALUES (
            %s, 'approved-source',
            'https://www.instagram.com/approved-source/', 90, 'imported'
        )
        """,
        (profile_id,),
    )


class _ObservedPool:
    """Expose the launch worker backend for a real PostgreSQL lock assertion."""

    def __init__(self, pool, entered: Event, backend_pid: list[int]) -> None:
        self.pool = pool
        self.entered = entered
        self.backend_pid = backend_pid

    @contextmanager
    def connection(self):
        with self.pool.connection() as connection:
            self.backend_pid.append(connection.info.backend_pid)
            self.entered.set()
            yield connection


def _wait_for_postgres_blocker(connection, worker_pid: int, blocker_pid: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        blocked = connection.execute(
            "SELECT %s = ANY(pg_blocking_pids(%s)) AS blocked",
            (blocker_pid, worker_pid),
        ).fetchone()["blocked"]
        if blocked:
            return
        Event().wait(0.01)
    pytest.fail("launch worker never entered a PostgreSQL lock wait")


def test_brand_analyze_returns_fast_then_fills_candidates(harness, monkeypatch):
    """POST emits its response before the captured discovery task runs."""

    client, calls, pool, _user_id, account_id = harness
    captured_tasks: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def capture_task(self, function, *args, **kwargs):
        captured_tasks.append((function, args, kwargs))

    monkeypatch.setattr(BackgroundTasks, "add_task", capture_task)
    response = client.post("/v1/onboarding/brand/analyze", json=_payload(account_id))
    assert response.status_code == 200, response.text
    body = response.json()

    # The fast half is present, and the slow half is honestly marked unfinished
    # rather than returned as an empty list that reads as "found nothing".
    assert body["status"] == "pending"
    assert body["candidates"] == []
    assert body["analysis"]["search_terms"] == ["specialty coffee roaster"]
    assert calls["search"] == 0
    assert calls["discover"] == 0
    assert len(captured_tasks) == 1

    function, args, kwargs = captured_tasks[0]
    asyncio.run(function(*args, **kwargs))

    polled = client.get(_profile_path(account_id))
    assert polled.status_code == 200, polled.text
    record = polled.json()
    assert record["status"] == "complete"
    assert record["candidates"], record
    assert "brooklynexampleroasters" in {c["username"] for c in record["candidates"]}
    assert record["reference_urls"] == _payload(account_id)["reference_urls"]
    assert record["answers"] == _payload(account_id)["answers"]
    assert all(candidate["imported"] is False for candidate in record["candidates"])

    # The non-instagram search hit never cost a profile fetch.
    assert calls["search"] == 1
    assert calls["hermes"] == 1
    assert 60.0 in calls["client_timeouts"]

    # Reference snapshots came from the authenticated extension. HyperCrawl is
    # used only for the two optional background candidates.
    assert calls["discover"] == 2, calls

    with pool.connection() as connection:
        rows = connection.execute(
            "SELECT username FROM discovery_candidates WHERE brand_profile_id = %s",
            (uuid.UUID(record["profile_id"]),),
        ).fetchall()
    assert rows, "candidates were returned but not stored"

    def _text(value: object) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    stored = {_text(r["username"] if isinstance(r, dict) else r[0]) for r in rows}
    assert "brooklynexampleroasters" in stored, stored


def test_brand_profile_restores_imported_candidate_selection(harness):
    """A reload must not forget which suggested account already became a target."""

    client, _calls, pool, _user_id, account_id = harness
    analyzed = client.post("/v1/onboarding/brand/analyze", json=_payload(account_id))
    profile_id = analyzed.json()["profile_id"]
    candidate = client.get(_profile_path(account_id)).json()["candidates"][0]

    approved = client.post(
        "/v1/onboarding/brand/approve",
        json={
            "profile_id": profile_id,
            "candidate_ids": [candidate["candidate_id"]],
            "ig_account_id": str(account_id),
        },
    )

    assert approved.status_code == 200, approved.text
    restored = client.get(_profile_path(account_id)).json()
    restored_candidate = next(
        item for item in restored["candidates"]
        if item["candidate_id"] == candidate["candidate_id"]
    )
    assert restored_candidate["imported"] is True
    with pool.connection() as connection:
        target = connection.execute(
            "SELECT username FROM targets WHERE ig_account_id = %s",
            (account_id,),
        ).fetchone()
    assert target is not None


def test_brand_profile_status_poll_omits_saved_strategy_details(harness):
    client, _calls, _pool, _user_id, account_id = harness
    analyzed = client.post("/v1/onboarding/brand/analyze", json=_payload(account_id))
    assert analyzed.status_code == 200

    status = client.get(_profile_path(account_id, details=False))

    assert status.status_code == 200
    body = status.json()
    assert body["profile_id"] == analyzed.json()["profile_id"]
    assert body["ig_account_id"] == str(account_id)
    assert body["status"] == "complete"
    assert body["reference_urls"] == []
    assert body["answers"] == {}
    assert body["analysis"] is None
    assert body["candidates"]


def test_brand_approval_rejects_candidate_without_instagram_identity(harness):
    client, _calls, pool, _user_id, account_id = harness
    analyzed = client.post("/v1/onboarding/brand/analyze", json=_payload(account_id))
    profile_id = analyzed.json()["profile_id"]
    candidate = client.get(_profile_path(account_id)).json()["candidates"][0]
    with pool.connection() as connection:
        connection.execute(
            "UPDATE discovery_candidates SET ig_user_id = NULL WHERE id = %s",
            (candidate["candidate_id"],),
        )

    approved = client.post(
        "/v1/onboarding/brand/approve",
        json={
            "profile_id": profile_id,
            "candidate_ids": [candidate["candidate_id"]],
            "ig_account_id": str(account_id),
        },
    )

    assert approved.status_code == 422
    assert approved.json() == {
        "detail": "one or more discovery candidates have no Instagram identity"
    }
    with pool.connection() as connection:
        target = connection.execute(
            "SELECT 1 FROM targets WHERE ig_account_id = %s",
            (account_id,),
        ).fetchone()
    assert target is None


def test_brand_profile_hides_candidate_without_instagram_identity(harness):
    client, _calls, pool, _user_id, account_id = harness
    analyzed = client.post("/v1/onboarding/brand/analyze", json=_payload(account_id))
    candidate = client.get(_profile_path(account_id)).json()["candidates"][0]
    with pool.connection() as connection:
        connection.execute(
            "UPDATE discovery_candidates SET ig_user_id = NULL WHERE id = %s",
            (candidate["candidate_id"],),
        )

    detailed = client.get(_profile_path(account_id))
    lightweight = client.get(_profile_path(account_id, details=False))

    assert detailed.status_code == 200
    assert detailed.json()["profile_id"] == analyzed.json()["profile_id"]
    assert candidate["candidate_id"] not in {
        item["candidate_id"] for item in detailed.json()["candidates"]
    }
    assert lightweight.status_code == 200
    assert candidate["candidate_id"] not in {
        item["candidate_id"] for item in lightweight.json()["candidates"]
    }


def test_brand_profiles_and_candidate_imports_are_scoped_to_one_account(harness):
    client, _calls, pool, user_id, first_account_id = harness
    second_account_id = uuid.uuid4()
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO ig_accounts (id, user_id, username) VALUES (%s, %s, 'second')",
            (second_account_id, user_id),
        )
        connection.execute(
            "INSERT INTO ig_sessions (user_id, ig_account_id) VALUES (%s, %s)",
            (user_id, second_account_id),
        )
        connection.execute(
            "INSERT INTO agent_configs (user_id, ig_account_id) VALUES (%s, %s)",
            (user_id, second_account_id),
        )

    first = client.post(
        "/v1/onboarding/brand/analyze",
        json=_payload(first_account_id),
    )
    second_payload = _payload(second_account_id)
    second_payload["answers"]["brand"] = "A separate account voice"
    second = client.post("/v1/onboarding/brand/analyze", json=second_payload)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["profile_id"] != second.json()["profile_id"]

    first_profile = client.get(_profile_path(first_account_id))
    second_profile = client.get(_profile_path(second_account_id))
    assert first_profile.status_code == 200, first_profile.text
    assert second_profile.status_code == 200, second_profile.text
    assert first_profile.json()["ig_account_id"] == str(first_account_id)
    assert second_profile.json()["ig_account_id"] == str(second_account_id)
    assert first_profile.json()["answers"]["brand"] == "We roast coffee in small batches"
    assert second_profile.json()["answers"]["brand"] == "A separate account voice"

    first_candidate_id = first_profile.json()["candidates"][0]["candidate_id"]
    rejected = client.post(
        "/v1/onboarding/brand/approve",
        json={
            "profile_id": first.json()["profile_id"],
            "candidate_ids": [first_candidate_id],
            "ig_account_id": str(second_account_id),
        },
    )

    assert rejected.status_code == 422
    assert rejected.json() == {
        "detail": "one or more discovery candidates are unavailable"
    }
    with pool.connection() as connection:
        imported = connection.execute(
            "SELECT 1 FROM targets WHERE ig_account_id = %s",
            (second_account_id,),
        ).fetchone()
    assert imported is None


def test_candidate_approval_rejects_another_users_profile(harness):
    """A caller cannot import or mutate a discovery candidate owned by another user."""

    client, _calls, pool, _user_id, account_id = harness
    victim_user_id = uuid.uuid4()
    victim_account_id = uuid.uuid4()
    victim_profile_id = uuid.uuid4()
    victim_candidate_id = uuid.uuid4()
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO users (id, plan) VALUES (%s, 'agency')",
            (victim_user_id,),
        )
        connection.execute(
            "INSERT INTO ig_accounts (id, user_id, username) VALUES (%s, %s, 'victim')",
            (victim_account_id, victim_user_id),
        )
        connection.execute(
            """
            INSERT INTO brand_profiles (
                id, user_id, ig_account_id, reference_url, reference_username
            ) VALUES (%s, %s, %s, 'https://www.instagram.com/victim/', 'victim')
            """,
            (victim_profile_id, victim_user_id, victim_account_id),
        )
        connection.execute(
            """
            INSERT INTO discovery_candidates (
                id, brand_profile_id, username, profile_url, score
            ) VALUES (
                %s, %s, 'victimcandidate',
                'https://www.instagram.com/victimcandidate/', 90
            )
            """,
            (victim_candidate_id, victim_profile_id),
        )

    response = client.post(
        "/v1/onboarding/brand/approve",
        json={
            "profile_id": str(victim_profile_id),
            "candidate_ids": [str(victim_candidate_id)],
            "ig_account_id": str(account_id),
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "one or more discovery candidates are unavailable"
    }
    with pool.connection() as connection:
        target = connection.execute(
            "SELECT 1 FROM targets WHERE username = 'victimcandidate'",
        ).fetchone()
        candidate = connection.execute(
            "SELECT status FROM discovery_candidates WHERE id = %s",
            (victim_candidate_id,),
        ).fetchone()
    assert target is None
    assert candidate["status"] in ("suggested", b"suggested")


def test_launch_starts_warmup_and_automatically_ramps_with_saved_maxima(harness):
    """Launch must turn a configured account into a useful, bounded schedule."""

    client, _calls, pool, user_id, account_id = harness
    analyzed = client.post("/v1/onboarding/brand/analyze", json=_payload(account_id))
    profile = client.get(_profile_path(account_id)).json()
    candidate_id = profile["candidates"][0]["candidate_id"]
    approved = client.post(
        "/v1/onboarding/brand/approve",
        json={
            "profile_id": analyzed.json()["profile_id"],
            "candidate_ids": [candidate_id],
            "ig_account_id": str(account_id),
        },
    )
    assert approved.status_code == 200, approved.text
    with pool.connection() as connection:
        connection.execute(
            "UPDATE agent_configs SET persona = %s, daily_like_limit = 20, "
            "daily_reply_limit = 5, daily_dm_limit = 2, daily_view_limit = 1000 "
            "WHERE user_id = %s AND ig_account_id = %s",
            ("Warm, direct, and useful", user_id, account_id),
        )
        connection.execute(
            "UPDATE ig_accounts SET warmup_started_at = now() - interval '40 days' WHERE id = %s",
            (account_id,),
        )

    response = client.post(
        "/v1/onboarding/launch",
        json={"ig_account_id": str(account_id), "timezone": "Asia/Makassar"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "enabled": True,
        "timezone": "Asia/Makassar",
        "schedule_hour": 9,
        "daily_like_limit": 20,
        "daily_reply_limit": 5,
        "daily_dm_limit": 2,
        "daily_view_limit": 1000,
    }
    with pool.connection() as connection:
        config = connection.execute(
            """
            SELECT enabled, timezone, schedule_hour, daily_like_limit,
                   daily_reply_limit, daily_dm_limit, daily_view_limit,
                   launched_at, account.warmup_started_at
              FROM agent_configs AS config
              JOIN ig_accounts AS account
                ON account.id = config.ig_account_id
               AND account.user_id = config.user_id
             WHERE config.user_id = %s AND config.ig_account_id = %s
            """,
            (user_id, account_id),
        ).fetchone()
    stored = dict(config)
    if isinstance(stored["timezone"], bytes):
        stored["timezone"] = stored["timezone"].decode()
    assert stored["enabled"] is True
    assert stored["timezone"] == "Asia/Makassar"
    assert stored["schedule_hour"] == 9
    assert stored["daily_like_limit"] == 20
    assert stored["daily_reply_limit"] == 5
    assert stored["daily_dm_limit"] == 2
    assert stored["daily_view_limit"] == 1000
    assert stored["launched_at"] is not None
    assert stored["warmup_started_at"] is not None
    assert stored["warmup_started_at"] == stored["launched_at"]

    from datetime import timedelta

    from engagemate.domain.budgets import (
        AccountBudgetState,
        AgentConfigLimits,
        BudgetPolicy,
        DailyActionUsage,
    )

    limits = AgentConfigLimits(
        plan="scale",
        **{name: stored[name] for name in (
            "daily_like_limit", "daily_reply_limit", "daily_dm_limit", "daily_view_limit"
        )},
    )
    for day, expected in [(1, (3, 0, 0, 30)), (4, (5, 1, 0, 60)),
                          (8, (8, 2, 0, 120)), (15, (12, 3, 0, 250)),
                          (31, (20, 5, 0, 300)), (45, (20, 5, 0, 400)),
                          (59, (20, 5, 0, 500)), (73, (20, 5, 0, 600))]:
        budget = BudgetPolicy.remaining(
            AccountBudgetState(stored["warmup_started_at"]), limits,
            DailyActionUsage(0, 0, 0), stored["launched_at"] + timedelta(days=day - 1),
        )
        assert (budget.likes, budget.replies, budget.dms, budget.views) == expected


@pytest.mark.parametrize("limits", [(0, 0, 0, 0), (2, 0, 0, 10), (3, 0, 0, 50)])
def test_first_launch_preserves_explicit_or_legacy_limits(harness, limits):
    client, _calls, pool, user_id, account_id = harness
    with pool.connection() as connection:
        _seed_approved_profile(connection, user_id, account_id)
        connection.execute(
            "INSERT INTO targets (user_id, ig_account_id, username, tier, source, enabled) "
            "VALUES (%s, %s, 'bounded-target', 'discovery', 'test', true)",
            (user_id, account_id),
        )
        connection.execute(
            "UPDATE agent_configs SET persona = 'Helpful', daily_like_limit = %s, "
            "daily_reply_limit = %s, daily_dm_limit = %s, daily_view_limit = %s "
            "WHERE ig_account_id = %s", (*limits, account_id),
        )
    response = client.post('/v1/onboarding/launch', json={
        'ig_account_id': str(account_id), 'timezone': 'UTC',
    })
    assert response.status_code == 200, response.text
    assert tuple(response.json()[name] for name in (
        'daily_like_limit', 'daily_reply_limit', 'daily_dm_limit', 'daily_view_limit'
    )) == limits


def test_launch_resume_preserves_user_limits_and_original_timezone(harness):
    """A paused launched account resumes without replaying first-launch defaults."""

    client, _calls, pool, user_id, account_id = harness
    launched_at = "2026-09-01T10:00:00+00:00"
    with pool.connection() as connection:
        _seed_approved_profile(connection, user_id, account_id)
        connection.execute(
            """
            UPDATE agent_configs
               SET persona = 'Concise and useful',
                   enabled = false,
                   timezone = 'America/Chicago',
                   daily_like_limit = 17,
                   daily_reply_limit = 8,
                   daily_dm_limit = 2,
                   daily_view_limit = 125,
                   launched_at = %s
             WHERE user_id = %s AND ig_account_id = %s
            """,
            (launched_at, user_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO targets (
                user_id, ig_account_id, username, tier, source, enabled
            ) VALUES (%s, %s, 'resume-target', 'discovery', 'test', true)
            """,
            (user_id, account_id),
        )

    response = client.post(
        "/v1/onboarding/launch",
        json={"ig_account_id": str(account_id), "timezone": "Asia/Makassar"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "enabled": True,
        "timezone": "America/Chicago",
        "schedule_hour": 9,
        "daily_like_limit": 17,
        "daily_reply_limit": 8,
        "daily_dm_limit": 2,
        "daily_view_limit": 125,
    }
    with pool.connection() as connection:
        stored = connection.execute(
            """
            SELECT config.launched_at, account.warmup_started_at
              FROM agent_configs AS config
              JOIN ig_accounts AS account
                ON account.id = config.ig_account_id
               AND account.user_id = config.user_id
             WHERE config.user_id = %s AND config.ig_account_id = %s
            """,
            (user_id, account_id),
        ).fetchone()
    assert stored["launched_at"].timestamp() == datetime.fromisoformat(launched_at).timestamp()
    assert stored["warmup_started_at"].timestamp() == datetime.fromisoformat(
        launched_at
    ).timestamp()


def test_launch_rejects_a_profile_approved_for_a_different_account(harness):
    client, _calls, pool, user_id, first_account_id = harness
    second_account_id = uuid.uuid4()
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO ig_accounts (id, user_id, username) VALUES (%s, %s, 'second')",
            (second_account_id, user_id),
        )
        connection.execute(
            "INSERT INTO ig_sessions (user_id, ig_account_id) VALUES (%s, %s)",
            (user_id, second_account_id),
        )
        connection.execute(
            "INSERT INTO proxy_bindings (user_id, ig_account_id) VALUES (%s, %s)",
            (user_id, second_account_id),
        )
        connection.execute(
            """
            INSERT INTO agent_configs (user_id, ig_account_id, persona)
            VALUES (%s, %s, 'Account-specific voice')
            """,
            (user_id, second_account_id),
        )
        connection.execute(
            """
            INSERT INTO targets (
                user_id, ig_account_id, username, tier, source, enabled
            ) VALUES (%s, %s, 'second-target', 'discovery', 'test', true)
            """,
            (user_id, second_account_id),
        )
        _seed_approved_profile(connection, user_id, first_account_id)

    response = client.post(
        "/v1/onboarding/launch",
        json={"ig_account_id": str(second_account_id), "timezone": "UTC"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "complete brand strategy for this Instagram account before launch"
    }
    with pool.connection() as connection:
        config = connection.execute(
            "SELECT enabled, launched_at FROM agent_configs WHERE ig_account_id = %s",
            (second_account_id,),
        ).fetchone()
    assert config == {"enabled": False, "launched_at": None}


def test_launch_rejects_an_account_owned_by_another_tenant(harness):
    client, _calls, pool, _user_id, _account_id = harness
    other_user_id = uuid.uuid4()
    other_account_id = uuid.uuid4()
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO users (id, plan) VALUES (%s, 'agency')",
            (other_user_id,),
        )
        connection.execute(
            "INSERT INTO ig_accounts (id, user_id, username) VALUES (%s, %s, 'other')",
            (other_account_id, other_user_id),
        )
        connection.execute(
            "INSERT INTO agent_configs (user_id, ig_account_id) VALUES (%s, %s)",
            (other_user_id, other_account_id),
        )

    response = client.post(
        "/v1/onboarding/launch",
        json={"ig_account_id": str(other_account_id), "timezone": "UTC"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Instagram account is not owned by this user"
    }


def test_launch_rejects_an_invalid_timezone_before_readiness_checks(harness):
    client, _calls, _pool, _user_id, account_id = harness

    response = client.post(
        "/v1/onboarding/launch",
        json={"ig_account_id": str(account_id), "timezone": "Makassar/Nowhere"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "timezone must be a valid IANA name"}


def test_launch_refuses_an_account_without_a_saved_voice(harness):
    client, _calls, pool, user_id, account_id = harness
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO targets (
                user_id, ig_account_id, username, tier, source, enabled
            ) VALUES (%s, %s, 'voice-target', 'discovery', 'test', true)
            """,
            (user_id, account_id),
        )

    response = client.post(
        "/v1/onboarding/launch",
        json={"ig_account_id": str(account_id), "timezone": "UTC"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "save the account voice before launch"}


def test_launch_refuses_an_expired_session(harness):
    client, _calls, pool, user_id, account_id = harness
    with pool.connection() as connection:
        connection.execute(
            """
            UPDATE ig_sessions
               SET expires_at = now() - interval '1 minute'
             WHERE user_id = %s AND ig_account_id = %s
            """,
            (user_id, account_id),
        )
        connection.execute(
            """
            UPDATE agent_configs
               SET persona = 'Concise and useful'
             WHERE user_id = %s AND ig_account_id = %s
            """,
            (user_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO targets (
                user_id, ig_account_id, username, tier, source, enabled
            ) VALUES (%s, %s, 'expired-session-target', 'discovery', 'test', true)
            """,
            (user_id, account_id),
        )

    response = client.post(
        "/v1/onboarding/launch",
        json={"ig_account_id": str(account_id), "timezone": "UTC"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "reconnect a healthy Instagram session before launch"
    }


def test_launch_refuses_a_session_older_than_the_worker_limit(harness):
    client, _calls, pool, user_id, account_id = harness
    with pool.connection() as connection:
        _seed_approved_profile(connection, user_id, account_id)
        connection.execute(
            "UPDATE ig_sessions SET captured_at = now() - interval '31 days' "
            "WHERE user_id = %s AND ig_account_id = %s",
            (user_id, account_id),
        )
        connection.execute(
            "UPDATE agent_configs SET persona = 'Concise and useful' "
            "WHERE user_id = %s AND ig_account_id = %s",
            (user_id, account_id),
        )
        connection.execute(
            "INSERT INTO targets (user_id, ig_account_id, username, tier, source) "
            "VALUES (%s, %s, 'stale-session-target', 'discovery', 'test')",
            (user_id, account_id),
        )

    response = client.post(
        "/v1/onboarding/launch",
        json={"ig_account_id": str(account_id), "timezone": "UTC"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "reconnect a healthy Instagram session before launch"
    }


def test_launch_refuses_account_without_active_proxy_binding(harness):
    """A launch cannot claim success when the scheduler has no network route."""

    client, _calls, pool, user_id, account_id = harness
    with pool.connection() as connection:
        _seed_approved_profile(connection, user_id, account_id)
        connection.execute(
            "UPDATE agent_configs SET persona = %s WHERE user_id = %s AND ig_account_id = %s",
            ("Warm, direct, and useful", user_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO targets (
                user_id, ig_account_id, username, tier, source, enabled
            ) VALUES (%s, %s, 'proxy-target', 'discovery', 'test', true)
            """,
            (user_id, account_id),
        )
        connection.execute(
            "UPDATE proxy_bindings SET released_at = now() WHERE ig_account_id = %s",
            (account_id,),
        )

    response = client.post(
        "/v1/onboarding/launch",
        json={"ig_account_id": str(account_id), "timezone": "UTC"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "reconnect Instagram from this browser before launch"
    }


def test_launch_refuses_a_binding_to_an_unhealthy_proxy(harness):
    client, _calls, pool, user_id, account_id = harness
    with pool.connection() as connection:
        _seed_approved_profile(connection, user_id, account_id)
        connection.execute(
            "UPDATE agent_configs SET persona = 'Warm and useful' WHERE ig_account_id = %s",
            (account_id,),
        )
        connection.execute(
            "INSERT INTO targets (user_id, ig_account_id, username, tier, source) "
            "VALUES (%s, %s, 'proxy-health-target', 'discovery', 'test')",
            (user_id, account_id),
        )
        connection.execute(
            "UPDATE proxies SET health_status = 'degraded' WHERE id = "
            "(SELECT proxy_id FROM proxy_bindings WHERE ig_account_id = %s)",
            (account_id,),
        )

    response = client.post(
        "/v1/onboarding/launch",
        json={"ig_account_id": str(account_id), "timezone": "UTC"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "reconnect Instagram from this browser before launch"
    }


def test_launch_refuses_an_enabled_target_without_instagram_identity(harness):
    client, _calls, pool, user_id, account_id = harness
    with pool.connection() as connection:
        _seed_approved_profile(connection, user_id, account_id)
        connection.execute(
            "UPDATE agent_configs SET persona = 'Warm and useful' WHERE ig_account_id = %s",
            (account_id,),
        )
        connection.execute(
            "INSERT INTO targets (user_id, ig_account_id, username, ig_user_id, tier, source) "
            "VALUES (%s, %s, 'unresolved-target', NULL, 'discovery', 'test')",
            (user_id, account_id),
        )

    response = client.post(
        "/v1/onboarding/launch",
        json={"ig_account_id": str(account_id), "timezone": "UTC"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "approve your source audience or at least one target before launch"}


def test_launch_refuses_an_account_without_an_approved_target(harness):
    """An enabled schedule with no target cannot perform the job the user launched."""

    client, _calls, pool, user_id, account_id = harness
    with pool.connection() as connection:
        connection.execute(
            "UPDATE agent_configs SET persona = %s WHERE user_id = %s AND ig_account_id = %s",
            ("Warm, direct, and useful", user_id, account_id),
        )

    response = client.post(
        "/v1/onboarding/launch",
        json={"ig_account_id": str(account_id), "timezone": "Asia/Makassar"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "approve your source audience or at least one target before launch"}


@pytest.mark.parametrize(
    ("mutation_sql", "expected_error"),
    [
        (
            "UPDATE ig_accounts SET health_status = 'expired' WHERE id = %s",
            "reconnect a healthy Instagram session before launch",
        ),
        (
            "DELETE FROM ig_sessions WHERE ig_account_id = %s",
            "reconnect a healthy Instagram session before launch",
        ),
        (
            "UPDATE targets SET enabled = false WHERE ig_account_id = %s",
            "approve your source audience or at least one target before launch",
        ),
        (
            "UPDATE proxy_bindings SET released_at = now() WHERE ig_account_id = %s",
            "reconnect Instagram from this browser before launch",
        ),
        (
            ("UPDATE proxies SET health_status = 'degraded' WHERE id = "
            "(SELECT proxy_id FROM proxy_bindings WHERE ig_account_id = %s)"),
            "reconnect Instagram from this browser before launch",
        ),
    ],
)
def test_launch_waits_for_concurrent_readiness_changes(
    harness, mutation_sql, expected_error
):
    """A launch cannot commit using readiness state another transaction removed."""

    _client, _calls, pool, user_id, account_id = harness
    with pool.connection() as connection:
        connection.execute(
            "UPDATE agent_configs SET persona = %s WHERE ig_account_id = %s",
            ("Warm, direct, and useful", account_id),
        )
        connection.execute(
            """
            INSERT INTO targets (
                user_id, ig_account_id, username, tier, source, enabled
            ) VALUES (%s, %s, 'creator', 'discovery', 'test', true)
            """,
            (user_id, account_id),
        )

    worker_entered = Event()
    worker_pid: list[int] = []
    repository = OnboardingLaunchRepository(
        _ObservedPool(pool, worker_entered, worker_pid)
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pool.connection() as blocker:
            blocker.execute(mutation_sql, (account_id,))
            future = executor.submit(
                repository.launch,
                user_id=user_id,
                ig_account_id=account_id,
                timezone_name="Asia/Makassar",
            )
            assert worker_entered.wait(timeout=2)
            _wait_for_postgres_blocker(
                blocker,
                worker_pid[0],
                blocker.info.backend_pid,
            )

        with pytest.raises(ValueError, match=expected_error):
            future.result(timeout=2)


def test_polling_before_any_analysis_is_404(harness):
    client, _calls, pool, user_id, account_id = harness
    with pool.connection() as connection:
        connection.execute("DELETE FROM brand_profiles WHERE user_id = %s", (user_id,))
    assert client.get(_profile_path(account_id)).status_code == 404


def test_polling_requires_an_instagram_account_id(harness):
    client, _calls, _pool, _user_id, _account_id = harness

    response = client.get("/v1/onboarding/brand/profile")

    assert response.status_code == 422


def test_lightweight_pending_poll_omits_reference_and_candidate_rows(harness):
    client, _calls, pool, user_id, account_id = harness
    profile_id = uuid.uuid4()
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO brand_profiles (
                id, user_id, ig_account_id, reference_url, reference_username,
                onboarding_answers, analysis, status
            ) VALUES (
                %s, %s, %s, 'https://www.instagram.com/source/', 'source',
                '{}'::jsonb, '{}'::jsonb, 'pending'
            )
            """,
            (profile_id, user_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO brand_reference_accounts (
                brand_profile_id, username, profile_url, position
            ) VALUES (%s, 'source', 'https://www.instagram.com/source/', 0)
            """,
            (profile_id,),
        )
        connection.execute(
            """
            INSERT INTO discovery_candidates (
                brand_profile_id, username, profile_url, score
            ) VALUES (%s, 'candidate', 'https://www.instagram.com/candidate/', 90)
            """,
            (profile_id,),
        )

    response = client.get(_profile_path(account_id, details=False))

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["reference_urls"] == []
    assert response.json()["answers"] == {}
    assert response.json()["analysis"] is None
    assert response.json()["candidates"] == []


def test_lightweight_completed_poll_returns_candidates_without_profile_details(harness):
    client, _calls, pool, user_id, account_id = harness
    profile_id = uuid.uuid4()
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO brand_profiles (
                id, user_id, ig_account_id, reference_url, reference_username,
                onboarding_answers, analysis, status
            ) VALUES (
                %s, %s, %s, 'https://www.instagram.com/source/', 'source',
                '{"brand":"builders"}'::jsonb,
                '{"voice_summary":"direct"}'::jsonb,
                'complete'
            )
            """,
            (profile_id, user_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO brand_reference_accounts (
                brand_profile_id, username, profile_url, position
            ) VALUES (%s, 'source', 'https://www.instagram.com/source/', 0)
            """,
            (profile_id,),
        )
        connection.execute(
            """
            INSERT INTO discovery_candidates (
                brand_profile_id, username, profile_url, score
            ) VALUES (%s, 'candidate', 'https://www.instagram.com/candidate/', 90)
            """,
            (profile_id,),
        )

    response = client.get(_profile_path(account_id, details=False))

    assert response.status_code == 200
    assert response.json()["status"] == "complete"
    assert response.json()["reference_urls"] == []
    assert response.json()["answers"] == {}
    assert response.json()["analysis"] is None
    assert [candidate["username"] for candidate in response.json()["candidates"]] == [
        "candidate"
    ]


def test_older_candidate_discovery_cannot_overwrite_a_newer_analysis(harness):
    _client, _calls, pool, user_id, account_id = harness
    repository = BrandProfileRepository(pool)
    references = [
        {
            "url": "https://www.instagram.com/source/",
            "username": "source",
            "scraped": {},
        }
    ]
    first_profile_id, first_revision = repository.save(
        user_id=user_id,
        ig_account_id=account_id,
        references=references,
        answers={"brand": "old"},
        analysis={"voice_summary": "old"},
        candidates=[],
        status="pending",
    )
    second_profile_id, second_revision = repository.save(
        user_id=user_id,
        ig_account_id=account_id,
        references=references,
        answers={"brand": "new"},
        analysis={"voice_summary": "new"},
        candidates=[],
        status="pending",
    )
    old_candidate = {
        "username": "stalecreator",
        "score": 91,
        "engagement_rate": 0.04,
        "us_signal": True,
        "reasons": ["old analysis"],
        "sources": ["source"],
        "profile": {"id": "candidate-id", "followersCount": 1_500},
    }
    new_candidate = {
        **old_candidate,
        "username": "freshcreator",
        "reasons": ["new analysis"],
    }

    assert first_profile_id == second_profile_id
    assert first_revision != second_revision
    assert repository.complete_discovery(
        first_profile_id,
        expected_revision=first_revision,
        candidates=[old_candidate],
    ) is False
    assert repository.complete_discovery(
        second_profile_id,
        expected_revision=second_revision,
        candidates=[new_candidate],
    ) is True

    record = repository.load_for_user(user_id, account_id)
    assert record is not None
    assert record["status"] == "complete"
    assert [candidate["username"] for candidate in record["candidates"]] == [
        "freshcreator"
    ]


def test_older_candidate_failure_cannot_overwrite_a_newer_analysis(harness):
    _client, _calls, pool, user_id, account_id = harness
    repository = BrandProfileRepository(pool)
    references = [
        {
            "url": "https://www.instagram.com/source/",
            "username": "source",
            "scraped": {},
        }
    ]
    first_profile_id, first_revision = repository.save(
        user_id=user_id,
        ig_account_id=account_id,
        references=references,
        answers={"brand": "old"},
        analysis={"voice_summary": "old"},
        candidates=[],
        status="pending",
    )
    second_profile_id, second_revision = repository.save(
        user_id=user_id,
        ig_account_id=account_id,
        references=references,
        answers={"brand": "new"},
        analysis={"voice_summary": "new"},
        candidates=[],
        status="pending",
    )

    assert first_profile_id == second_profile_id
    assert first_revision != second_revision
    assert repository.fail_discovery(
        first_profile_id,
        expected_revision=first_revision,
    ) is False

    record = repository.load_for_user(user_id, account_id)
    assert record is not None
    assert record["status"] == "pending"
    assert record["analysis"] == {"voice_summary": "new"}


def test_brand_analyze_reports_hermes_timeout_as_temporary_upstream_failure(harness):
    client, calls, _pool, _user_id, account_id = harness
    calls["hermes_timeout"] = True

    response = client.post("/v1/onboarding/brand/analyze", json=_payload(account_id))

    assert response.status_code == 502
    assert response.json() == {
        "detail": "brand analysis is temporarily unavailable"
    }



def test_brand_analyze_rejects_missing_answers(harness):
    client, _calls, _pool, _user, account_id = harness
    payload = _payload(account_id)
    payload["answers"] = {"interests": "only one"}
    assert client.post("/v1/onboarding/brand/analyze", json=payload).status_code == 422


def test_brand_analyze_rejects_an_account_not_owned_by_the_tenant(harness):
    client, calls, _pool, _user, _account_id = harness

    response = client.post(
        "/v1/onboarding/brand/analyze",
        json=_payload(uuid.uuid4()),
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "connect Instagram before brand analysis"
    }
    assert calls["hermes"] == 0
    assert calls["discover"] == 0


def test_brand_analyze_rejects_an_owned_account_without_a_captured_session(harness):
    client, calls, pool, _user, account_id = harness
    with pool.connection() as connection:
        connection.execute(
            "DELETE FROM ig_sessions WHERE ig_account_id = %s",
            (account_id,),
        )

    response = client.post(
        "/v1/onboarding/brand/analyze",
        json=_payload(account_id),
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "connect Instagram before brand analysis"
    }
    assert calls["hermes"] == 0
    assert calls["discover"] == 0


def test_brand_analyze_rejects_profiles_that_do_not_match_reference_urls(harness):
    client, calls, _pool, _user, account_id = harness
    payload = _payload(account_id)
    payload["reference_profiles"][1]["username"] = "someoneelse"

    response = client.post("/v1/onboarding/brand/analyze", json=payload)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "reference profiles must match reference URLs"
    }
    assert calls["hermes"] == 0
    assert calls["discover"] == 0


def test_candidate_search_failure_does_not_fail_brand_analysis(harness):
    client, calls, _pool, _user, account_id = harness
    calls["candidate_failure"] = True

    response = client.post(
        "/v1/onboarding/brand/analyze",
        json=_payload(account_id),
    )

    assert response.status_code == 200
    assert response.json()["analysis"]["brand_identity"] == "Small-batch roaster"
    assert client.get(_profile_path(account_id)).json()["status"] == "failed"
    assert calls["hermes"] == 1


def test_profile_outage_is_failed_and_retry_recovers_importable_targets(harness):
    client, calls, pool, _user, account_id = harness
    calls["profile_failure"] = True
    first = client.post("/v1/onboarding/brand/analyze", json=_payload(account_id))
    assert first.status_code == 200
    failed = client.get(_profile_path(account_id)).json()
    assert failed["status"] == "failed"
    assert failed["analysis"]["brand_identity"] == "Small-batch roaster"
    assert failed["candidates"] == []

    calls["profile_failure"] = False
    retried = client.post("/v1/onboarding/brand/analyze", json=_payload(account_id))
    assert retried.status_code == 200
    restored = client.get(_profile_path(account_id)).json()
    assert restored["status"] == "complete"
    candidate_id = restored["candidates"][0]["candidate_id"]
    approved = client.post("/v1/onboarding/brand/approve", json={
        "profile_id": restored["profile_id"],
        "candidate_ids": [candidate_id],
        "ig_account_id": str(account_id),
    })
    assert approved.status_code == 200
    with pool.connection() as connection:
        target = connection.execute(
            "SELECT ig_user_id FROM targets WHERE ig_account_id = %s",
            (account_id,),
        ).fetchone()
    assert target is not None
    identity = target["ig_user_id"]
    if isinstance(identity, bytes):
        identity = identity.decode()
    assert identity.isascii() and identity.isdecimal()


def test_brand_analysis_succeeds_without_hypercrawl_candidate_search(harness):
    client, calls, _pool, _user, account_id = harness
    client.app.state.settings.hypercrawl_url = None

    response = client.post(
        "/v1/onboarding/brand/analyze",
        json=_payload(account_id),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "complete"
    assert response.json()["analysis"]["brand_identity"] == "Small-batch roaster"
    assert calls["hermes"] == 1
    assert calls["search"] == 0
    assert calls["discover"] == 0


def test_brand_approval_accepts_json_uuid_strings_and_reaches_the_route(harness):
    client, _calls, _pool, _user, _account_id = harness

    response = client.post(
        "/v1/onboarding/brand/approve",
        json={
            "profile_id": str(uuid.uuid4()),
            "candidate_ids": [str(uuid.uuid4())],
            "ig_account_id": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Instagram account is not owned by this user"
    }


def test_follower_source_approval_replaces_candidate_import_and_launches(harness):
    client, calls, pool, _user_id, account_id = harness
    payload = _payload(account_id)
    payload.pop("audience_mode")
    response = client.post("/v1/onboarding/brand/analyze", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "complete"
    assert response.json()["candidates"] == []
    assert calls["search"] == 0
    assert calls["discover"] == 0
    approval = {
        "profile_id": response.json()["profile_id"],
        "ig_account_id": str(account_id),
        "reference_urls": payload["reference_urls"],
    }
    assert client.post("/v1/onboarding/brand/approve-sources", json=approval).status_code == 200
    assert client.get(_profile_path(account_id)).json()["follower_audience_approved"] is True
    with pool.connection() as connection:
        connection.execute("UPDATE agent_configs SET persona = 'Warm and direct' WHERE ig_account_id = %s", (account_id,))
        assert connection.execute("SELECT count(*) AS n FROM targets WHERE ig_account_id = %s", (account_id,)).fetchone()["n"] == 0
    launch = client.post("/v1/onboarding/launch", json={"ig_account_id": str(account_id), "timezone": "UTC"})
    assert launch.status_code == 200, launch.text


def test_source_approval_rejects_foreign_or_changed_sources_and_save_revokes(harness):
    client, _, _pool, _, account_id = harness
    payload = _payload(account_id)
    payload.pop("audience_mode")
    profile = client.post("/v1/onboarding/brand/analyze", json=payload).json()
    approval = {"profile_id": profile["profile_id"], "ig_account_id": str(account_id), "reference_urls": payload["reference_urls"]}
    for changed in (
        {**approval, "ig_account_id": str(uuid.uuid4())},
        {**approval, "profile_id": str(uuid.uuid4())},
        {**approval, "reference_urls": ["https://www.instagram.com/different/", *payload["reference_urls"][1:]]},
    ):
        assert client.post("/v1/onboarding/brand/approve-sources", json=changed).status_code == 422
    assert client.post("/v1/onboarding/brand/approve-sources", json=approval).status_code == 200
    assert client.post("/v1/onboarding/brand/analyze", json=payload).status_code == 200
    assert client.get(_profile_path(account_id)).json()["follower_audience_approved"] is False


def test_source_mutation_revokes_approval_and_reuses_position_gap(harness):
    from engagemate.mcp.control_repository import ControlRepository
    client, _, pool, user_id, account_id = harness
    payload = _payload(account_id)
    payload.pop("audience_mode")
    profile = client.post("/v1/onboarding/brand/analyze", json=payload).json()
    approval = {"profile_id": profile["profile_id"], "ig_account_id": str(account_id), "reference_urls": payload["reference_urls"]}
    assert client.post("/v1/onboarding/brand/approve-sources", json=approval).status_code == 200
    repository = ControlRepository(pool)
    sources = repository.list_sources(user_id, account_id)
    assert repository.remove_source(user_id, account_id, uuid.UUID(str(sources[1]["id"]))) is True
    assert client.get(_profile_path(account_id)).json()["follower_audience_approved"] is False
    added = repository.add_source(user_id, account_id, "replacement")
    assert added["position"] == 1
    fresh_urls = [row["profile_url"] for row in repository.list_sources(user_id, account_id)]
    assert client.post("/v1/onboarding/brand/approve-sources", json={**approval, "reference_urls": fresh_urls}).status_code == 200
    repository.add_source(user_id, account_id, "fourth")
    assert client.get(_profile_path(account_id)).json()["follower_audience_approved"] is False
    assert client.post("/v1/onboarding/brand/approve-sources", json=approval).status_code == 422


def test_source_draft_is_durable_unapproved_and_keeps_existing_profile_id(harness):
    client, calls, pool, _, account_id = harness
    payload = _payload(account_id)
    payload.pop("audience_mode")
    profile = client.post("/v1/onboarding/brand/analyze", json=payload).json()
    approval = {"profile_id": profile["profile_id"], "ig_account_id": str(account_id), "reference_urls": payload["reference_urls"]}
    assert client.post("/v1/onboarding/brand/approve-sources", json=approval).status_code == 200
    before_calls = dict(calls)
    changed_urls = [f"https://www.instagram.com/{u}/" for u in ("firstnew", "secondnew", "thirdnew")]
    saved = client.post("/v1/onboarding/brand/sources", json={
        "ig_account_id": str(account_id), "reference_urls": changed_urls,
        "answers": {"audience": "My corrected audience"},
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["profile_id"] == profile["profile_id"]
    assert saved.json()["source_revision"]
    assert calls == before_calls
    record = client.get(_profile_path(account_id)).json()
    assert record["reference_urls"] == changed_urls
    assert record["answers"] == {"audience": "My corrected audience"}
    assert record["analysis"] is None
    assert record["status"] == "pending"
    assert record["candidates"] == []
    assert record["follower_audience_approved"] is False
    with pool.connection() as connection:
        stored = connection.execute("SELECT analysis IS NULL AS empty FROM brand_profiles WHERE id = %s", (profile["profile_id"],)).fetchone()
        assert stored["empty"] is True
        evidence = connection.execute("SELECT scraped FROM brand_reference_accounts WHERE brand_profile_id = %s", (profile["profile_id"],)).fetchall()
        assert all(row["scraped"] == {} for row in evidence)
        connection.execute("UPDATE brand_profiles SET updated_at = now() - interval '2 days' WHERE id = %s", (profile["profile_id"],))
    assert client.get(_profile_path(account_id)).json()["status"] == "pending"
    assert client.get(_profile_path(account_id, details=False)).json()["status"] == "pending"
    assert client.post("/v1/onboarding/brand/approve-sources", json={**approval, "reference_urls": changed_urls}).status_code == 422


def test_source_draft_checks_ownership_and_late_analysis_cannot_overwrite_it(harness):
    client, _, _, _, account_id = harness
    payload = _payload(account_id)
    payload.pop("audience_mode")
    draft_request = {key: payload[key] for key in ("ig_account_id", "reference_urls", "answers")}
    assert client.post("/v1/onboarding/brand/sources", json={**draft_request, "ig_account_id": str(uuid.uuid4())}).status_code == 422
    assert client.post("/v1/onboarding/brand/sources", json={**draft_request, "reference_urls": ["https://www.instagram.com/single/"]}).status_code == 422
    saved = client.post("/v1/onboarding/brand/sources", json=draft_request).json()
    revision = saved["source_revision"]
    newer_urls = ["https://www.instagram.com/newsource/", *payload["reference_urls"][1:]]
    assert client.post("/v1/onboarding/brand/sources", json={**draft_request, "reference_urls": newer_urls}).status_code == 200
    response = client.post("/v1/onboarding/brand/analyze", json={**payload, "source_revision": revision})
    assert response.status_code == 422, response.text
    assert "source accounts changed" in response.json()["detail"]
    assert client.get(_profile_path(account_id)).json()["reference_urls"] == newer_urls
    latest = client.post("/v1/onboarding/brand/sources", json=draft_request).json()
    completed = client.post("/v1/onboarding/brand/analyze", json={**payload, "source_revision": latest["source_revision"]})
    assert completed.status_code == 200, completed.text


def test_manual_owner_setup_approves_sources_without_remote_requests(harness):
    client, calls, pool, user_id, account_id = harness
    sources = ["https://www.instagram.com/exampleone/", "https://www.instagram.com/exampletwo/", "https://www.instagram.com/examplethree/"]
    response = client.post("/v1/onboarding/manual", json={"ig_account_id":str(account_id),"reference_urls":sources,"persona":"Use a short friendly question about an observed detail.","approve":True})
    assert response.status_code == 200, response.text
    assert response.json() == {"approved":True}
    assert calls["discover"] == calls["search"] == calls["hermes"] == 0
    profile = client.get("/v1/onboarding/brand/profile",params={"ig_account_id":str(account_id)})
    assert profile.status_code == 200
    assert profile.json()["follower_audience_approved"] is True
    assert profile.json()["analysis"]["brand_identity"] == "Manual setup; no model analysis"
    denied = client.post("/v1/onboarding/manual",json={"ig_account_id":str(uuid.uuid4()),"reference_urls":sources,"persona":"Synthetic voice","approve":True})
    assert denied.status_code == 422
    not_approved = client.post("/v1/onboarding/manual",json={"ig_account_id":str(account_id),"reference_urls":sources,"persona":"Synthetic voice","approve":False})
    assert not_approved.status_code == 422
