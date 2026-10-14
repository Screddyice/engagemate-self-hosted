import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { refresh, push, fetchMock } = vi.hoisted(() => ({
  refresh: vi.fn(), push: vi.fn(), fetchMock: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, push }),
}));

import { AccountResumeButton } from "@/components/dashboard/account-resume-button";

describe("AccountResumeButton", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("fetch", fetchMock);
  });

  it("waits for an explicit click, then resumes the selected account and opens the dashboard", async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ health: "active" }) });
    render(<AccountResumeButton igAccountId="account-1" />);
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Resume account automation" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard"));
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      "/api/accounts/account-1/resume", { method: "POST" },
    );
    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Resuming account…" })).toBeDisabled();
  });

  it("keeps the pause screen when the server requires a fresh login", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      json: async () => ({ error: "Reconnect the matching Instagram account before resuming" }),
    });
    render(<AccountResumeButton igAccountId="account-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Resume account automation" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Reconnect the matching Instagram account before resuming",
    );
    expect(push).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Resume account automation" })).toBeEnabled();
  });
});
