/**
 * The content script is the extension's trust boundary.
 *
 * It runs in the page, so anything it accepts is reachable by any script the
 * page loads, and anything it echoes back is readable from the DOM. These are
 * behavioral tests against the real file — not a copy — because a source-string
 * assertion would pass on a bridge that forwards everything.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const SOURCE = readFileSync(
  path.resolve(__dirname, "../../../../extension/content-script.js"),
  "utf-8",
);

const NONCE = "a".repeat(64);
const TICKET = "em_xt_dGVzdC1wYXlsb2Fk.dGVzdC1zaWduYXR1cmU";
const ORIGIN = window.location.origin;
const EXTENSION_ID = "a".repeat(32);
const EXTENSION_VERSION = "3.5.0";

let sendMessage: ReturnType<typeof vi.fn>;
let posted: unknown[];
let foreignWindow: Window;

// The script registers a permanent window listener, exactly as it does in a
// real page. Loading it per test would stack listeners and every assertion
// after the first would see duplicated status messages.
function loadContentScript() {
  new Function(SOURCE)();
}

function send(data: unknown, init: { origin?: string; source?: Window | null } = {}) {
  window.dispatchEvent(
    new MessageEvent("message", {
      data,
      origin: init.origin ?? ORIGIN,
      source: "source" in init ? init.source : window,
    }),
  );
}

/** The handler is async; let its sendMessage and postMessage settle. */
async function settle() {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

beforeAll(() => {
  loadContentScript();
});

beforeEach(() => {
  posted = [];
  sendMessage = vi.fn().mockResolvedValue({ status: "installed" });
  (globalThis as unknown as { chrome: unknown }).chrome = {
    runtime: {
      id: EXTENSION_ID,
      getManifest: () => ({ version: EXTENSION_VERSION }),
      sendMessage,
    },
  };
  vi.spyOn(window, "postMessage").mockImplementation((message: unknown) => {
    posted.push(message);
  });

  const frame = document.createElement("iframe");
  document.body.appendChild(frame);
  foreignWindow = frame.contentWindow as Window;
});

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("page-to-extension bridge", () => {
  it("forwards a valid same-origin ping", async () => {
    send({ type: "ENGAGEMATE_EXTENSION_PING", nonce: NONCE });
    await settle();

    expect(sendMessage).toHaveBeenCalledWith({
      type: "ONBOARDING_PING",
      nonce: NONCE,
    });
    expect(posted).toEqual([
      { type: "ENGAGEMATE_EXTENSION_STATUS", nonce: NONCE, status: "installed" },
    ]);
  });

  it("identifies the current extension instance over the targeted bridge", async () => {
    send({ type: "ENGAGEMATE_EXTENSION_PING_V2", nonce: NONCE });
    await settle();

    expect(sendMessage).toHaveBeenCalledWith({
      type: "ONBOARDING_PING",
      nonce: NONCE,
    });
    expect(posted).toEqual([
      {
        type: "ENGAGEMATE_EXTENSION_STATUS_V2",
        nonce: NONCE,
        status: "installed",
        extensionId: EXTENSION_ID,
        version: EXTENSION_VERSION,
      },
    ]);
  });

  it("ignores a targeted request meant for another installed copy", async () => {
    send({
      type: "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP_V2",
      nonce: NONCE,
      extensionId: "b".repeat(32),
      usernames: ["source.one"],
    });
    await settle();

    expect(sendMessage).not.toHaveBeenCalled();
    expect(posted).toEqual([]);
  });

  it("ignores a message from a foreign origin", async () => {
    send(
      { type: "ENGAGEMATE_EXTENSION_PING", nonce: NONCE },
      { origin: "https://evil.example" },
    );
    await settle();

    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("ignores a message from another window", async () => {
    send({ type: "ENGAGEMATE_EXTENSION_PING", nonce: NONCE }, { source: foreignWindow });
    await settle();

    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("ignores a malformed nonce", async () => {
    send({ type: "ENGAGEMATE_EXTENSION_PING", nonce: "short" });
    await settle();

    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("ignores an unknown message type", async () => {
    send({ type: "ENGAGEMATE_EXTENSION_DRAIN_COOKIES", nonce: NONCE });
    await settle();

    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("does not accept user-supplied identity or geography", async () => {
    sendMessage.mockResolvedValue({ status: "opening_instagram" });
    send({
      type: "ENGAGEMATE_EXTENSION_CONNECT",
      nonce: NONCE,
      ticket: TICKET,
      username: "not a handle!",
      loginGeo: { country: "United States" },
    });
    await settle();

    expect(sendMessage).toHaveBeenCalledWith({
      type: "ONBOARDING_CONNECT",
      nonce: NONCE,
      ticket: TICKET,
    });
  });

  it("never echoes cookies, proxy credentials, or raw upstream errors", async () => {
    sendMessage.mockResolvedValue({
      status: "connected",
      cookies: [{ name: "sessionid", value: "IGSESSION-super-secret" }],
      proxy_url: "http://user:pass@proxy.example:8080",
      raw_error: "psycopg2 OperationalError at 10.0.0.4",
      account: {
        id: "acct-1",
        username: "engagemate",
        health: "active",
        captured_at: "2026-09-03T00:00:00Z",
        proxy_bound: true,
        cookies: [{ name: "sessionid", value: "IGSESSION-super-secret" }],
        proxy_url: "http://user:pass@proxy.example:8080",
      },
    });

    send({
      type: "ENGAGEMATE_EXTENSION_CONNECT",
      nonce: NONCE,
      ticket: TICKET,
    });
    await settle();

    const [status] = posted as Record<string, unknown>[];
    expect(status).toEqual({
      type: "ENGAGEMATE_EXTENSION_STATUS",
      nonce: NONCE,
      status: "connected",
    });

    const serialized = JSON.stringify(status);
    expect(serialized).not.toContain("IGSESSION-super-secret");
    expect(serialized).not.toContain("proxy.example");
    expect(serialized).not.toContain("OperationalError");
    expect(serialized).not.toContain("cookies");
    expect(serialized).not.toContain("proxy_url");
    expect(serialized).not.toContain("raw_error");
  });

  it("reports a bounded error code when the service worker is unreachable", async () => {
    sendMessage.mockRejectedValue(new Error("Receiving end does not exist"));
    send({ type: "ENGAGEMATE_EXTENSION_PING", nonce: NONCE });
    await settle();

    expect(posted).toEqual([
      {
        type: "ENGAGEMATE_EXTENSION_STATUS",
        nonce: NONCE,
        status: "error",
        code: "network_error",
      },
    ]);
  });

  it("collapses an unrecognized status or code to a safe pair", async () => {
    sendMessage.mockResolvedValue({ status: "exfiltrating", code: "stack_trace" });
    send({ type: "ENGAGEMATE_EXTENSION_PING", nonce: NONCE });
    await settle();

    expect(posted).toEqual([
      {
        type: "ENGAGEMATE_EXTENSION_STATUS",
        nonce: NONCE,
        status: "error",
        code: "sync_failed",
      },
    ]);
  });

  it("forwards a bounded profile lookup and returns public fields only", async () => {
    sendMessage.mockResolvedValue({
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
          latestPosts: [{ likesCount: 48, commentsCount: 3, caption: "A useful post" }],
          cookies: [{ name: "sessionid", value: "secret-session" }],
          profilePicUrl: "https://cdn.example/private-token",
        },
      ],
    });

    send({
      type: "ENGAGEMATE_EXTENSION_PROFILE_LOOKUP",
      nonce: NONCE,
      usernames: ["source.one"],
    });
    await settle();

    expect(sendMessage).toHaveBeenCalledWith({
      type: "ONBOARDING_PROFILE_LOOKUP",
      nonce: NONCE,
      usernames: ["source.one"],
    });
    expect(posted).toEqual([
      {
        type: "ENGAGEMATE_EXTENSION_PROFILES",
        nonce: NONCE,
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
      },
    ]);
    expect(JSON.stringify(posted)).not.toContain("secret-session");
    expect(JSON.stringify(posted)).not.toContain("private-token");
  });

  it("passes the page-renderable instagram_access_required code through", async () => {
    sendMessage.mockResolvedValue({
      status: "error",
      code: "instagram_access_required",
    });

    send({ type: "ENGAGEMATE_EXTENSION_CONNECT", nonce: NONCE, ticket: TICKET });
    await settle();

    expect(posted).toEqual([
      {
        type: "ENGAGEMATE_EXTENSION_STATUS",
        nonce: NONCE,
        status: "error",
        code: "instagram_access_required",
      },
    ]);
  });
});

describe("browser namespace selection", () => {
  const SAFARI_ID = "io.engagemate.extension.Extension (ABCDE12345)";

  afterEach(() => {
    delete (globalThis as unknown as { browser?: unknown }).browser;
  });

  it("uses Safari's browser runtime when it exists", async () => {
    const safariSend = vi.fn().mockResolvedValue({ status: "installed" });
    (globalThis as unknown as { browser: unknown }).browser = {
      runtime: {
        id: SAFARI_ID,
        getManifest: () => ({ version: EXTENSION_VERSION }),
        sendMessage: safariSend,
      },
    };

    send({ type: "ENGAGEMATE_EXTENSION_PING_V2", nonce: NONCE });
    await settle();

    expect(safariSend).toHaveBeenCalledWith({ type: "ONBOARDING_PING", nonce: NONCE });
    expect(sendMessage).not.toHaveBeenCalled();
    expect(posted).toEqual([
      {
        type: "ENGAGEMATE_EXTENSION_STATUS_V2",
        nonce: NONCE,
        status: "installed",
        extensionId: SAFARI_ID,
        version: EXTENSION_VERSION,
      },
    ]);
  });

  it("does not let a page element named browser replace the runtime", async () => {
    // Browsers expose <form id="browser"> as window.browser and its control
    // named "runtime" as browser.runtime. jsdom implements neither lookup, so
    // build the same shape by hand: DOM nodes that carry no sendMessage.
    const runtime = document.createElement("input");
    runtime.name = "runtime";
    (globalThis as unknown as { browser: unknown }).browser = { runtime };

    send({ type: "ENGAGEMATE_EXTENSION_PING_V2", nonce: NONCE });
    await settle();

    expect(sendMessage).toHaveBeenCalledWith({ type: "ONBOARDING_PING", nonce: NONCE });
    expect(posted).toEqual([
      {
        type: "ENGAGEMATE_EXTENSION_STATUS_V2",
        nonce: NONCE,
        status: "installed",
        extensionId: EXTENSION_ID,
        version: EXTENSION_VERSION,
      },
    ]);
  });
});
