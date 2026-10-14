// EngageMate page bridge — the only channel between an EngageMate page and the
// extension service worker.
//
// The page cannot talk to the service worker directly, and the service worker
// holds the Instagram session. So this file is the whole trust boundary, and it
// is deliberately a whitelist in both directions: only named request types leave
// the page, and only named, non-secret fields come back. Cookies, proxy URLs,
// and raw upstream errors reach the service worker but never reach the DOM.
// Connect requests carry intent only; account identity and geography never
// cross this page boundary. Profile lookups return bounded public fields only.

const REQUEST_TYPES = new Set([
  "ENGAGEMATE_EXTENSION_PING",
  "ENGAGEMATE_EXTENSION_CONNECT",
  "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP",
  "ENGAGEMATE_EXTENSION_PING_V2",
  "ENGAGEMATE_EXTENSION_CONNECT_V2",
  "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP_V2",
]);
const TARGETED_REQUEST_TYPES = new Set([
  "ENGAGEMATE_EXTENSION_CONNECT_V2",
  "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP_V2",
]);
const NONCE = /^[A-Za-z0-9_-]{32,128}$/;
const TICKET = /^em_xt_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;
const USERNAME = /^[a-z0-9._]{1,64}$/;
const STATUSES = new Set([
  "installed",
  "opening_instagram",
  "waiting_for_session",
  "syncing",
  "connected",
  "error",
]);
const ERROR_CODES = new Set([
  "invalid_connection_input",
  "instagram_open_failed",
  "ig_session_missing",
  "auth_required",
  "account_conflict",
  "account_limit",
  "server_error",
  "network_error",
  "identity_discovery_failed",
  "identity_rate_limited",
  "geo_discovery_failed",
  "instagram_access_required",
  "sync_failed",
]);

// Chrome exposes `chrome.runtime`; Safari also exposes `browser.runtime`.
// A page element with id="browser" is visible to content scripts as a window
// property, so only a real sendMessage function selects Safari's namespace.
function extensionRuntime() {
  const safari = globalThis.browser?.runtime;
  return typeof safari?.sendMessage === "function"
    ? safari
    : globalThis.chrome.runtime;
}

window.addEventListener("message", async (event) => {
  if (event.source !== window || event.origin !== window.location.origin) return;
  const request = event.data;
  if (!request || !REQUEST_TYPES.has(request.type) || !NONCE.test(request.nonce || "")) return;
  const runtime = extensionRuntime();
  if (
    TARGETED_REQUEST_TYPES.has(request.type) &&
    request.extensionId !== runtime.id
  ) {
    return;
  }

  const targeted = request.type.endsWith("_V2");
  const extensionIdentity = targeted
    ? {
        extensionId: runtime.id,
        version: runtime.getManifest().version,
      }
    : {};

  let payload;
  if (
    request.type === "ENGAGEMATE_EXTENSION_PING" ||
    request.type === "ENGAGEMATE_EXTENSION_PING_V2"
  ) {
    payload = { type: "ONBOARDING_PING", nonce: request.nonce };
  } else if (
    request.type === "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP" ||
    request.type === "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP_V2"
  ) {
    if (
      !Array.isArray(request.usernames) ||
      request.usernames.length < 1 ||
      request.usernames.length > 5 ||
      request.usernames.some((value) => !USERNAME.test(value)) ||
      new Set(request.usernames).size !== request.usernames.length
    ) {
      return;
    }
    payload = {
      type: "ONBOARDING_PROFILE_LOOKUP",
      nonce: request.nonce,
      usernames: [...request.usernames],
    };
  } else {
    payload = {
          type: "ONBOARDING_CONNECT",
          nonce: request.nonce,
          ...(TICKET.test(request.ticket || "")
            ? { ticket: request.ticket }
            : {}),
        };
  }

  const raw = await runtime.sendMessage(payload).catch(() => ({
    status: "error",
    code: "network_error",
  }));

  if (
    request.type === "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP" ||
    request.type === "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP_V2"
  ) {
    const profiles = Array.isArray(raw?.profiles)
      ? raw.profiles
          .filter((profile) => profile && USERNAME.test(profile.username || ""))
          .slice(0, 5)
          .map((profile) => ({
            username: profile.username,
            fullName: String(profile.fullName || "").slice(0, 256),
            biography: String(profile.biography || "").slice(0, 2_200),
            followersCount: profile.followersCount,
            followingCount: profile.followingCount,
            postsCount: profile.postsCount,
            isPrivate: Boolean(profile.isPrivate),
            isVerified: Boolean(profile.isVerified),
            latestPosts: Array.isArray(profile.latestPosts)
              ? profile.latestPosts.slice(0, 12).map((post) => ({
                  likesCount: post.likesCount,
                  commentsCount: post.commentsCount,
                  caption: String(post.caption || "").slice(0, 2_200),
                }))
              : [],
          }))
      : [];
    const success = raw?.success === true && profiles.length === request.usernames.length;
    window.postMessage(
      {
        type: targeted
          ? "ENGAGEMATE_EXTENSION_PROFILES_V2"
          : "ENGAGEMATE_EXTENSION_PROFILES",
        nonce: request.nonce,
        ...extensionIdentity,
        success,
        ...(success
          ? { profiles }
          : { code: ERROR_CODES.has(raw?.code) ? raw.code : "sync_failed" }),
      },
      window.location.origin,
    );
    return;
  }

  const status = STATUSES.has(raw?.status) ? raw.status : "error";
  const response = {
    status,
    ...(status === "error"
      ? { code: ERROR_CODES.has(raw?.code) ? raw.code : "sync_failed" }
      : {}),
  };

  window.postMessage(
    {
      type: targeted
        ? "ENGAGEMATE_EXTENSION_STATUS_V2"
        : "ENGAGEMATE_EXTENSION_STATUS",
      nonce: request.nonce,
      ...extensionIdentity,
      ...response,
    },
    window.location.origin,
  );
});
