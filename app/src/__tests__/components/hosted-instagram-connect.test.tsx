import React from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InstagramConnect } from "@/components/platform-login/instagram-connect";
const id = "12345678-1234-1234-1234-123456789abc";
const active = () => ({
  id,
  state: "active",
  expires_at: new Date(Date.now() + 600_000).toISOString(),
});
const fetchMock = vi.fn();
const openMock = vi.fn();
const changed = vi.fn();
function setup() {
  render(
    <InstagramConnect accounts={[]} onConnected={changed} hostedLoginEnabled />,
  );
  fireEvent.change(screen.getByLabelText("Instagram username"), {
    target: { value: "@demo_user" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Connect Instagram" }));
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(window, "open").mockImplementation(openMock);
  openMock.mockReturnValue(null);
  fetchMock.mockImplementation(() => Promise.resolve(Response.json(active())));
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
describe("hosted Instagram connection", () => {
  it("replaces a stale connected display when owned account props become paused", () => {
    const { rerender } = render(<InstagramConnect hostedLoginEnabled
      accounts={[{ id, username: "demo_user", healthStatus: "active" }]} onConnected={changed} />);
    expect(screen.getByText("Instagram connected")).toBeInTheDocument();
    rerender(<InstagramConnect hostedLoginEnabled
      accounts={[{ id, username: "demo_user", healthStatus: "paused" }]} onConnected={changed} />);
    expect(screen.queryByText("Instagram connected")).toBeNull();
    expect(screen.getByLabelText("Instagram username")).toHaveValue("demo_user");
    expect(screen.getByRole("button", { name: "Connect Instagram" })).toBeEnabled();
  });
  it("keeps typed input when a background account refresh arrives", () => {
    const { rerender } = render(<InstagramConnect hostedLoginEnabled
      accounts={[{ id, username: "demo_user", healthStatus: "paused" }]} onConnected={changed} />);
    fireEvent.change(screen.getByLabelText("Instagram username"), { target: { value: "my_other_account" } });
    rerender(<InstagramConnect hostedLoginEnabled
      accounts={[{ id, username: "demo_user", healthStatus: "active" }]} onConnected={changed} />);
    expect(screen.getByLabelText("Instagram username")).toHaveValue("my_other_account");
    expect(screen.queryByText("Instagram connected")).toBeNull();
  });
  it("keeps an active connection open when account props refresh", async () => {
    const { rerender } = render(<InstagramConnect hostedLoginEnabled accounts={[]} onConnected={changed} />);
    fireEvent.change(screen.getByLabelText("Instagram username"), { target: { value: "demo_user" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect Instagram" }));
    await screen.findByRole("link", { name: "Open Instagram connection tab" });
    rerender(<InstagramConnect hostedLoginEnabled
      accounts={[{ id, username: "demo_user", healthStatus: "active" }]} onConnected={changed} />);
    expect(screen.getByRole("link", { name: "Open Instagram connection tab" })).toBeInTheDocument();
    expect(screen.queryByText("Instagram connected")).toBeNull();
    expect(changed).not.toHaveBeenCalled();
  });
  it("shows a saved paused sign-in separately and lets the user reconnect again", async () => {
    fetchMock.mockResolvedValueOnce(Response.json(active())).mockResolvedValueOnce(Response.json({
      ...active(), state: "connected", account: { id, username: "demo_user", healthStatus: "paused" },
    }));
    setup();
    await screen.findByText("Instagram sign-in saved");
    expect(screen.queryByText("Instagram connected")).toBeNull();
    expect(screen.getByText(/Automation remains paused/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review account health" })).toHaveAttribute("href", "/account-health");
    fireEvent.click(screen.getByRole("button", { name: "Reconnect Instagram" }));
    expect(screen.getByRole("button", { name: "Connect Instagram" })).toBeEnabled();
  });
  it("pins a paused account reconnect to its owned account id", async () => {
    render(<InstagramConnect hostedLoginEnabled
      accounts={[{ id, username: "demo_user", healthStatus: "paused" }]} onConnected={changed} />);
    fireEvent.click(screen.getByRole("button", { name: "Connect Instagram" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).account_id).toBe(id);
  });
  it("identifies an unavailable service without blaming the user connection", async () => {
    fetchMock.mockResolvedValueOnce(Response.json(
      { code: "connection_unavailable" }, { status: 503 },
    ));
    setup();
    await screen.findByText("EngageMate’s Instagram sign-in service is unavailable. Please try again later.");
    expect(screen.queryByText(/check your connection/i)).toBeNull();
    expect(changed).not.toHaveBeenCalled();
  });
  it("explains a transient dedicated-proxy check without blaming Instagram sign-in", async () => {
    fetchMock.mockResolvedValueOnce(Response.json(
      { code: "proxy_unavailable" }, { status: 503 },
    ));
    setup();
    await screen.findByText(
      /couldn't verify the dedicated Instagram connection/i,
    );
    expect(screen.queryByText(/couldn't prepare a connection/i)).toBeNull();
    expect(changed).not.toHaveBeenCalled();
  });
  it("has no download in hosted mode and supports a blocked popup", async () => {
    setup();
    await screen.findByRole("link", { name: "Open Instagram connection tab" });
    expect(screen.queryByText(/download the engagemate extension/i)).toBeNull();
    expect(screen.queryByText(/continue on desktop/i)).toBeNull();
    const link = screen.getByRole("link", {
      name: "Open Instagram connection tab",
    });
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("href", `/api/instagram/connect/${id}/view`);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).username).toBe("demo_user");
    expect(changed).not.toHaveBeenCalled();
  });
  it("opens from the click, clears opener, and navigates only to owned same-origin viewer", async () => {
    const tab = {
      opener: window,
      document: { title: "" },
      location: { replace: vi.fn() },
      close: vi.fn(),
      closed: false,
    };
    openMock.mockReturnValue(tab);
    setup();
    expect(openMock).toHaveBeenCalledWith("about:blank", "_blank");
    expect(tab.opener).toBeNull();
    await waitFor(() =>
      expect(tab.location.replace).toHaveBeenCalledWith(
        `/api/instagram/connect/${id}/view`,
      ),
    );
  });
  it("confirms success only from owned server status and calls onConnected once", async () => {
    const account = { id, username: "demo_user", healthStatus: "active" };
    fetchMock
      .mockResolvedValueOnce(Response.json(active()))
      .mockResolvedValueOnce(
        Response.json({ ...active(), state: "connected", account }),
      );
    setup();
    await screen.findByText("Instagram connected");
    expect(changed).toHaveBeenCalledExactlyOnceWith(account);
    expect(
      screen.queryByRole("link", { name: "Open Instagram connection tab" }),
    ).toBeNull();
  });
  it("keeps existing healthy accounts usable without starting a session", () => {
    render(
      <InstagramConnect
        accounts={[{ id, username: "demo_user", healthStatus: "active" }]}
        onConnected={changed}
        hostedLoginEnabled
      />,
    );
    expect(screen.getByText("Instagram connected")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
  it("shows fixed safe failure copy, closes the empty tab and permits retry", async () => {
    const tab = { opener: null, document: {}, close: vi.fn() };
    openMock.mockReturnValue(tab);
    fetchMock.mockResolvedValueOnce(
      Response.json({ code: "upstream-password-secret" }, { status: 503 }),
    );
    setup();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "We couldn't complete that request",
    );
    expect(screen.queryByText(/upstream-password-secret/)).toBeNull();
    expect(tab.close).toHaveBeenCalledOnce();
    openMock.mockReturnValue(null);
    fireEvent.click(screen.getByRole("button", { name: "Connect Instagram" }));
    await screen.findByRole("link", { name: "Open Instagram connection tab" });
  });
  it("does not claim cancellation if its result is uncertain, and retries only on a click", async () => {
    setup();
    const cancel = await screen.findByRole("button", {
      name: "Cancel connection",
    });
    fetchMock.mockImplementation((_url, opts) =>
      opts?.method === "DELETE"
        ? Promise.reject(new Error("transport"))
        : Promise.resolve(Response.json(active())),
    );
    fireEvent.click(cancel);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "couldn't confirm cancellation",
    );
    expect(
      screen.getByRole("link", { name: "Open Instagram connection tab" }),
    ).toBeInTheDocument();
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel connection" }));
    await screen.findByRole("button", { name: "Connect Instagram" });
    expect(changed).not.toHaveBeenCalled();
  });
  it("expires and stops polling without reporting a connected account", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(
      Response.json({
        ...active(),
        expires_at: new Date(Date.now() + 1000).toISOString(),
      }),
    );
    setup();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2001);
    });
    expect(screen.getByRole("alert")).toHaveTextContent("expired");
    const calls = fetchMock.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(calls);
    expect(changed).not.toHaveBeenCalled();
  });
  it("bounds an unresponsive start and allows recovery", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(
      (_url, opts) =>
        new Promise((_resolve, reject) =>
          opts.signal.addEventListener("abort", () =>
            reject(new Error("timeout")),
          ),
        ),
    );
    setup();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100_001);
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "couldn't complete that request",
    );
    expect(
      screen.getByRole("button", { name: "Connect Instagram" }),
    ).toBeEnabled();
  });
});
