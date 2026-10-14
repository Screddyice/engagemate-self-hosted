"""Behavioral checks for the MV3 worker's automatic connection details."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_TICKET = "em_xt_dGVzdC1wYXlsb2Fk.dGVzdC1zaWduYXR1cmU"

HARNESS = r"""
const fs = require("node:fs");
const vm = require("node:vm");

let cookieRows = [];
const stored = {};
const listeners = [];
const TEST_TICKET = "em_xt_dGVzdC1wYXlsb2Fk.dGVzdC1zaWduYXR1cmU";
globalThis.chrome = {
  storage: {
    local: {
      get: async (keys) => Object.fromEntries(keys.map((key) => [key, stored[key]])),
      set: async (values) => Object.assign(stored, values),
    },
  },
  cookies: {
    getAll: async () => cookieRows,
    onChanged: { addListener: () => {} },
  },
  alarms: {
    create: () => {},
    onAlarm: { addListener: () => {} },
  },
  runtime: {
    onMessage: { addListener: (listener) => listeners.push(listener) },
  },
  tabs: { create: async () => {} },
};
Object.defineProperty(globalThis, "navigator", {
  configurable: true,
  value: { userAgent: "EngageMate runtime test" },
});

function response(status, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  };
}

/*PRELUDE*/

vm.runInThisContext(fs.readFileSync(/*WORKER*/"extension/background.js", "utf8"));

function emit(value) {
  process.stdout.write(JSON.stringify(value));
}
"""


def run_worker(scenario: str, prelude: str = "", worker: Path | None = None) -> dict:
    """Load the worker, then run one scenario.

    `prelude` runs before the worker loads, which is where a test changes the
    browser the worker sees (for example, Safari's namespace). `worker` loads a
    packaged background.js instead of the committed source.
    """

    harness = HARNESS.replace("/*PRELUDE*/", prelude)
    if worker is not None:
        harness = harness.replace(
            '/*WORKER*/"extension/background.js"', json.dumps(str(worker))
        )
    completed = subprocess.run(
        ["node", "-"],
        cwd=REPO_ROOT,
        input=harness
        + "\n(async () => {\n"
        + scenario
        + "\n})().catch((error) => { console.error(error); process.exit(1); });\n",
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_sync_derives_identity_and_geography_without_storing_session_values() -> None:
    result = run_worker(
        r"""
cookieRows = [
  { name: "sessionid", value: "secret-session", domain: ".instagram.com", path: "/", secure: true, httpOnly: true, sameSite: "unspecified" },
  { name: "ds_user_id", value: "1234567890123456789", domain: ".instagram.com", path: "/", secure: true, httpOnly: false, sameSite: "unspecified" },
];
const requests = [];
globalThis.fetch = async (url, options) => {
  requests.push({ url, options });
  if (url.includes("instagram.com/api/")) {
    return response(200, { user: { pk_id: "1234567890123456789", username: "Derived.User" } });
  }
  if (url.endsWith("/api/extension/suggest-geo")) {
    return response(200, {
      recommended: { country: "US", region: "TX", city: "Austin" },
      within_guard: true,
    });
  }
  return response(200, { id: "acct-1", username: "derived.user", health: "active" });
};

const sync = await syncSession({ ticket: TEST_TICKET });
const upload = JSON.parse(requests.at(-1).options.body);
emit({
  sync,
  identityRequest: {
    url: requests[0].url,
    credentials: requests[0].options.credentials,
    cache: requests[0].options.cache,
    appId: requests[0].options.headers["X-IG-App-ID"],
    hasSignal: Boolean(requests[0].options.signal),
  },
  geoRequest: {
    url: requests[1].url,
    credentials: requests[1].options.credentials,
    authorization: requests[1].options.headers.Authorization,
    body: JSON.parse(requests[1].options.body),
  },
  uploadAuthorization: requests.at(-1).options.headers.Authorization,
  upload,
  stored,
});
"""
    )

    assert result["sync"]["success"] is True
    assert result["identityRequest"] == {
        "url": "https://www.instagram.com/api/v1/accounts/current_user/?edit=true",
        "credentials": "include",
        "cache": "no-store",
        "appId": "936619743392459",
        "hasSignal": True,
    }
    assert result["geoRequest"]["url"].endswith("/api/extension/suggest-geo")
    assert result["geoRequest"]["credentials"] == "include"
    assert result["geoRequest"]["body"].get("timezone")
    assert result["geoRequest"]["authorization"] == f"Bearer {TEST_TICKET}"
    assert result["uploadAuthorization"] == f"Bearer {TEST_TICKET}"
    assert result["upload"]["username"] == "derived.user"
    assert result["upload"]["ig_user_id"] == "1234567890123456789"
    assert result["upload"]["login_geo"] == {
        "country": "US",
        "region": "TX",
        "city": "Austin",
    }
    assert result["stored"]["username"] == "derived.user"
    assert "cookies" not in result["stored"]
    assert "secret-session" not in json.dumps(result["stored"])


def test_login_geography_rejects_inventory_far_from_the_request_ip() -> None:
    result = run_worker(
        r"""
globalThis.fetch = async () => response(200, {
  recommended: { country: "DK", region: "84", city: "Copenhagen" },
  distance_miles: 7162.8,
  within_guard: false,
});
emit(await discoverLoginGeo(TEST_TICKET));
"""
    )

    assert result == {
        "success": False,
        "code": "geo_discovery_failed",
        "error": "EngageMate has no execution proxy close enough to this Instagram login. Try again after a nearby proxy is available.",
    }


def test_identity_response_must_match_the_cookie_user_id() -> None:
    result = run_worker(
        r"""
const cookies = [{ name: "ds_user_id", value: "123" }];
globalThis.fetch = async () => response(200, { user: { pk_id: "456", username: "wrong" } });
emit(await discoverInstagramUsername(cookies));
"""
    )

    assert result["success"] is False
    assert result["code"] == "identity_discovery_failed"
    assert "wrong" not in json.dumps(result)


@pytest.mark.parametrize(
    ("fetch_body", "expected_code"),
    (
        ("return response(401, {});", "ig_session_missing"),
        ("return response(403, {});", "ig_session_missing"),
        ("return response(429, {});", "identity_rate_limited"),
        ("return response(503, {});", "network_error"),
        ("throw new Error('offline');", "network_error"),
        (
            'return response(200, { user: { pk_id: "123", username: "not a handle!" } });',
            "identity_discovery_failed",
        ),
    ),
)
def test_identity_failures_collapse_to_bounded_codes(
    fetch_body: str, expected_code: str
) -> None:
    result = run_worker(
        f"""
const cookies = [{{ name: "ds_user_id", value: "123" }}];
globalThis.fetch = async () => {{ {fetch_body} }};
emit(await discoverInstagramUsername(cookies));
"""
    )

    assert result["success"] is False
    assert result["code"] == expected_code


def test_onboarding_profile_lookup_returns_only_bounded_public_fields() -> None:
    result = run_worker(
        r"""
cookieRows = [
  { name: "sessionid", value: "secret-session", domain: ".instagram.com", path: "/", secure: true, httpOnly: true, sameSite: "unspecified" },
];
globalThis.fetch = async (url, options) => response(200, {
  data: {
    user: {
      username: "source.one",
      full_name: "Source One",
      biography: "Austin product builder",
      follower_count: 1200,
      following_count: 75,
      media_count: 40,
      is_private: false,
      is_verified: true,
      profile_pic_url: "https://cdn.example/private-token",
      edge_owner_to_timeline_media: {
        edges: [{ node: {
          edge_liked_by: { count: 48 },
          edge_media_to_comment: { count: 3 },
          edge_media_to_caption: { edges: [{ node: { text: "A useful post" } }] },
        } }],
      },
    },
  },
});

const lookup = listeners[0];
let reply;
const keepAlive = lookup(
  {
    type: "ONBOARDING_PROFILE_LOOKUP",
    nonce: "a".repeat(64),
    usernames: ["source.one"],
  },
  { url: "http://localhost:3030/onboarding" },
  (value) => { reply = value; },
);
await new Promise((resolve) => setTimeout(resolve, 0));
emit({ keepAlive, reply });
"""
    )

    assert result == {
        "keepAlive": True,
        "reply": {
            "success": True,
            "profiles": [
                {
                    "username": "source.one",
                    "fullName": "Source One",
                    "biography": "Austin product builder",
                    "followersCount": 1200,
                    "followingCount": 75,
                    "postsCount": 40,
                    "isPrivate": False,
                    "isVerified": True,
                    "latestPosts": [
                        {
                            "likesCount": 48,
                            "commentsCount": 3,
                            "caption": "A useful post",
                        }
                    ],
                }
            ],
        },
    }
    assert "secret-session" not in json.dumps(result)
    assert "private-token" not in json.dumps(result)


def test_onboarding_profile_lookup_paces_instagram_requests() -> None:
    result = run_worker(
        r"""
cookieRows = [
  { name: "sessionid", value: "secret-session", domain: ".instagram.com", path: "/", secure: true, httpOnly: true, sameSite: "unspecified" },
];
const starts = [];
let inFlight = 0;
let maxInFlight = 0;
globalThis.fetch = async (url) => {
  starts.push(Date.now());
  inFlight += 1;
  maxInFlight = Math.max(maxInFlight, inFlight);
  await new Promise((resolve) => setTimeout(resolve, 25));
  inFlight -= 1;
  const username = new URL(url).searchParams.get("username");
  return response(200, {
    data: {
      user: {
        username,
        full_name: username,
        follower_count: 1200,
        following_count: 75,
        media_count: 40,
        is_private: false,
        is_verified: false,
        biography: "Austin product builder",
      },
    },
  });
};

const result = await discoverInstagramProfiles(["source.one", "source.two", "source.three"]);
emit({
  result,
  maxInFlight,
  gaps: starts.slice(1).map((startedAt, index) => startedAt - starts[index]),
});
"""
    )

    assert result["result"]["success"] is True
    assert result["maxInFlight"] == 1
    assert len(result["gaps"]) == 2
    assert min(result["gaps"]) >= 700


def test_onboarding_profile_lookup_stops_after_the_first_failure() -> None:
    result = run_worker(
        r"""
cookieRows = [
  { name: "sessionid", value: "secret-session", domain: ".instagram.com", path: "/", secure: true, httpOnly: true, sameSite: "unspecified" },
];
const requested = [];
globalThis.fetch = async (url) => {
  const username = new URL(url).searchParams.get("username");
  requested.push(username);
  if (username === "source.two") return response(429, {});
  return response(200, {
    data: {
      user: {
        username,
        full_name: username,
        follower_count: 1200,
        following_count: 75,
        media_count: 40,
        is_private: false,
        is_verified: false,
        biography: "Austin product builder",
      },
    },
  });
};

const lookup = await discoverInstagramProfiles(["source.one", "source.two", "source.three"]);
emit({ lookup, requested });
"""
    )

    assert result == {
        "lookup": {"success": False, "code": "identity_rate_limited"},
        "requested": ["source.one", "source.two"],
    }


def test_onboarding_connect_ignores_page_supplied_account_details() -> None:
    result = run_worker(
        r"""
cookieRows = [];
const connect = listeners[0];
let reply;
const keepAlive = connect(
  {
    type: "ONBOARDING_CONNECT",
    nonce: "a".repeat(64),
    ticket: TEST_TICKET,
    username: "page_supplied",
    loginGeo: { country: "ZZ", city: "Page City" },
  },
  { url: "http://localhost:3030/onboarding" },
  (value) => { reply = value; },
);
await new Promise((resolve) => setTimeout(resolve, 0));
emit({ keepAlive, reply, stored });
"""
    )

    assert result["keepAlive"] is True
    assert result["reply"] == {"status": "opening_instagram"}
    assert "username" not in result["stored"]
    assert "loginGeo" not in result["stored"]


def test_onboarding_connect_requires_a_signed_ticket_shape() -> None:
    result = run_worker(
        r"""
const connect = listeners[0];
let reply;
const keepAlive = connect(
  { type: "ONBOARDING_CONNECT", nonce: "a".repeat(64) },
  { url: "http://localhost:3030/onboarding" },
  (value) => { reply = value; },
);
emit({ keepAlive, reply });
"""
    )

    assert result == {
        "keepAlive": False,
        "reply": {"status": "error", "code": "invalid_connection_input"},
    }


def test_retired_user_configuration_cannot_restore_an_arbitrary_endpoint() -> None:
    result = run_worker(
        r"""
stored.engagemateUrl = "https://evil.example/session-collector";
emit(await getConfig());
"""
    )

    assert result == {"url": "http://localhost:3030"}


def test_missing_ticket_preserves_sign_in_error_for_popup_reload() -> None:
    result = run_worker(r'''
stored.connectionRequestedAt = Date.now();
stored.lastAccount = { id: 'old-account', username: 'old-user' };
globalThis.fetch = async () => { throw new Error('must not send without ticket'); };
const sync = await syncSession();
emit({ sync, stored });
''')
    assert result['sync']['code'] == 'auth_required'
    assert result['stored']['lastErrorCode'] == 'auth_required'
    assert result['stored']['lastAccount'] is None


@pytest.mark.parametrize('same_account', [True, False])
def test_reconnect_reuses_confirmed_geo_only_for_same_instagram_id(same_account):
    result = run_worker(r'''
cookieRows = [
 {name:'sessionid',value:'test-cookie',domain:'.instagram.com',path:'/'},
 {name:'ds_user_id',value:'12345',domain:'.instagram.com',path:'/'},
];
stored.confirmedLoginGeo = {
 igUserId: SAME_ACCOUNT ? '12345' : '67890',
 geo: {country:'US',region:'NC',city:'Raleigh'},
};
let geoLookups = 0;
let upload;
globalThis.fetch = async (url, options) => {
 if (url.includes('instagram.com/api/')) return response(200,{user:{pk:'12345',username:'account'}});
 if (url.endsWith('/suggest-geo')) {
  geoLookups++;
  return response(200,{recommended:{country:'US',region:'MA',city:'Springfield'},within_guard:true});
 }
 upload = JSON.parse(options.body);
 return response(200,{id:'acct',username:'account',health:'active'});
};
const result = await syncSession({ticket:TEST_TICKET});
emit({success:result.success,geoLookups,geo:upload.login_geo,confirmed:stored.confirmedLoginGeo});
'''.replace('SAME_ACCOUNT', json.dumps(same_account)))
    assert result['success'] is True
    assert result['geoLookups'] == (0 if same_account else 1)
    assert result['geo']['city'] == ('Raleigh' if same_account else 'Springfield')
    assert result['confirmed'] == {'igUserId': '12345', 'geo': result['geo']}


def test_failed_capture_does_not_confirm_a_location():
    result = run_worker(r'''
cookieRows = [
 {name:'sessionid',value:'test-cookie',domain:'.instagram.com',path:'/'},
 {name:'ds_user_id',value:'12345',domain:'.instagram.com',path:'/'},
];
globalThis.fetch = async (url) => url.includes('instagram.com/api/')
 ? response(200,{user:{pk:'12345',username:'account'}})
 : response(409,{error:'Account conflict'});
const result = await syncSession({ticket:TEST_TICKET,loginGeo:{country:'US',region:'NC',city:'Raleigh'}});
emit({success:result.success,confirmed:stored.confirmedLoginGeo || null});
''')
    assert result == {'success': False, 'confirmed': None}


@pytest.mark.parametrize('matching_id', [True, False])
def test_legacy_reconnect_resolves_tenant_account_by_immutable_id(matching_id):
    result = run_worker(r'''
cookieRows = [
 {name:'sessionid',value:'test-cookie',domain:'.instagram.com',path:'/'},
 {name:'ds_user_id',value:'12345',domain:'.instagram.com',path:'/'},
];
stored.username = 'account';
stored.loginGeo = {country:'DK',region:'84',city:'Copenhagen'};
let geoLookups=0, upload;
globalThis.fetch = async (url,options) => {
 if (url.includes('instagram.com/api/')) return response(200,{user:{pk:'12345',username:'account'}});
 if (url.endsWith('/api/accounts')) return response(200,[{igUserId:MATCHING_ID?'12345':'67890',loginCountry:'US',loginRegion:'NC',loginCity:'Raleigh'}]);
 if (url.endsWith('/suggest-geo')) {
  geoLookups++;
  return response(200,{recommended:{country:'US',region:'MA',city:'Springfield'},within_guard:true});
 }
 upload=JSON.parse(options.body);
 return response(200,{id:'acct',username:'account',health:'active'});
};
const result=await syncSession({ticket:TEST_TICKET});
emit({success:result.success,geoLookups,geo:upload.login_geo});
'''.replace('MATCHING_ID', json.dumps(matching_id)))
    assert result['success'] is True
    assert result['geoLookups'] == (0 if matching_id else 1)
    assert result['geo']['city'] == ('Raleigh' if matching_id else 'Springfield')


# ─── Safari ────────────────────────────────────────────────────────────
#
# Safari runs the same worker. It exposes the promise-based `browser`
# namespace, and Apple documents `cookies.onChanged` as unsupported, so this
# stand-in has no cookie listener at all. `chrome` stays defined, as it is in
# Safari, but records anything registered on it so a test can prove the
# worker used `browser` instead.

SAFARI_PRELUDE = r"""
// The harness reads one JSON document from stdout; worker logs would break it.
console.log = () => {};
const chromeListeners = [];
const alarmCalls = [];
const clearedAlarms = [];
const alarmListeners = [];
const openedTabs = [];
let instagramAccess = true;
globalThis.chrome = {
  runtime: { onMessage: { addListener: (listener) => chromeListeners.push(listener) } },
};
globalThis.browser = {
  storage: chrome_storage_stub(),
  cookies: { getAll: async () => cookieRows },
  alarms: {
    create: async (name, info) => { alarmCalls.push({ name, info }); },
    clear: async (name) => { clearedAlarms.push(name); return true; },
    onAlarm: { addListener: (listener) => alarmListeners.push(listener) },
  },
  permissions: { contains: async () => instagramAccess },
  runtime: {
    sendMessage: async () => undefined,
    onMessage: { addListener: (listener) => listeners.push(listener) },
  },
  tabs: { create: async (options) => { openedTabs.push(options.url); } },
};
function chrome_storage_stub() {
  return {
    local: {
      get: async (keys) => Object.fromEntries(keys.map((key) => [key, stored[key]])),
      set: async (values) => Object.assign(stored, values),
    },
  };
}
"""

SIGNED_IN_COOKIES = r"""
cookieRows = [
  { name: "sessionid", value: "secret-session", domain: ".instagram.com", path: "/", secure: true, httpOnly: true, sameSite: "no_restriction" },
  { name: "ds_user_id", value: "12345", domain: ".instagram.com", path: "/", secure: true, httpOnly: false, sameSite: "no_restriction" },
];
"""


def test_safari_worker_loads_without_a_cookie_listener_and_uses_browser() -> None:
    result = run_worker(
        r"""
emit({
  safariListeners: listeners.length,
  chromeListeners: chromeListeners.length,
  alarms: alarmCalls.map((call) => call.name),
});
""",
        prelude=SAFARI_PRELUDE,
    )

    assert result == {
        "safariListeners": 2,
        "chromeListeners": 0,
        "alarms": ["session-sync"],
    }


def test_safari_connect_watches_for_sign_in_and_finishes_the_connection() -> None:
    result = run_worker(
        r"""
cookieRows = [];
let reply;
listeners[0](
  { type: "ONBOARDING_CONNECT", nonce: "a".repeat(64), ticket: TEST_TICKET },
  { url: "http://localhost:3030/onboarding" },
  (value) => { reply = value; },
);
await new Promise((resolve) => setTimeout(resolve, 0));
const scheduled = alarmCalls.filter((call) => call.name === "session-watch");

// Nothing to sync until Instagram's session exists.
let uploads = 0;
globalThis.fetch = async (url) => {
  if (url.includes("instagram.com/api/")) return response(200, { user: { pk: "12345", username: "account" } });
  if (url.endsWith("/suggest-geo")) return response(200, { recommended: { country: "US", region: "NC", city: "Raleigh" }, within_guard: true });
  uploads += 1;
  return response(200, { id: "acct", username: "account", health: "active" });
};
await alarmListeners[0]({ name: "session-watch" });
const beforeSignIn = { uploads, cleared: [...clearedAlarms] };

SIGNED_IN_COOKIES
await alarmListeners[0]({ name: "session-watch" });

emit({ reply, openedTabs, scheduled, beforeSignIn, uploads, clearedAlarms, account: stored.lastAccount });
""".replace("SIGNED_IN_COOKIES", SIGNED_IN_COOKIES),
        prelude=SAFARI_PRELUDE,
    )

    assert result["reply"] == {"status": "opening_instagram"}
    assert result["openedTabs"] == ["https://www.instagram.com/accounts/login/"]
    assert result["scheduled"] == [
        {"name": "session-watch", "info": {"delayInMinutes": 0.5, "periodInMinutes": 0.5}}
    ]
    assert result["beforeSignIn"] == {"uploads": 0, "cleared": []}
    assert result["uploads"] == 1
    assert result["clearedAlarms"] == ["session-watch"]
    assert result["account"]["id"] == "acct"


def test_safari_session_watch_stops_after_the_sign_in_window() -> None:
    result = run_worker(
        r"""
SIGNED_IN_COOKIES
stored.connectionRequestedAt = Date.now() - 11 * 60_000;
stored.connectionTicket = TEST_TICKET;
let requests = 0;
globalThis.fetch = async () => { requests += 1; return response(500, {}); };
await alarmListeners[0]({ name: "session-watch" });
emit({ requests, clearedAlarms });
""".replace("SIGNED_IN_COOKIES", SIGNED_IN_COOKIES),
        prelude=SAFARI_PRELUDE,
    )

    assert result == {"requests": 0, "clearedAlarms": ["session-watch"]}


def test_withheld_instagram_access_is_reported_instead_of_opening_sign_in() -> None:
    result = run_worker(
        r"""
cookieRows = [];
instagramAccess = false;
let reply;
const keepAlive = listeners[0](
  { type: "ONBOARDING_CONNECT", nonce: "a".repeat(64), ticket: TEST_TICKET },
  { url: "http://localhost:3030/onboarding" },
  (value) => { reply = value; },
);
await new Promise((resolve) => setTimeout(resolve, 0));
emit({ keepAlive, reply, openedTabs, alarms: alarmCalls.map((call) => call.name) });
""",
        prelude=SAFARI_PRELUDE,
    )

    assert result == {
        "keepAlive": True,
        "reply": {"status": "error", "code": "instagram_access_required"},
        "openedTabs": [],
        "alarms": ["session-sync"],
    }


def test_chrome_connect_keeps_its_cookie_listener_and_schedules_no_watch() -> None:
    """Chrome has cookies.onChanged, so its connection flow is unchanged."""

    result = run_worker(
        r"""
cookieRows = [];
let reply;
listeners[0](
  { type: "ONBOARDING_CONNECT", nonce: "a".repeat(64), ticket: TEST_TICKET },
  { url: "http://localhost:3030/onboarding" },
  (value) => { reply = value; },
);
await new Promise((resolve) => setTimeout(resolve, 0));
emit({ reply, cookieListeners, alarms: alarmNames, permissionChecks });
""",
        prelude=r"""
let cookieListeners = 0;
let permissionChecks = 0;
const alarmNames = [];
chrome.cookies.onChanged.addListener = () => { cookieListeners += 1; };
chrome.alarms.create = (name) => { alarmNames.push(name); };
chrome.permissions = { contains: async () => { permissionChecks += 1; return true; } };
""",
    )

    assert result == {
        "reply": {"status": "opening_instagram"},
        "cookieListeners": 1,
        "alarms": ["session-sync"],
        "permissionChecks": 1,
    }


# ─── Trusted origins come from the build, never from a built-in domain ──

PING = r"""
const ping = listeners[0];
const replies = {};
for (const url of URLS) {
  ping({ type: "ONBOARDING_PING", nonce: "a".repeat(64) }, { url }, (value) => {
    replies[url] = value.status;
  });
}
emit({ replies, config: await getConfig() });
"""


def _ping(urls: list[str], worker: Path | None = None) -> dict:
    return run_worker(f"const URLS = {json.dumps(urls)};" + PING, worker=worker)


def test_unconfigured_worker_trusts_only_local_development() -> None:
    result = _ping(
        [
            "http://localhost:3030/onboarding",
            "https://engagemate.io/onboarding",
            "https://app.example.com/onboarding",
        ]
    )

    assert result == {
        "replies": {
            "http://localhost:3030/onboarding": "installed",
            "https://engagemate.io/onboarding": "error",
            "https://app.example.com/onboarding": "error",
        },
        "config": {"url": "http://localhost:3030"},
    }


def test_configured_worker_trusts_its_origin_and_nothing_else(tmp_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "package_extension.py"),
            "--origin",
            "https://app.example.com",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    [archive] = (tmp_path / "dist").glob("*.zip")
    worker = tmp_path / "background.js"
    with zipfile.ZipFile(archive) as packaged:
        worker.write_bytes(packaged.read("background.js"))

    result = _ping(
        [
            "https://app.example.com/onboarding",
            "http://localhost:3030/onboarding",
            "https://engagemate.io/onboarding",
        ],
        worker=worker,
    )

    assert result == {
        "replies": {
            "https://app.example.com/onboarding": "installed",
            "http://localhost:3030/onboarding": "error",
            "https://engagemate.io/onboarding": "error",
        },
        "config": {"url": "https://app.example.com"},
    }
