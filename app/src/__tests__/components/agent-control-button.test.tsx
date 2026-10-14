import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockRefresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: mockRefresh }),
}));

import { AgentControlButton } from "@/components/dashboard/agent-control-button";

const mockFetch = vi.fn();
global.fetch = mockFetch;

describe("AgentControlButton", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("resumes the selected account and refreshes its status", async () => {
    mockFetch
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({ ok: true });
    render(<AgentControlButton igAccountId="account-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Resume agent" }));

    await waitFor(() =>
      expect(mockFetch).toHaveBeenCalledWith(
        "/api/onboarding/launch",
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"ig_account_id":"account-1"'),
        }),
      ),
    );
    expect(mockRefresh).toHaveBeenCalledOnce();
  });

  it("uses a 44px touch target and stays pending after refresh starts", async () => {
    render(<AgentControlButton igAccountId="account-1" />);

    const resume = screen.getByRole("button", { name: "Resume agent" });
    expect(resume).toHaveClass("min-h-11");
    let resolveResume!: (response: { ok: boolean }) => void;
    mockFetch
      .mockResolvedValueOnce({ ok: true })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveResume = resolve;
        }),
      );
    fireEvent.click(resume);
    expect(screen.getByRole("button", { name: "Resuming…" })).toBeDisabled();

    resolveResume({ ok: true });

    await waitFor(() => expect(mockRefresh).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: "Resuming…" })).toBeDisabled();
  });

  it("shows the server error and leaves the dashboard state unchanged", async () => {
    mockFetch
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({
        ok: false,
        json: async () => ({ error: "Account needs attention" }),
      });
    render(<AgentControlButton igAccountId="account-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Resume agent" }));

    expect(await screen.findByText("Account needs attention")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Account needs attention");
    expect(mockRefresh).not.toHaveBeenCalled();
  });
});
