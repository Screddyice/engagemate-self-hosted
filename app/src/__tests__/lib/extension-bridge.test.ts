import { afterEach, describe, expect, it, vi } from "vitest";

import {
  REQUIRED_EXTENSION_VERSION,
  isDesktopSafari,
  isExtensionId,
  safariExtensionDownloadUrl,
  onExtensionStatus,
  requestExtensionInstance,
  requestExtensionProfiles,
} from "@/lib/extension-bridge";

const EXTENSION_ID = "a".repeat(32);

function dispatch(data: Record<string, unknown>) {
  window.dispatchEvent(
    new MessageEvent("message", {
      origin: window.location.origin,
      source: window,
      data,
    }),
  );
}

async function answerCurrentPing(posted: Record<string, unknown>[]) {
  dispatch({
    type: "ENGAGEMATE_EXTENSION_STATUS_V2",
    nonce: String(posted[0].nonce),
    status: "installed",
    extensionId: EXTENSION_ID,
    version: REQUIRED_EXTENSION_VERSION,
  });
  await Promise.resolve();
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("requestExtensionProfiles", () => {
  it("returns only the nonce-bound public profile response", async () => {
    const posted: Record<string, unknown>[] = [];
    vi.spyOn(window, "postMessage").mockImplementation((message: unknown) => {
      posted.push(message as Record<string, unknown>);
    });

    const pending = requestExtensionProfiles(["source.one"]);
    await answerCurrentPing(posted);
    const request = posted[1];
    const nonce = String(request.nonce);

    dispatch({
      type: "ENGAGEMATE_EXTENSION_PROFILES_V2",
      nonce,
      extensionId: EXTENSION_ID,
      version: REQUIRED_EXTENSION_VERSION,
      success: true,
      profiles: [
        {
          username: "source.one",
          fullName: "Source One",
          biography: "Austin product builder",
          followersCount: 1200,
          followingCount: 75,
          postsCount: 40,
          isPrivate: false,
          isVerified: true,
          latestPosts: [
            { likesCount: 48, commentsCount: 3, caption: "A useful post" },
          ],
        },
      ],
    });

    await expect(pending).resolves.toEqual([
      {
        username: "source.one",
        fullName: "Source One",
        biography: "Austin product builder",
        followersCount: 1200,
        followingCount: 75,
        postsCount: 40,
        isPrivate: false,
        isVerified: true,
        latestPosts: [
          { likesCount: 48, commentsCount: 3, caption: "A useful post" },
        ],
      },
    ]);
    expect(posted[0]).toMatchObject({
      type: "ENGAGEMATE_EXTENSION_PING_V2",
    });
    expect(request).toMatchObject({
      type: "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP_V2",
      extensionId: EXTENSION_ID,
      usernames: ["source.one"],
    });
  });

  it("rejects a bounded extension failure code", async () => {
    const posted: Record<string, unknown>[] = [];
    vi.spyOn(window, "postMessage").mockImplementation((message: unknown) => {
      posted.push(message as Record<string, unknown>);
    });

    const pending = requestExtensionProfiles(["source.one"]);
    await answerCurrentPing(posted);
    dispatch({
      type: "ENGAGEMATE_EXTENSION_PROFILES_V2",
      nonce: String(posted[1].nonce),
      extensionId: EXTENSION_ID,
      version: REQUIRED_EXTENSION_VERSION,
      success: false,
      code: "ig_session_missing",
    });

    await expect(pending).rejects.toThrow("Instagram session expired");
  });

  it("ignores an older installed copy and targets the required version", async () => {
    const posted: Record<string, unknown>[] = [];
    vi.spyOn(window, "postMessage").mockImplementation((message: unknown) => {
      posted.push(message as Record<string, unknown>);
    });

    const pending = requestExtensionProfiles(["source.one"]);
    const pingNonce = String(posted[0].nonce);
    dispatch({
      type: "ENGAGEMATE_EXTENSION_STATUS_V2",
      nonce: pingNonce,
      status: "installed",
      extensionId: "b".repeat(32),
      version: "3.3.6",
    });
    await Promise.resolve();
    expect(posted).toHaveLength(1);

    dispatch({
      type: "ENGAGEMATE_EXTENSION_STATUS_V2",
      nonce: pingNonce,
      status: "installed",
      extensionId: EXTENSION_ID,
      version: REQUIRED_EXTENSION_VERSION,
    });
    await Promise.resolve();
    expect(posted[1]).toMatchObject({
      type: "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP_V2",
      extensionId: EXTENSION_ID,
    });

    dispatch({
      type: "ENGAGEMATE_EXTENSION_PROFILES_V2",
      nonce: String(posted[1].nonce),
      extensionId: EXTENSION_ID,
      version: REQUIRED_EXTENSION_VERSION,
      success: false,
      code: "ig_session_missing",
    });
    await expect(pending).rejects.toThrow("Instagram session expired");
  });

  it("keeps the page bridge open for the extension's maximum paced batch", async () => {
    vi.useFakeTimers();
    const posted: Record<string, unknown>[] = [];
    vi.spyOn(window, "postMessage").mockImplementation((message: unknown) => {
      posted.push(message as Record<string, unknown>);
    });

    const pending = requestExtensionProfiles([
      "source.one",
      "source.two",
      "source.three",
      "source.four",
      "source.five",
    ]);
    await answerCurrentPing(posted);
    let settled = false;
    void pending.then(
      () => {
        settled = true;
      },
      () => {
        settled = true;
      },
    );

    await vi.advanceTimersByTimeAsync(89_999);
    expect(settled).toBe(false);

    await vi.advanceTimersByTimeAsync(1);
    await expect(pending).rejects.toThrow("could not reach the browser extension");
  });
});

describe("onExtensionStatus", () => {
  it("handles only the first valid reply for one request", () => {
    const handler = vi.fn();
    const nonce = "c".repeat(64);

    onExtensionStatus(nonce, handler, {
      instance: {
        extensionId: EXTENSION_ID,
        version: REQUIRED_EXTENSION_VERSION,
      },
    });
    const response = {
      type: "ENGAGEMATE_EXTENSION_STATUS_V2",
      nonce,
      status: "connected",
      extensionId: EXTENSION_ID,
      version: REQUIRED_EXTENSION_VERSION,
    };

    dispatch(response);
    dispatch(response);

    expect(handler).toHaveBeenCalledOnce();
  });

  it("ignores a matching reply after its timeout", async () => {
    vi.useFakeTimers();
    const handler = vi.fn();
    const onTimeout = vi.fn();
    const nonce = "d".repeat(64);

    onExtensionStatus(nonce, handler, {
      timeoutMs: 100,
      onTimeout,
      instance: {
        extensionId: EXTENSION_ID,
        version: REQUIRED_EXTENSION_VERSION,
      },
    });
    await vi.advanceTimersByTimeAsync(100);

    dispatch({
      type: "ENGAGEMATE_EXTENSION_STATUS_V2",
      nonce,
      status: "connected",
      extensionId: EXTENSION_ID,
      version: REQUIRED_EXTENSION_VERSION,
    });

    expect(onTimeout).toHaveBeenCalledOnce();
    expect(handler).not.toHaveBeenCalled();
  });
});

const SAFARI_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15";

describe("isExtensionId", () => {
  it.each([
    ["a Chrome extension ID", "abcdefghijklmnopabcdefghijklmnop"],
    ["a signed Safari extension", "io.engagemate.extension.Extension (ABCDE12345)"],
    ["an unsigned Safari extension", "io.engagemate.extension.Extension"],
  ])("accepts %s", (_label, value) => {
    expect(isExtensionId(value)).toBe(true);
  });

  it.each([
    ["a non-string", 42],
    ["letters outside Chrome's alphabet", "z".repeat(32)],
    ["markup", "<img src=x>.example"],
    ["a name without a dot", "EngageMate"],
    ["an oversized identifier", `${"a".repeat(200)}.example`],
  ])("rejects %s", (_label, value) => {
    expect(isExtensionId(value)).toBe(false);
  });

  it("lets a Safari extension answer the discovery ping", async () => {
    const posted: Record<string, unknown>[] = [];
    vi.spyOn(window, "postMessage").mockImplementation((message: unknown) => {
      posted.push(message as Record<string, unknown>);
    });
    const safariId = "io.engagemate.extension.Extension (ABCDE12345)";

    const pending = requestExtensionInstance();
    dispatch({
      type: "ENGAGEMATE_EXTENSION_STATUS_V2",
      nonce: String(posted[0].nonce),
      status: "installed",
      extensionId: safariId,
      version: REQUIRED_EXTENSION_VERSION,
    });

    await expect(pending).resolves.toEqual({
      extensionId: safariId,
      version: REQUIRED_EXTENSION_VERSION,
    });
  });
});

describe("isDesktopSafari", () => {
  // jsdom has no maxTouchPoints, so the test defines it on the instance.
  function useAgent(userAgent: string, maxTouchPoints = 0) {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(userAgent);
    Object.defineProperty(navigator, "maxTouchPoints", {
      configurable: true,
      get: () => maxTouchPoints,
    });
  }

  afterEach(() => {
    Reflect.deleteProperty(navigator, "maxTouchPoints");
  });

  it("detects Safari on a Mac", () => {
    useAgent(SAFARI_MAC);
    expect(isDesktopSafari()).toBe(true);
  });

  it.each([
    [
      "Chrome",
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    ],
    [
      "Edge",
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
    ],
    [
      "Opera",
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 OPR/120.0.0.0",
    ],
    [
      "Firefox",
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:140.0) Gecko/20100101 Firefox/140.0",
    ],
    [
      "Safari on iPhone",
      "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
    ],
  ])("rejects %s", (_label, userAgent) => {
    useAgent(userAgent);
    expect(isDesktopSafari()).toBe(false);
  });

  it("rejects an iPad asking for the desktop site", () => {
    useAgent(SAFARI_MAC, 5);
    expect(isDesktopSafari()).toBe(false);
  });
});

describe("safariExtensionDownloadUrl", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("reads the build-time download URL", () => {
    vi.stubEnv(
      "NEXT_PUBLIC_SAFARI_EXTENSION_URL",
      "https://downloads.example.com/EngageMate-Safari-3.5.0.zip",
    );
    expect(safariExtensionDownloadUrl()).toBe(
      "https://downloads.example.com/EngageMate-Safari-3.5.0.zip",
    );
  });

  it.each([
    ["an unset value", undefined, null],
    ["a blank value", "  ", null],
    [
      "a same-site path",
      "/downloads/EngageMate-Safari.zip",
      "/downloads/EngageMate-Safari.zip",
    ],
    ["a protocol-relative URL", "//evil.example/app.zip", null],
    ["plain HTTP", "http://downloads.example.com/app.zip", null],
    ["a script URL", "javascript:alert(1)", null],
    ["garbage", "not a url", null],
  ])("handles %s", (_label, value, expected) => {
    expect(safariExtensionDownloadUrl(value)).toBe(expected);
  });
});
