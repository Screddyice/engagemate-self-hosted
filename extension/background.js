// EngageMate browser extension (Chrome and Safari): secure Instagram session
// bridge only.
//
// Captures Instagram cookies with full metadata and posts them to the
// EngageMate web tier (POST /api/extension/session), which proxies to the
// Control API. Never stores session cookies in extension storage.
// Never performs likes, replies, or DMs.

// Chrome exposes the extension APIs as `chrome`. Safari also exposes the
// promise-based `browser` namespace. Select `browser` only when it carries a
// real runtime, so Chrome keeps calling exactly the APIs it always has.
if (typeof importScripts === "function") {
  importScripts("cookie-selection.js");
}

const ext =
  typeof globalThis.browser?.runtime?.sendMessage === "function"
    ? globalThis.browser
    : globalThis.chrome;

const INSTAGRAM_DOMAINS = [
  "instagram.com",
  ".instagram.com",
  "www.instagram.com",
];

// Cookie names we capture when present. sessionid is required by Control API.
const COOKIE_NAMES = [
  "sessionid",
  "csrftoken",
  "ds_user_id",
  "mid",
  "ig_did",
  "rur",
];

const SYNC_ALARM = "session-sync";
// Safari documents `cookies.onChanged` as unsupported. Where it is missing, a
// short alarm watches for the Instagram sign-in the connection is waiting on.
const SESSION_WATCH_ALARM = "session-watch";
const SESSION_WATCH_WINDOW_MS = 10 * 60_000;
const COOKIE_CHANGES_SUPPORTED =
  typeof ext.cookies?.onChanged?.addListener === "function";
// Safari grants host access per site. Without instagram.com the cookie read
// comes back empty even when the manager is signed in.
const INSTAGRAM_ACCESS_ORIGINS = ["https://www.instagram.com/*"];
const cookieSelection = globalThis.EngageMateCookieSelection ?? (() => {
  const mapSameSite = (sameSite) => {
    if (sameSite === "no_restriction") return "None";
    if (sameSite === "lax") return "Lax";
    if (sameSite === "strict") return "Strict";
    if (sameSite === "unspecified" || sameSite == null) return "unspecified";
    return String(sameSite);
  };
  const cookieToPayload = (cookie) => {
    const payload = {
      name: cookie.name,
      value: cookie.value,
      domain: cookie.domain,
      path: cookie.path || "/",
      secure: Boolean(cookie.secure),
      httpOnly: Boolean(cookie.httpOnly),
      sameSite: mapSameSite(cookie.sameSite),
    };
    if (
      typeof cookie.expirationDate === "number" &&
      Number.isFinite(cookie.expirationDate) &&
      cookie.expirationDate > 0
    ) {
      payload.expirationDate = cookie.expirationDate;
    }
    return payload;
  };
  const selectInstagramCookies = (cookies, allowedNames) => {
    const selected = new Map();
    const rank = (domain) =>
      domain === "www.instagram.com" ? 2 : domain === ".instagram.com" ? 1 : 0;
    for (const cookie of Array.isArray(cookies) ? cookies : []) {
      if (!allowedNames.includes(cookie.name)) continue;
      const current = selected.get(cookie.name);
      const domain = String(cookie.domain || "").toLowerCase();
      const currentDomain = String(current?.domain || "").toLowerCase();
      if (
        !current ||
        rank(domain) > rank(currentDomain) ||
        (rank(domain) === rank(currentDomain) &&
          String(cookie.path || "/").length > String(current.path || "/").length)
      ) {
        selected.set(cookie.name, cookie);
      }
    }
    return Array.from(selected.values()).map(cookieToPayload);
  };
  return { selectInstagramCookies };
})();
const { selectInstagramCookies } = cookieSelection;
const MAX_ATTEMPTS = 3;
const BASE_BACKOFF_MS = 500;
// The web origins this extension trusts, primary first. The packaging script
// rewrites this one line from ENGAGEMATE_WEB_ORIGIN. The source trusts only
// local development, so an unconfigured build talks to no public host.
const ENGAGEMATE_WEB_ORIGINS = ["http://localhost:3030"];
const ONBOARDING_ENGAGEMATE_ORIGINS = new Set(ENGAGEMATE_WEB_ORIGINS);

// ─── Internal origin selection ─────────────────────────────────────────

async function getConfig() {
  const data = await ext.storage.local.get(["engagemateUrl"]);
  try {
    const origin = new URL(data.engagemateUrl || ENGAGEMATE_WEB_ORIGINS[0]).origin;
    if (ONBOARDING_ENGAGEMATE_ORIGINS.has(origin)) return { url: origin };
  } catch {
    // Fall through to the primary configured origin.
  }
  return { url: ENGAGEMATE_WEB_ORIGINS[0] };
}

function onboardingOrigin(sender) {
  try {
    const origin = new URL(sender?.url || "").origin;
    return ONBOARDING_ENGAGEMATE_ORIGINS.has(origin) ? origin : null;
  } catch {
    return null;
  }
}

// ─── Cookie helpers ────────────────────────────────────────────────────

async function getInstagramCookies() {
  try {
    // Query the exact origin used by the worker. Domain filters overlap, and
    // choosing the first cookie by name can retain an old parent-domain
    // sessionid while discarding the current account cookie.
    const cookies = await ext.cookies.getAll({
      url: "https://www.instagram.com/",
    });
    const captured = selectInstagramCookies(cookies, COOKIE_NAMES);
    return captured.length > 0 ? captured : null;
  } catch (e) {
    console.error("Error reading Instagram cookies for account origin");
    return null;
  }
}

/**
 * True unless the browser reports that instagram.com access was withheld.
 * Browsers without the permissions API, or a failing check, keep the original
 * flow rather than blocking a connection on an unanswerable question.
 */
async function hasInstagramAccess() {
  if (typeof ext.permissions?.contains !== "function") return true;
  try {
    return (
      (await ext.permissions.contains({ origins: INSTAGRAM_ACCESS_ORIGINS })) !==
      false
    );
  } catch {
    return true;
  }
}

async function detectInstagramSession() {
  const cookies = await getInstagramCookies();
  const hasSession =
    Array.isArray(cookies) && cookies.some((c) => c.name === "sessionid");
  return {
    loggedIn: hasSession,
    cookieCount: cookies ? cookies.length : 0,
  };
}

function igUserIdFromCookies(cookies) {
  const ds = cookies.find((c) => c.name === "ds_user_id");
  return ds && typeof ds.value === "string" ? ds.value : null;
}

const INSTAGRAM_USERNAME = /^[a-z0-9._]{1,64}$/;
const INSTAGRAM_WEB_APP_ID = "936619743392459";
const INSTAGRAM_IDENTITY_TIMEOUT_MS = 8_000;
const INSTAGRAM_PROFILE_TIMEOUT_MS = 12_000;
const INSTAGRAM_PROFILE_PACE_MS = 750;

/**
 * Read the handle from Instagram's authenticated session. The page and popup
 * never supply identity: the browser session is the source of truth.
 */
async function discoverInstagramUsername(cookies) {
  const igUserId = igUserIdFromCookies(cookies);
  if (!igUserId) {
    return {
      success: false,
      code: "identity_discovery_failed",
      error: "EngageMate could not identify the signed-in Instagram account. Reopen Instagram and try again.",
    };
  }
  const endpoints = [
    "https://www.instagram.com/api/v1/accounts/current_user/?edit=true",
    ...(igUserId
      ? [`https://www.instagram.com/api/v1/users/${encodeURIComponent(igUserId)}/info/`]
      : []),
  ];

  let retryableFailure = false;

  for (const endpoint of endpoints) {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      INSTAGRAM_IDENTITY_TIMEOUT_MS,
    );
    try {
      const response = await fetch(endpoint, {
        method: "GET",
        credentials: "include",
        cache: "no-store",
        headers: { "X-IG-App-ID": INSTAGRAM_WEB_APP_ID },
        signal: controller.signal,
      });
      if (response.status === 401 || response.status === 403) {
        return {
          success: false,
          code: "ig_session_missing",
          error: "Your Instagram session expired. Sign in to Instagram and try again.",
        };
      }
      if (response.status === 429) {
        return {
          success: false,
          code: "identity_rate_limited",
          error: "Instagram is temporarily limiting account checks. Wait a few minutes and try again.",
        };
      }
      if (response.status >= 500) {
        retryableFailure = true;
        continue;
      }
      if (!response.ok) continue;

      const data = await response.json();
      const user = data?.user || data;
      const endpointUserIds = [user?.pk_id, user?.id, user?.pk]
        .filter((value) => value !== undefined && value !== null)
        .map(String);
      if (!endpointUserIds.includes(igUserId)) {
        return {
          success: false,
          code: "identity_discovery_failed",
          error: "Instagram returned a different account than the signed-in session. Reopen Instagram and try again.",
        };
      }

      const username = String(user?.username || "")
        .trim()
        .toLowerCase();
      if (INSTAGRAM_USERNAME.test(username)) {
        return { success: true, data: username };
      }
    } catch {
      retryableFailure = true;
    } finally {
      clearTimeout(timeout);
    }
  }

  if (retryableFailure) {
    return {
      success: false,
      code: "network_error",
      error: "EngageMate could not reach Instagram to identify the account. Try again.",
    };
  }
  return {
    success: false,
    code: "identity_discovery_failed",
    error: "EngageMate could not identify the signed-in Instagram account. Reopen Instagram and try again.",
  };
}

function boundedText(value, limit) {
  return String(value || "")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
    .slice(0, limit);
}

function boundedCount(value) {
  return Number.isInteger(value) && value >= 0 && value <= 2_147_483_647
    ? value
    : 0;
}

function mapPublicInstagramProfile(user, expectedUsername) {
  const username = String(user?.username || "").trim().toLowerCase();
  if (username !== expectedUsername || !INSTAGRAM_USERNAME.test(username)) return null;
  const timeline = Array.isArray(user?.edge_owner_to_timeline_media?.edges)
    ? user.edge_owner_to_timeline_media.edges
    : [];
  const latestPosts = timeline.slice(0, 12).map((edge) => {
    const node = edge?.node || {};
    const caption = node?.edge_media_to_caption?.edges?.[0]?.node?.text;
    return {
      likesCount: boundedCount(
        node?.edge_liked_by?.count ?? node?.edge_media_preview_like?.count ?? node?.like_count,
      ),
      commentsCount: boundedCount(
        node?.edge_media_to_comment?.count ?? node?.comment_count,
      ),
      caption: boundedText(caption, 2_200),
    };
  });
  return {
    username,
    fullName: boundedText(user?.full_name, 256),
    biography: boundedText(user?.biography, 2_200),
    followersCount: boundedCount(
      user?.edge_followed_by?.count ?? user?.follower_count,
    ),
    followingCount: boundedCount(user?.edge_follow?.count ?? user?.following_count),
    postsCount: boundedCount(
      user?.edge_owner_to_timeline_media?.count ?? user?.media_count,
    ),
    isPrivate: user?.is_private === true,
    isVerified: user?.is_verified === true,
    latestPosts,
  };
}

async function fetchPublicInstagramProfile(username) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), INSTAGRAM_PROFILE_TIMEOUT_MS);
  try {
    const response = await fetch(
      `https://www.instagram.com/api/v1/users/web_profile_info/?username=${encodeURIComponent(username)}`,
      {
        method: "GET",
        credentials: "include",
        cache: "no-store",
        headers: { "X-IG-App-ID": INSTAGRAM_WEB_APP_ID },
        signal: controller.signal,
      },
    );
    if (response.status === 401 || response.status === 403) {
      return { success: false, code: "ig_session_missing" };
    }
    if (response.status === 429) {
      return { success: false, code: "identity_rate_limited" };
    }
    if (!response.ok) return { success: false, code: "network_error" };
    const body = await response.json();
    const profile = mapPublicInstagramProfile(body?.data?.user || body?.user, username);
    return profile
      ? { success: true, data: profile }
      : { success: false, code: "identity_discovery_failed" };
  } catch {
    return { success: false, code: "network_error" };
  } finally {
    clearTimeout(timeout);
  }
}

async function discoverInstagramProfiles(usernames) {
  const cookies = await getInstagramCookies();
  if (!cookies?.some((cookie) => cookie.name === "sessionid")) {
    return { success: false, code: "ig_session_missing" };
  }
  const profiles = [];
  for (let index = 0; index < usernames.length; index += 1) {
    if (index > 0) {
      await sleep(INSTAGRAM_PROFILE_PACE_MS);
    }
    const result = await fetchPublicInstagramProfile(usernames[index]);
    if (!result.success) {
      return {
        success: false,
        code: result.code || "identity_discovery_failed",
      };
    }
    profiles.push(result.data);
  }
  return { success: true, profiles };
}

function browserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

/** Resolve a proxy-compatible login location without prompting the user. */
async function discoverLoginGeo(ticket) {
  const timezone = browserTimezone();
  if (!timezone) {
    return {
      success: false,
      code: "geo_discovery_failed",
      error: "EngageMate could not detect this browser's login region. Try again.",
    };
  }

  const { url } = await getConfig();
  try {
    const response = await fetch(`${url}/api/extension/suggest-geo`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(ticket ? { Authorization: `Bearer ${ticket}` } : {}),
      },
      body: JSON.stringify({ timezone }),
    });
    if (response.status === 401) {
      return {
        success: false,
        code: "auth_required",
        error: "Sign in to EngageMate in your browser, then try again.",
      };
    }
    if (!response.ok) throw new Error("geo suggestion failed");

    const data = await response.json();
    if (data?.within_guard !== true) {
      return {
        success: false,
        code: "geo_discovery_failed",
        error:
          "EngageMate has no execution proxy close enough to this Instagram login. Try again after a nearby proxy is available.",
      };
    }
    const recommended = data?.recommended;
    const country = String(recommended?.country || "").toUpperCase();
    if (!/^[A-Z]{2}$/.test(country)) throw new Error("invalid geo suggestion");

    return {
      success: true,
      data: {
        country,
        ...(recommended?.region
          ? { region: String(recommended.region) }
          : {}),
        ...(recommended?.city ? { city: String(recommended.city) } : {}),
      },
    };
  } catch {
    return {
      success: false,
      code: "geo_discovery_failed",
      error: "EngageMate could not choose a login region. Try again.",
    };
  }
}

// ─── User agent ────────────────────────────────────────────────────────

async function captureUserAgent() {
  // Service workers expose navigator.userAgent in Chromium and Safari.
  // We intentionally avoid the scripting API (no activeTab / scripting
  // permission) so the bridge stays cookies+storage+alarms only.
  if (typeof navigator !== "undefined" && navigator.userAgent) {
    return navigator.userAgent;
  }
  return "Mozilla/5.0 EngageMateSessionBridge";
}

// ─── Redacted account status (extension storage, no cookies) ───────────

async function saveAccountStatus(account, extra = {}) {
  const payload = {
    lastAccount: account
      ? {
          id: account.id,
          username: account.username,
          health: account.health,
          captured_at: account.captured_at,
          proxy_bound: account.proxy_bound,
        }
      : null,
    lastSyncAt: account ? Date.now() : null,
    lastError: extra.error || null,
    lastErrorCode: extra.code || null,
    ...extra.fields,
  };
  await ext.storage.local.set(payload);
}

async function getAccountStatus() {
  const data = await ext.storage.local.get([
    "lastAccount",
    "lastSyncAt",
    "lastError",
    "lastErrorCode",
    "username",
    "loginGeo",
    "confirmedLoginGeo",
    "connectionRequestedAt",
    "connectionTicket",
  ]);
  return data;
}

// ─── Sync with retry (idempotent POST only) ────────────────────────────

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * @param {object} [opts]
 * @param {{ country: string, region?: string, city?: string }} [opts.loginGeo]
 *   A previously server-selected geography used for background refreshes.
 */
async function syncSession(opts = {}) {
  const ticket = opts.ticket || (await getAccountStatus()).connectionTicket;
  if (!ticket) {
    await saveAccountStatus(null, {
      code: "auth_required",
      error: "Open EngageMate and reconnect this Instagram account.",
    });
    return {
      success: false,
      code: "auth_required",
      error: "Open EngageMate and reconnect this Instagram account.",
    };
  }

  const cookies = await getInstagramCookies();
  if (!cookies || !cookies.some((c) => c.name === "sessionid")) {
    await saveAccountStatus(null, {
      error: "Not logged into Instagram. Open Instagram and sign in, then reconnect.",
      code: "ig_session_missing",
    });
    return {
      success: false,
      code: "ig_session_missing",
      error:
        "Not logged into Instagram. Open Instagram and sign in, then reconnect.",
    };
  }

  const igUserId = igUserIdFromCookies(cookies);
  if (!igUserId) {
    const error =
      "EngageMate could not identify the signed-in Instagram account. Reopen Instagram and try again.";
    await saveAccountStatus(null, {
      error,
      code: "identity_discovery_failed",
    });
    return { success: false, code: "identity_discovery_failed", error };
  }

  const identity = await discoverInstagramUsername(cookies);
  if (!identity.success) {
    await saveAccountStatus(null, {
      error: identity.error,
      code: identity.code,
    });
    return identity;
  }
  const username = identity.data;

  // Instagram can rotate sessionid/csrftoken while the identity request runs.
  // Persist the post-verification snapshot so this account does not receive
  // the stale cookie set that triggered the sync.
  const freshCookies = await getInstagramCookies();
  if (!freshCookies || !freshCookies.some((c) => c.name === "sessionid")) {
    const error =
      "Instagram changed the session while EngageMate verified the account. Reopen Instagram and try again.";
    await saveAccountStatus(null, { error, code: "ig_session_changed" });
    return { success: false, code: "ig_session_changed", error };
  }
  if (igUserIdFromCookies(freshCookies) !== igUserId) {
    const error =
      "The Instagram account changed while EngageMate was saving the session. Reopen Instagram and try again.";
    await saveAccountStatus(null, { error, code: "ig_account_changed" });
    return { success: false, code: "ig_account_changed", error };
  }

  let loginGeo = opts.loginGeo;
  if (!loginGeo?.country) {
    const stored = await getAccountStatus();
    const saved = stored.confirmedLoginGeo;
    // A browser can switch Instagram accounts. Reuse a confirmed location
    // only for the immutable account ID that successfully captured it.
    if (saved?.igUserId === igUserId && saved.geo?.country) {
      loginGeo = saved.geo;
    }
    // Older installations have no ID-bound cache. Resolve their previous
    // account from the signed-in tenant rather than trusting a saved handle.
    if (!loginGeo && stored.username === username) {
      try {
        const { url } = await getConfig();
        const response = await fetch(`${url}/api/accounts`, {
          credentials: "include",
          cache: "no-store",
          signal: AbortSignal.timeout(5000),
        });
        const accounts = response.ok ? await response.json() : null;
        const account = Array.isArray(accounts)
          ? accounts.find((item) => item?.igUserId === igUserId)
          : null;
        if (
          /^[A-Z]{2}$/.test(account?.loginCountry || "") &&
          typeof account?.loginRegion === "string" &&
          typeof account?.loginCity === "string"
        ) {
          loginGeo = {
            country: account.loginCountry,
            region: account.loginRegion,
            city: account.loginCity,
          };
        }
      } catch {
        // An unverified previous account cannot supply a location.
      }
    }
  }
  if (!loginGeo?.country) {
    const geo = await discoverLoginGeo(ticket);
    if (!geo.success) {
      await saveAccountStatus(null, { error: geo.error, code: geo.code });
      return geo;
    }
    loginGeo = geo.data;
  }

  const { url } = await getConfig();
  if (!url) {
    return {
      success: false,
      code: "not_configured",
      error: "EngageMate URL is not configured",
    };
  }

  const userAgent = await captureUserAgent();
  const body = {
    username,
    ig_user_id: igUserId,
    cookies: freshCookies,
    user_agent: userAgent,
    login_geo: {
      country: loginGeo.country,
      ...(loginGeo.region ? { region: loginGeo.region } : {}),
      ...(loginGeo.city ? { city: loginGeo.city } : {}),
    },
  };

  // Persist non-secret derived values for automatic refreshes (never cookies).
  await ext.storage.local.set({
    username,
    loginGeo: {
      country: loginGeo.country,
      region: loginGeo.region || "",
      city: loginGeo.city || "",
    },
  });

  let lastError = "Sync failed";
  let lastCode = "sync_failed";

  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      const response = await fetch(`${url}/api/extension/session`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${ticket}`,
        },
        body: JSON.stringify(body),
      });

      if (response.status === 401) {
        await saveAccountStatus(null, {
          error: "Sign in to EngageMate in your browser, then try again.",
          code: "auth_required",
        });
        return {
          success: false,
          code: "auth_required",
          error: "Sign in to EngageMate in your browser, then try again.",
        };
      }

      // Do not retry client validation / conflicts.
      if (response.status >= 400 && response.status < 500) {
        let message = "Session capture failed";
        try {
          const data = await response.json();
          if (data && typeof data.error === "string") message = data.error;
        } catch {
          // ignore
        }
        await saveAccountStatus(null, { error: message, code: "client_error" });
        return { success: false, code: "client_error", error: message };
      }

      if (!response.ok) {
        lastError = `Server error (${response.status})`;
        lastCode = "server_error";
        if (attempt < MAX_ATTEMPTS) {
          await sleep(BASE_BACKOFF_MS * Math.pow(2, attempt - 1));
          continue;
        }
        await saveAccountStatus(null, { error: lastError, code: lastCode });
        return { success: false, code: lastCode, error: lastError };
      }

      const account = await response.json();
      await saveAccountStatus(account, {
        fields: { confirmedLoginGeo: { igUserId, geo: body.login_geo } },
      });
      return { success: true, data: account };
    } catch (e) {
      lastError = "Network error reaching EngageMate";
      lastCode = "network_error";
      if (attempt < MAX_ATTEMPTS) {
        await sleep(BASE_BACKOFF_MS * Math.pow(2, attempt - 1));
        continue;
      }
      await saveAccountStatus(null, { error: lastError, code: lastCode });
      return { success: false, code: lastCode, error: lastError };
    }
  }

  await saveAccountStatus(null, { error: lastError, code: lastCode });
  return { success: false, code: lastCode, error: lastError };
}

/** Start the user-approved connection, opening Instagram only when needed. */
async function connectInstagram(origin = null, ticket = null) {
  const fields = {
    connectionRequestedAt: Date.now(),
    lastError: null,
    lastErrorCode: null,
    ...(origin ? { engagemateUrl: origin } : {}),
    ...(ticket ? { connectionTicket: ticket } : {}),
  };
  await ext.storage.local.set(fields);

  const ig = await detectInstagramSession();
  if (!ig.loggedIn) {
    if (!(await hasInstagramAccess())) {
      return {
        success: false,
        status: "error",
        code: "instagram_access_required",
        error: "Allow EngageMate on instagram.com, then try again.",
      };
    }
    try {
      await ext.tabs.create({
        url: "https://www.instagram.com/accounts/login/",
      });
    } catch {
      return {
        success: false,
        status: "error",
        code: "instagram_open_failed",
        error: "EngageMate could not open Instagram.",
      };
    }
    if (!COOKIE_CHANGES_SUPPORTED) {
      try {
        await ext.alarms.create(SESSION_WATCH_ALARM, {
          delayInMinutes: 0.5,
          periodInMinutes: 0.5,
        });
      } catch {
        console.warn("Could not schedule the Instagram sign-in watch");
      }
    }
    return { success: false, status: "opening_instagram" };
  }

  return syncSession({ ticket });
}

// ─── Auth check (GET, credentials included — Clerk cookies) ────────────

async function checkAuth() {
  const { url } = await getConfig();
  const { connectionTicket } = await getAccountStatus();
  try {
    const response = await fetch(`${url}/api/extension/session`, {
      method: "GET",
      credentials: "include",
      headers: {
        ...(connectionTicket
          ? { Authorization: `Bearer ${connectionTicket}` }
          : {}),
      },
    });
    if (response.status === 401) {
      return { authenticated: false };
    }
    if (!response.ok) {
      return { authenticated: false, error: `status ${response.status}` };
    }
    const data = await response.json();
    return { authenticated: Boolean(data.authenticated), ...data };
  } catch {
    return { authenticated: false, error: "unreachable" };
  }
}

// ─── Cookie-change auto sync ───────────────────────────────────────────

let pendingSync = null;

// A missing listener API must not throw here: an exception at the top level
// of the worker would stop every onMessage listener below from registering.
if (COOKIE_CHANGES_SUPPORTED) {
  ext.cookies.onChanged.addListener((changeInfo) => {
    const cookie = changeInfo.cookie;
    const domain = cookie.domain || "";
    const isIg = INSTAGRAM_DOMAINS.some((d) =>
      domain.endsWith(d.replace(/^\./, ""))
    );
    if (!isIg) return;
    if (!COOKIE_NAMES.includes(cookie.name)) return;

    if (pendingSync) clearTimeout(pendingSync);
    pendingSync = setTimeout(async () => {
      pendingSync = null;
      const status = await getAccountStatus();
      if (!status.connectionRequestedAt) return;
      console.log("Auto-syncing Instagram session after cookie change");
      const result = await syncSession({
        ticket: status.connectionTicket,
      });
      if (!result.success) {
        console.warn("Auto-sync failed:", result.code);
      }
    }, 2000);
  });
}

/**
 * Stand-in for the cookie listener where the browser has none: finish a
 * connection once Instagram's session appears, and stop watching after the
 * same ten-minute window the EngageMate page waits.
 */
async function watchPendingSession() {
  const status = await getAccountStatus();
  const requestedAt = Number(status.connectionRequestedAt) || 0;
  if (!requestedAt || Date.now() - requestedAt > SESSION_WATCH_WINDOW_MS) {
    await ext.alarms.clear(SESSION_WATCH_ALARM);
    return;
  }
  const ig = await detectInstagramSession();
  if (!ig.loggedIn) return;

  await ext.alarms.clear(SESSION_WATCH_ALARM);
  console.log("Syncing Instagram session after sign-in");
  const result = await syncSession({ ticket: status.connectionTicket });
  if (!result.success) {
    console.warn("Session watch sync failed:", result.code);
  }
}

// ─── 30-minute alarm sync ──────────────────────────────────────────────

ext.alarms.create(SYNC_ALARM, { periodInMinutes: 30 });

ext.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === SESSION_WATCH_ALARM) {
    await watchPendingSession();
    return;
  }
  if (alarm.name !== SYNC_ALARM) return;
  const status = await getAccountStatus();
  if (!status.connectionRequestedAt || !status.lastAccount) return;
  const ig = await detectInstagramSession();
  if (!ig.loggedIn) {
    await saveAccountStatus(status.lastAccount || null, {
      error:
        "Instagram session missing or expired. Reopen Instagram and reconnect.",
      code: "ig_session_missing",
    });
    return;
  }
  console.log("Periodic Instagram session sync");
  await syncSession({
    ticket: status.connectionTicket,
  });
});

// ─── Messages from popup ───────────────────────────────────────────────

// ─── Messages from an EngageMate page (via content-script.js) ──────────
//
// These branches are the only way a web page can make the extension act.
// The content script has already validated shape and origin, but it runs in
// the page, so it is not trusted: everything is re-validated here.

const ONBOARDING_NONCE = /^[A-Za-z0-9_-]{32,128}$/;
const ONBOARDING_TICKET = /^em_xt_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;

// Codes the page bridge is allowed to render. Anything else collapses to
// sync_failed rather than leaking an upstream message into the DOM.
const ONBOARDING_ERROR_CODES = new Set([
  "ig_session_missing",
  "auth_required",
  "account_conflict",
  "account_limit",
  "server_error",
  "network_error",
  "identity_discovery_failed",
  "identity_rate_limited",
  "geo_discovery_failed",
  "instagram_open_failed",
  "instagram_access_required",
  "sync_failed",
]);

ext.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "ONBOARDING_PING") {
    if (
      !ONBOARDING_NONCE.test(message.nonce || "") ||
      !onboardingOrigin(sender)
    ) {
      sendResponse({ status: "error", code: "invalid_connection_input" });
      return false;
    }
    sendResponse({ status: "installed" });
    return false;
  }

  if (message.type === "ONBOARDING_CONNECT") {
    const origin = onboardingOrigin(sender);
    if (
      !ONBOARDING_NONCE.test(message.nonce || "") ||
      !ONBOARDING_TICKET.test(message.ticket || "") ||
      !origin
    ) {
      sendResponse({ status: "error", code: "invalid_connection_input" });
      return false;
    }

    (async () => {
      const result = await connectInstagram(origin, message.ticket);
      if (result.status === "opening_instagram") {
        sendResponse({ status: "opening_instagram" });
        return;
      }
      if (result.success) {
        sendResponse({ status: "connected" });
        return;
      }
      sendResponse({
        status: "error",
        code: ONBOARDING_ERROR_CODES.has(result.code)
          ? result.code
          : "sync_failed",
      });
    })().catch(() => {
      sendResponse({ status: "error", code: "instagram_open_failed" });
    });
    return true;
  }

  if (message.type === "ONBOARDING_PROFILE_LOOKUP") {
    const origin = onboardingOrigin(sender);
    if (
      !ONBOARDING_NONCE.test(message.nonce || "") ||
      !origin ||
      !Array.isArray(message.usernames) ||
      message.usernames.length < 1 ||
      message.usernames.length > 5 ||
      message.usernames.some((value) => !INSTAGRAM_USERNAME.test(value)) ||
      new Set(message.usernames).size !== message.usernames.length
    ) {
      sendResponse({ success: false, code: "identity_discovery_failed" });
      return false;
    }
    discoverInstagramProfiles(message.usernames)
      .then(sendResponse)
      .catch(() => sendResponse({ success: false, code: "network_error" }));
    return true;
  }

  return false;
});

ext.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "GET_STATUS") {
    (async () => {
      const [auth, ig, stored] = await Promise.all([
        checkAuth(),
        detectInstagramSession(),
        getAccountStatus(),
      ]);
      sendResponse({
        auth,
        instagram: ig,
        account: stored.lastAccount || null,
        lastSyncAt: stored.lastSyncAt || null,
        lastError: stored.lastError || null,
        lastErrorCode: stored.lastErrorCode || null,
        connectionRequested: Boolean(stored.connectionRequestedAt),
      });
    })();
    return true;
  }

  if (message.type === "CONNECT_INSTAGRAM") {
    getAccountStatus()
      .then((status) => connectInstagram(null, status.connectionTicket))
      .then(sendResponse);
    return true;
  }

  if (message.type === "DETECT_INSTAGRAM") {
    detectInstagramSession().then(sendResponse);
    return true;
  }

  return false;
});
