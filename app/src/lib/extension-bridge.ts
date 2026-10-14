/**
 * Typed page-side half of the EngageMate extension bridge.
 *
 * The page and the content script share a window, so any script on the page
 * can post a message that looks like ours. A nonce per request is what makes a
 * reply attributable: a status without the outstanding nonce is discarded
 * rather than rendered.
 */

export type ExtensionStatus =
  | "installed"
  | "opening_instagram"
  | "waiting_for_session"
  | "syncing"
  | "connected"
  | "error";

export interface ExtensionStatusMessage {
  status: ExtensionStatus;
  code?: string;
}

export const EXTENSION_PING = "ENGAGEMATE_EXTENSION_PING";
export const EXTENSION_CONNECT = "ENGAGEMATE_EXTENSION_CONNECT";
export const EXTENSION_PROFILE_LOOKUP = "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP";
export const EXTENSION_PING_V2 = "ENGAGEMATE_EXTENSION_PING_V2";
export const EXTENSION_CONNECT_V2 = "ENGAGEMATE_EXTENSION_CONNECT_V2";
export const EXTENSION_PROFILE_LOOKUP_V2 =
  "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP_V2";
export const REQUIRED_EXTENSION_VERSION = "3.5.0";

const STATUS_TYPE = "ENGAGEMATE_EXTENSION_STATUS";
const STATUS_TYPE_V2 = "ENGAGEMATE_EXTENSION_STATUS_V2";
const PROFILES_TYPE_V2 = "ENGAGEMATE_EXTENSION_PROFILES_V2";
const USERNAME = /^[a-z0-9._]{1,64}$/;
// Chrome derives a 32-letter ID from the extension key. Safari reports the
// extension's bundle identifier and signing team instead, for example
// "io.engagemate.extension.Extension (ABCDE12345)".
const CHROME_EXTENSION_ID = /^[a-p]{32}$/;
const SAFARI_EXTENSION_ID =
  /^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?: \([A-Za-z0-9_-]{1,32}\))?$/;
const MAX_EXTENSION_ID_LENGTH = 200;
const MAX_PROFILE_COUNT = 5;
const EXTENSION_DISCOVERY_TIMEOUT_MS = 750;
// Profile reads have a bounded machine deadline. Instagram sign-in is a human
// flow and gets a separate window long enough for 2FA or a security challenge.
export const EXTENSION_PROFILE_LOOKUP_TIMEOUT_MS = 90_000;
export const INSTAGRAM_SIGN_IN_TIMEOUT_MS = 10 * 60_000;

export interface ExtensionProfilePost {
  likesCount: number;
  commentsCount: number;
  caption: string;
}

export interface ExtensionPublicProfile {
  username: string;
  fullName: string;
  biography: string;
  followersCount: number;
  followingCount: number;
  postsCount: number;
  isPrivate: boolean;
  isVerified: boolean;
  latestPosts: ExtensionProfilePost[];
}

export interface ExtensionInstance {
  extensionId: string;
  version: string;
}

const STATUSES = new Set<ExtensionStatus>([
  "installed",
  "opening_instagram",
  "waiting_for_session",
  "syncing",
  "connected",
  "error",
]);

/** True for an extension ID Chrome or Safari could have assigned. */
export function isExtensionId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length <= MAX_EXTENSION_ID_LENGTH &&
    (CHROME_EXTENSION_ID.test(value) || SAFARI_EXTENSION_ID.test(value))
  );
}

/** Post a nonce-bound intent to the content script and return the nonce. */
export function createExtensionRequest(
  type: string,
  fields: Record<string, unknown> = {},
): string {
  const nonce =
    crypto.randomUUID().replaceAll("-", "") + crypto.randomUUID().replaceAll("-", "");
  window.postMessage({ type, nonce, ...fields }, window.location.origin);
  return nonce;
}

/** Find one current extension copy before sending it any account work. */
export function requestExtensionInstance(
  timeoutMs = EXTENSION_DISCOVERY_TIMEOUT_MS,
): Promise<ExtensionInstance> {
  return new Promise((resolve, reject) => {
    const nonce =
      crypto.randomUUID().replaceAll("-", "") +
      crypto.randomUUID().replaceAll("-", "");
    const cleanup = () => {
      window.removeEventListener("message", listener);
      clearTimeout(timer);
    };
    const listener = (event: MessageEvent) => {
      if (event.source !== window || event.origin !== window.location.origin) return;
      const data = event.data as Record<string, unknown> | null;
      if (
        !data ||
        data.type !== STATUS_TYPE_V2 ||
        data.nonce !== nonce ||
        data.status !== "installed" ||
        !isExtensionId(data.extensionId) ||
        data.version !== REQUIRED_EXTENSION_VERSION
      ) {
        return;
      }
      cleanup();
      resolve({
        extensionId: data.extensionId,
        version: REQUIRED_EXTENSION_VERSION,
      });
    };

    window.addEventListener("message", listener);
    const timer = setTimeout(() => {
      cleanup();
      reject(
        new Error(
          "EngageMate could not reach the browser extension. Reconnect Instagram and try again.",
        ),
      );
    }, timeoutMs);
    window.postMessage(
      { type: EXTENSION_PING_V2, nonce },
      window.location.origin,
    );
  });
}

function isBoundedCount(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0 && Number(value) <= 2_147_483_647;
}

function publicProfile(
  value: unknown,
  expectedUsername: string,
): ExtensionPublicProfile | null {
  if (!value || typeof value !== "object") return null;
  const profile = value as Record<string, unknown>;
  if (
    profile.username !== expectedUsername ||
    typeof profile.fullName !== "string" ||
    profile.fullName.length > 256 ||
    typeof profile.biography !== "string" ||
    profile.biography.length > 2_200 ||
    !isBoundedCount(profile.followersCount) ||
    !isBoundedCount(profile.followingCount) ||
    !isBoundedCount(profile.postsCount) ||
    typeof profile.isPrivate !== "boolean" ||
    typeof profile.isVerified !== "boolean" ||
    !Array.isArray(profile.latestPosts) ||
    profile.latestPosts.length > 12
  ) {
    return null;
  }
  const latestPosts: ExtensionProfilePost[] = [];
  for (const value of profile.latestPosts) {
    if (!value || typeof value !== "object") return null;
    const post = value as Record<string, unknown>;
    if (
      !isBoundedCount(post.likesCount) ||
      !isBoundedCount(post.commentsCount) ||
      typeof post.caption !== "string" ||
      post.caption.length > 2_200
    ) {
      return null;
    }
    latestPosts.push({
      likesCount: post.likesCount,
      commentsCount: post.commentsCount,
      caption: post.caption,
    });
  }
  return {
    username: profile.username,
    fullName: profile.fullName,
    biography: profile.biography,
    followersCount: profile.followersCount,
    followingCount: profile.followingCount,
    postsCount: profile.postsCount,
    isPrivate: profile.isPrivate,
    isVerified: profile.isVerified,
    latestPosts,
  };
}

const PROFILE_ERROR_MESSAGES: Record<string, string> = {
  ig_session_missing: "Instagram session expired. Reconnect Instagram and try again.",
  identity_rate_limited:
    "Instagram is limiting profile checks. Wait a few minutes and try again.",
  network_error: "EngageMate could not reach Instagram. Try again.",
};

/** Ask the authenticated extension for bounded public profile snapshots. */
export async function requestExtensionProfiles(
  usernames: string[],
  timeoutMs = EXTENSION_PROFILE_LOOKUP_TIMEOUT_MS,
): Promise<ExtensionPublicProfile[]> {
  if (
    usernames.length < 1 ||
    usernames.length > MAX_PROFILE_COUNT ||
    usernames.some((username) => !USERNAME.test(username)) ||
    new Set(usernames).size !== usernames.length
  ) {
    throw new Error("Invalid Instagram source accounts");
  }

  const instance = await requestExtensionInstance();

  return new Promise((resolve, reject) => {
    const nonce =
      crypto.randomUUID().replaceAll("-", "") +
      crypto.randomUUID().replaceAll("-", "");
    const cleanup = () => {
      window.removeEventListener("message", listener);
      clearTimeout(timer);
    };
    const listener = (event: MessageEvent) => {
      if (event.source !== window || event.origin !== window.location.origin) return;
      const data = event.data as Record<string, unknown> | null;
      if (
        !data ||
        data.type !== PROFILES_TYPE_V2 ||
        data.nonce !== nonce ||
        data.extensionId !== instance.extensionId ||
        data.version !== instance.version
      ) {
        return;
      }
      cleanup();
      if (data.success !== true || !Array.isArray(data.profiles)) {
        const code = typeof data.code === "string" ? data.code : "sync_failed";
        reject(
          new Error(
            PROFILE_ERROR_MESSAGES[code] ||
              "EngageMate could not read those Instagram profiles. Try again.",
          ),
        );
        return;
      }
      if (data.profiles.length !== usernames.length) {
        reject(new Error("EngageMate could not read every Instagram source account."));
        return;
      }
      const profiles = data.profiles.map((profile, index) =>
        publicProfile(profile, usernames[index]),
      );
      if (profiles.some((profile) => profile === null)) {
        reject(new Error("Instagram returned an invalid source profile."));
        return;
      }
      resolve(profiles as ExtensionPublicProfile[]);
    };

    window.addEventListener("message", listener);
    const timer = setTimeout(() => {
      cleanup();
      reject(
        new Error(
          "EngageMate could not reach the browser extension. Reconnect Instagram and try again.",
        ),
      );
    }, timeoutMs);
    window.postMessage(
      {
        type: EXTENSION_PROFILE_LOOKUP_V2,
        nonce,
        extensionId: instance.extensionId,
        usernames: [...usernames],
      },
      window.location.origin,
    );
  });
}

interface StatusListenerOptions {
  /** Give up after this long. Omit to listen until cleanup. */
  timeoutMs?: number;
  onTimeout?: () => void;
  /** Require a reply from this exact installed extension copy. */
  instance?: ExtensionInstance;
}

/**
 * Listen for the reply to one request.
 *
 * Returns a cleanup function that removes the listener and clears the timeout,
 * so an unmounting component leaves nothing behind.
 */
export function onExtensionStatus(
  nonce: string,
  handler: (message: ExtensionStatusMessage) => void,
  options: StatusListenerOptions = {},
): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const cleanup = () => {
    window.removeEventListener("message", listener);
    if (timer !== undefined) {
      clearTimeout(timer);
      timer = undefined;
    }
  };

  const listener = (event: MessageEvent) => {
    if (event.source !== window) return;
    if (event.origin !== window.location.origin) return;

    const data = event.data as
      | (ExtensionStatusMessage & {
          type?: string;
          nonce?: string;
          extensionId?: string;
          version?: string;
        })
      | null;
    const expectedType = options.instance ? STATUS_TYPE_V2 : STATUS_TYPE;
    if (!data || data.type !== expectedType || data.nonce !== nonce) return;
    if (
      options.instance &&
      (data.extensionId !== options.instance.extensionId ||
        data.version !== options.instance.version)
    ) {
      return;
    }
    if (!STATUSES.has(data.status)) return;

    cleanup();
    handler({
      status: data.status,
      ...(data.code ? { code: data.code } : {}),
    });
  };

  window.addEventListener("message", listener);

  if (options.timeoutMs !== undefined) {
    timer = setTimeout(() => {
      cleanup();
      options.onTimeout?.();
    }, options.timeoutMs);
  }

  return cleanup;
}

/**
 * True when this browser cannot install a Chrome extension.
 *
 * `userAgentData` is authoritative where it exists; the string fallback covers
 * Safari and Firefox on iOS and Android.
 */
export function isMobileBrowser(): boolean {
  const data = (navigator as Navigator & { userAgentData?: { mobile?: boolean } })
    .userAgentData;
  if (typeof data?.mobile === "boolean") return data.mobile;
  return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent || "");
}

// Every Chromium browser, plus Firefox and the iOS shells, also says "Safari"
// in its user agent. Brave and Arc report a plain Chrome string.
const NOT_SAFARI = /Chrome|Chromium|CriOS|FxiOS|EdgiOS|Edg\/|OPR\/|Brave|Firefox/i;

/**
 * True for Safari on a Mac, the one desktop browser that cannot load the
 * unpacked Chrome package and needs the EngageMate for Safari app instead.
 */
export function isDesktopSafari(): boolean {
  if (isMobileBrowser()) return false;
  const nav = navigator as Navigator & {
    userAgentData?: { brands?: unknown[] };
  };
  // Only Chromium implements User-Agent Client Hints.
  const brands = nav.userAgentData?.brands;
  if (Array.isArray(brands) && brands.length > 0) return false;
  const userAgent = nav.userAgent || "";
  if (!/Macintosh/.test(userAgent) || !/Safari\//.test(userAgent)) return false;
  if (NOT_SAFARI.test(userAgent)) return false;
  // iPadOS requests desktop sites with a Mac user agent but has touch input,
  // and it cannot run the macOS app.
  return (nav.maxTouchPoints ?? 0) <= 1;
}

/**
 * The EngageMate for Safari download, or null when none is published.
 *
 * NEXT_PUBLIC_* values are inlined at build time. Only an HTTPS URL or a
 * same-site path can become a link, so a malformed value cannot turn the
 * download button into a script URL.
 */
export function safariExtensionDownloadUrl(
  value: string | undefined = process.env.NEXT_PUBLIC_SAFARI_EXTENSION_URL,
): string | null {
  const candidate = (value ?? "").trim();
  if (!candidate) return null;
  if (candidate.startsWith("/") && !candidate.startsWith("//")) return candidate;
  try {
    const url = new URL(candidate);
    return url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}
