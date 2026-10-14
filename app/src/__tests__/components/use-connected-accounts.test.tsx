import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useConnectedAccounts } from "@/components/platform-login/use-connected-accounts";

const active = [{ id: "owned", username: "demo_user", healthStatus: "active" }];
const paused = [{ ...active[0], healthStatus: "paused" }];
const fetchMock = vi.fn();
beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset().mockResolvedValue(Response.json(paused));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

it("reconciles new server props", () => {
  const { result, rerender } = renderHook(({ rows }) => useConnectedAccounts(rows), { initialProps: { rows: active } });
  rerender({ rows: paused });
  expect(result.current.accounts).toEqual(paused);
  expect(fetchMock).not.toHaveBeenCalled();
});

it("refreshes on focus, polls visible accounts every 30 seconds, and stops on unmount", async () => {
  const { result, unmount } = renderHook(() => useConnectedAccounts(active));
  await act(async () => { await vi.advanceTimersByTimeAsync(29_999); });
  expect(fetchMock).not.toHaveBeenCalled();
  await act(async () => { window.dispatchEvent(new Event("focus")); });
  expect(result.current.accounts).toEqual(paused);
  expect(fetchMock).toHaveBeenCalledWith("/api/accounts", expect.objectContaining({ cache: "no-store" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(fetchMock).toHaveBeenCalledTimes(2);
  unmount();
  await act(async () => { window.dispatchEvent(new Event("focus")); await vi.advanceTimersByTimeAsync(60_000); });
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it("does not poll hidden tabs or empty account lists", async () => {
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  const { unmount } = renderHook(() => useConnectedAccounts(active));
  await act(async () => { window.dispatchEvent(new Event("focus")); await vi.advanceTimersByTimeAsync(60_000); });
  expect(fetchMock).not.toHaveBeenCalled();
  unmount();
  vi.restoreAllMocks();
  const empty: typeof active = [];
  renderHook(() => useConnectedAccounts(empty));
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(fetchMock).not.toHaveBeenCalled();
});

it("deduplicates concurrent refreshes and aborts a hung read after ten seconds", async () => {
  fetchMock.mockImplementation((_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new Error("private")), { once: true });
  }));
  const { result } = renderHook(() => useConnectedAccounts(null));
  await act(async () => { window.dispatchEvent(new Event("focus")); document.dispatchEvent(new Event("visibilitychange")); });
  expect(fetchMock).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
  expect(result.current.accounts).toBeNull();
  expect(result.current.refreshFailed).toBe(true);
});

it("does not replace a completed connection with an older in-flight response", async () => {
  let resolve!: (response: Response) => void;
  fetchMock.mockImplementation(() => new Promise<Response>((done) => { resolve = done; }));
  const { result } = renderHook(() => useConnectedAccounts(paused));
  await act(async () => { window.dispatchEvent(new Event("focus")); });
  act(() => result.current.setAccounts(active));
  await act(async () => { resolve(Response.json(paused)); });
  expect(result.current.accounts).toEqual(active);
});

it.each([Response.json({}, { status: 503 }), Response.json([{ id: "owned", username: "demo_user" }])])(
  "keeps the last snapshot and exposes refresh failure", async (response) => {
    fetchMock.mockResolvedValue(response);
    const { result } = renderHook(() => useConnectedAccounts(paused));
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    expect(result.current.accounts).toEqual(paused);
    expect(result.current.refreshFailed).toBe(true);
  },
);
