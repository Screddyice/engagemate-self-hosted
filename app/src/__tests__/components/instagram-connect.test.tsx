/**
 * The connection component is the only place onboarding can be blocked or
 * unblocked, so its state machine is the gate on the whole product.
 *
 * Two properties matter more than the happy path: it must never render
 * session material, and it must stop polling. A component that keeps a
 * two-second interval alive after unmount leaks a request per tab per user.
 */

import React from "react";
import { render, screen, act, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InstagramConnect } from "@/components/platform-login/instagram-connect";
import extensionManifest from "../../../../extension/manifest.json";

const ORIGIN = "http://localhost:3000";
const TICKET = "em_xt_dGVzdC1wYXlsb2Fk.dGVzdC1zaWduYXR1cmU";
const EXTENSION_ID = "a".repeat(32);
const SAFARI_EXTENSION_ID = "io.engagemate.extension.Extension (ABCDE12345)";
const EXPECTED_SITE_HOST = "app.example.com";
const SAFARI_DOWNLOAD =
  "https://downloads.example.com/EngageMate-Safari-3.5.0.zip";
const SAFARI_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15";
const CHROME_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36";
const EDGE_MAC = `${CHROME_MAC} Edg/140.0.0.0`;

function useUserAgent(userAgent: string) {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(userAgent);
}

let postMessageSpy: ReturnType<typeof vi.spyOn>;
let fetchMock: ReturnType<typeof vi.fn>;

/** The nonce the component most recently sent to the content script. */
function postedNonce(): string {
  const calls = postMessageSpy.mock.calls;
  const last = calls[calls.length - 1]?.[0] as { nonce?: string } | undefined;
  return last?.nonce ?? "";
}

function lastPosted(): Record<string, unknown> {
  const calls = postMessageSpy.mock.calls;
  return (calls[calls.length - 1]?.[0] ?? {}) as Record<string, unknown>;
}

/** Deliver an extension status message the way the content script does. */
async function deliver(data: Record<string, unknown>, nonce = postedNonce()) {
  await act(async () => {
    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          type: "ENGAGEMATE_EXTENSION_STATUS_V2",
          nonce,
          extensionId: EXTENSION_ID,
          version: extensionManifest.version,
          ...data,
        },
        origin: ORIGIN,
        source: window,
      }),
    );
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  postMessageSpy = vi.spyOn(window, "postMessage").mockImplementation(() => {});
  fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    if (String(input) === "/api/extension/ticket") {
      return { ok: true, json: async () => ({ ticket: TICKET }) };
    }
    return { ok: true, json: async () => [] };
  });
  global.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("InstagramConnect", () => {
  it.each([401, 503, "network"])("distinguishes connection failure %s from expired login", async (status) => {
    fetchMock.mockImplementation(async () => {
      if (status === "network") throw new TypeError("private network details");
      return { ok: false, status, json: async () => ({ error: "private server details" }) };
    });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await deliver({ status: "installed" });
    await user.click(screen.getByRole("button", { name: "Connect Instagram" }));
    if (status === 401) {
      expect(screen.getByText(/session expired/i)).toBeInTheDocument();
    } else {
      expect(screen.getByText(/couldn't start the connection/i)).toBeInTheDocument();
      expect(screen.queryByText(/session expired/i)).not.toBeInTheDocument();
    }
    expect(screen.queryByText(/private .* details/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect Instagram" })).toBeEnabled();
  });

  it("expires a stalled ticket request without opening Instagram on a late response", async () => {
    let resolveTicket: ((response: unknown) => void) | undefined;
    fetchMock.mockImplementationOnce(() => new Promise((resolve) => { resolveTicket = resolve; }));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await deliver({ status: "installed" });
    await user.click(screen.getByRole("button", { name: "Connect Instagram" }));
    const posted = postMessageSpy.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(screen.getByText(/couldn't start the connection/i)).toBeInTheDocument();
    expect(screen.queryByText(/extension didn't respond/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect Instagram" })).toBeEnabled();
    await act(async () => {
      resolveTicket?.({ ok: true, json: async () => ({ ticket: TICKET }) });
      await Promise.resolve();
    });
    expect(postMessageSpy.mock.calls.length).toBe(posted);
  });

  it("recovers if the installed extension never acknowledges connection", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await deliver({ status: "installed" });
    await user.click(screen.getByRole("button", { name: "Connect Instagram" }));
    const staleNonce = postedNonce();
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(screen.getByText(/extension didn't respond/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect Instagram" })).toBeEnabled();
    await deliver({ status: "waiting_for_session" }, staleNonce);
    expect(screen.queryByText(/waiting for you/i)).not.toBeInTheDocument();
  });

  it("detects the extension and offers Instagram sign-in", async () => {
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await deliver({ status: "installed" });

    expect(
      await screen.findByRole("button", { name: "Connect Instagram" }),
    ).toBeEnabled();
  });

  it("always offers the reviewed self-hosted download", async () => {
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(
      screen.getByRole("link", { name: /download the engagemate extension/i }),
    ).toHaveAttribute(
      "href",
      `/downloads/engagemate-extension-${extensionManifest.version}.zip`,
    );
    expect(screen.getByText("chrome://extensions")).toBeInTheDocument();
    expect(screen.getByText(/load unpacked/i)).toBeInTheDocument();
    expect(
      screen.getByText(/return to engagemate and refresh/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /add the chrome extension/i }),
    ).not.toBeInTheDocument();
  });

  it("keeps sign-in blocked and offers the download until the extension answers", async () => {
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(
      screen.getByRole("button", { name: "Connect Instagram" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("link", { name: /download the engagemate extension/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /add the chrome extension/i }),
    ).not.toBeInTheDocument();
  });

  it("keeps pinging so a newly loaded extension appears without a reload", async () => {
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const first = postedNonce();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(postedNonce()).not.toBe("");
    expect(postedNonce()).not.toBe(first);
  });

  it("ignores a status carrying the wrong nonce", async () => {
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await deliver({ status: "installed" }, "b".repeat(64));

    expect(
      screen.getByRole("button", { name: "Connect Instagram" }),
    ).toBeDisabled();
  });

  it("blocks progression on a mobile browser without contacting the extension", async () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(
      "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Mobile Safari/537.36",
    );

    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    expect(await screen.findByText(/continue on desktop/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Connect Instagram" }),
    ).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(postMessageSpy).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows Safari install steps when a Safari download is published", async () => {
    useUserAgent(SAFARI_MAC);
    vi.stubEnv("NEXT_PUBLIC_SAFARI_EXTENSION_URL", SAFARI_DOWNLOAD);
    vi.stubEnv("NEXT_PUBLIC_APP_URL", `https://${EXPECTED_SITE_HOST}`);
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(
      screen.getByRole("link", { name: /download engagemate for safari/i }),
    ).toHaveAttribute("href", SAFARI_DOWNLOAD);
    expect(screen.getByText(/applications folder, then open it once/i)).toBeInTheDocument();
    expect(screen.getByText(/settings, then extensions/i)).toBeInTheDocument();
    expect(screen.getByText("instagram.com")).toBeInTheDocument();
    // The host comes from NEXT_PUBLIC_APP_URL, never a built-in domain.
    expect(screen.getByText(EXPECTED_SITE_HOST)).toBeInTheDocument();
    expect(
      screen.getByText(/return to engagemate and refresh/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("chrome://extensions")).not.toBeInTheDocument();
    expect(screen.queryByText(/load unpacked/i)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /download the engagemate extension/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Connect Instagram" }),
    ).toBeDisabled();
  });

  it("sends Safari users to a Chromium browser while Safari support is pending", async () => {
    useUserAgent(SAFARI_MAC);
    vi.stubEnv("NEXT_PUBLIC_SAFARI_EXTENSION_URL", "");
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(
      screen.getByText(
        /open engagemate in chrome, edge, brave, or arc on this computer/i,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/safari support is pending/i)).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByText("chrome://extensions")).not.toBeInTheDocument();
  });

  it("treats a script URL as an unpublished Safari download", async () => {
    useUserAgent(SAFARI_MAC);
    vi.stubEnv("NEXT_PUBLIC_SAFARI_EXTENSION_URL", "javascript:alert(1)");
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByText(/safari support is pending/i)).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it.each([
    ["Chrome", CHROME_MAC],
    ["Edge", EDGE_MAC],
  ])("keeps the unpacked Chrome steps for %s on a Mac", async (_name, agent) => {
    useUserAgent(agent);
    vi.stubEnv("NEXT_PUBLIC_SAFARI_EXTENSION_URL", SAFARI_DOWNLOAD);
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(
      screen.getByRole("link", { name: /download the engagemate extension/i }),
    ).toHaveAttribute(
      "href",
      `/downloads/engagemate-extension-${extensionManifest.version}.zip`,
    );
    expect(screen.getByText("chrome://extensions")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /download engagemate for safari/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/safari support is pending/i)).not.toBeInTheDocument();
  });

  it("connects through a Safari extension identified by its bundle ID", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    useUserAgent(SAFARI_MAC);
    vi.stubEnv("NEXT_PUBLIC_SAFARI_EXTENSION_URL", SAFARI_DOWNLOAD);
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await deliver({ status: "installed", extensionId: SAFARI_EXTENSION_ID });

    const button = await screen.findByRole("button", {
      name: "Connect Instagram",
    });
    expect(button).toBeEnabled();
    expect(
      screen.queryByRole("link", { name: /download engagemate for safari/i }),
    ).not.toBeInTheDocument();

    await user.click(button);
    const sent = lastPosted();
    expect(sent.type).toBe("ENGAGEMATE_EXTENSION_CONNECT_V2");
    expect(sent.extensionId).toBe(SAFARI_EXTENSION_ID);
    expect(sent.ticket).toBe(TICKET);
  });

  it("sends only a connection ticket and lets the extension discover account details", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await deliver({ status: "installed" });

    expect(
      screen.queryByLabelText(/instagram username/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/login country/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Connect Instagram" }));

    const sent = lastPosted();
    expect(sent.type).toBe("ENGAGEMATE_EXTENSION_CONNECT_V2");
    expect(sent.extensionId).toBe(EXTENSION_ID);
    expect(sent.ticket).toBe(TICKET);
    expect(sent.username).toBeUndefined();
    expect(sent.loginGeo).toBeUndefined();
  });

  it("confirms the account with the server before reporting success", async () => {
    const onConnected = vi.fn();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<InstagramConnect accounts={[]} onConnected={onConnected} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await deliver({ status: "installed" });
    await user.click(screen.getByRole("button", { name: "Connect Instagram" }));

    await deliver({ status: "opening_instagram" });
    expect(onConnected).not.toHaveBeenCalled();

    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => [
        { id: "acct-1", username: "engagemate", healthStatus: "active" },
      ],
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    await waitFor(() =>
      expect(onConnected).toHaveBeenCalledWith(
        expect.objectContaining({ id: "acct-1", username: "engagemate" }),
      ),
    );
    expect(await screen.findByText(/^Instagram$/)).toBeInTheDocument();
    expect(screen.queryByText(/@engagemate/)).not.toBeInTheDocument();
  });

  it("keeps waiting through 2FA and stops after ten minutes", async () => {
    const onConnected = vi.fn();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<InstagramConnect accounts={[]} onConnected={onConnected} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await deliver({ status: "installed" });
    await user.click(screen.getByRole("button", { name: "Connect Instagram" }));
    await deliver({ status: "opening_instagram" });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(90_000);
    });
    expect(
      screen.getByText(/waiting for you to finish signing in/i),
    ).toBeInTheDocument();
    expect(onConnected).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(510_000);
    });
    const afterTimeout = fetchMock.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(fetchMock.mock.calls.length).toBe(afterTimeout);
    expect(onConnected).not.toHaveBeenCalled();
    expect(
      screen.getByText(/didn't see a connected account after 10 minutes/i),
    ).toBeInTheDocument();
  });

  it("clears its timers on unmount", async () => {
    const { unmount } = render(
      <InstagramConnect accounts={[]} onConnected={vi.fn()} />,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    unmount();
    const posted = postMessageSpy.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expect(postMessageSpy.mock.calls.length).toBe(posted);
  });

  it("does not finish a pending sign-in after unmount", async () => {
    let resolveTicket: ((response: unknown) => void) | undefined;
    fetchMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveTicket = resolve;
        }),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { unmount } = render(
      <InstagramConnect accounts={[]} onConnected={vi.fn()} />,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await deliver({ status: "installed" });
    await user.click(screen.getByRole("button", { name: "Connect Instagram" }));
    const posted = postMessageSpy.mock.calls.length;

    unmount();
    await act(async () => {
      resolveTicket?.({ ok: true, json: async () => ({ ticket: TICKET }) });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(postMessageSpy.mock.calls.length).toBe(posted);
  });

  it.each([
    ["ig_session_missing", /not signed in to instagram in this browser/i],
    ["auth_required", /sign in to engagemate again/i],
    ["account_conflict", /already connected/i],
    ["account_limit", /account limit/i],
    ["identity_discovery_failed", /could not identify/i],
    ["identity_rate_limited", /temporarily limiting/i],
    ["geo_discovery_failed", /could not prepare the connection/i],
    ["instagram_access_required", /not allowed on instagram\.com/i],
    ["server_error", /couldn't complete the connection/i],
    ["a_code_we_have_never_seen", /couldn't complete the connection/i],
  ])("renders safe copy for %s", async (code, copy) => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await deliver({ status: "installed" });
    await user.click(screen.getByRole("button", { name: "Connect Instagram" }));
    await deliver({ status: "error", code });

    expect(await screen.findByText(copy)).toBeInTheDocument();
  });

  it("never renders session material or raw upstream errors", async () => {
    render(<InstagramConnect accounts={[]} onConnected={vi.fn()} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await deliver({ status: "installed" });
    act(() => {
      screen.getByRole("button", { name: "Connect Instagram" }).click();
    });
    await deliver({
      status: "error",
      code: "server_error",
      cookies: [{ name: "sessionid", value: "IGSESSION-super-secret" }],
      raw_error: "psycopg2 OperationalError at 10.0.0.4",
    });

    const rendered = document.body.textContent ?? "";
    expect(rendered).not.toContain("sessionid");
    expect(rendered).not.toContain("IGSESSION-super-secret");
    expect(rendered).not.toContain("OperationalError");
    expect(rendered).not.toContain("10.0.0.4");
  });

  it("shows an existing healthy account without opening Instagram", async () => {
    render(
      <InstagramConnect
        accounts={[
          { id: "acct-1", username: "engagemate", healthStatus: "active" },
        ]}
        onConnected={vi.fn()}
      />,
    );

    expect(await screen.findByText(/^Instagram$/)).toBeInTheDocument();
    expect(screen.queryByText(/@engagemate/)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Connect Instagram" }),
    ).not.toBeInTheDocument();
  });

  it("asks an expired account to reconnect", async () => {
    render(
      <InstagramConnect
        accounts={[
          { id: "acct-1", username: "engagemate", healthStatus: "expired" },
        ]}
        onConnected={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("button", { name: "Connect Instagram" }),
    ).toBeInTheDocument();
  });
});
