import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(), getHealthEvents: vi.fn(),
}));
vi.mock("@/lib/selected-account", () => ({ resolveSelectedAccount: vi.fn() }));
vi.mock("@/components/dashboard/account-resume-button", () => ({
  AccountResumeButton: ({ igAccountId }: { igAccountId: string }) => (
    <button data-account={igAccountId}>Resume account automation</button>
  ),
}));

import AccountHealthPage from "@/app/(dashboard)/account-health/page";
import { getHealthEvents, getUserByOwnerKey } from "@/lib/queries";
import { resolveSelectedAccount } from "@/lib/selected-account";

function selectAccount(pauseReason: string | null, healthStatus = "paused") {
  const selected = {
    id: "selected-account", username: "creator", healthStatus, pauseReason,
    pausedAt: new Date("2026-10-01T00:00:00Z"), lastHealthyAt: null,
  };
  vi.mocked(resolveSelectedAccount).mockResolvedValue({
    selected, accounts: [selected],
  } as never);
}

describe("AccountHealthPage reconnect recovery", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
    vi.mocked(getHealthEvents).mockResolvedValue([]);
  });

  it.each([
    "login_required", "session_expired", "authentication_required",
    "relationship_login_required", "relationship_identity_mismatch",
  ])("offers an explicit guarded resume after %s", async (reason) => {
    selectAccount(reason);

    render(await AccountHealthPage());

    expect(screen.getByRole("button", { name: "Resume account automation" }))
      .toHaveAttribute("data-account", "selected-account");
    expect(screen.getByText(/Reconnecting keeps automation paused until you resume it/))
      .toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Reconnect account" }))
      .toHaveAttribute("href", "/accounts/connect");
    expect(getHealthEvents).toHaveBeenCalledWith("user-1", "selected-account", 20);
  });

  it("retains the matching-account explanation for an identity mismatch", async () => {
    selectAccount("relationship_identity_mismatch");

    render(await AccountHealthPage());

    expect(screen.getByText(/Reconnect as @creator before resuming automation/))
      .toBeInTheDocument();
    expect(screen.getByText(/automatic unfollow setting will make overdue/))
      .toBeInTheDocument();
  });

  it.each([
    ["manual_pause", "paused"], ["rate_limited", "paused"],
    ["login_required", "challenged"], [null, "active"],
  ])("does not expose login recovery for %s / %s", async (reason, status) => {
    selectAccount(reason, status as string);

    render(await AccountHealthPage());

    expect(screen.queryByRole("button", { name: "Resume account automation" }))
      .not.toBeInTheDocument();
  });

  it("explains a proxy pause without requesting another Instagram login", async () => {
    selectAccount("proxy_unhealthy");

    render(await AccountHealthPage());

    expect(screen.getByText("Proxy connection needs attention")).toBeInTheDocument();
    expect(screen.getByText(/Restore that connection before resuming automation/))
      .toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View accounts" })).toHaveAttribute("href", "/accounts");
    expect(screen.queryByRole("link", { name: "Reconnect account" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume account automation" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Instagram needs you to sign in again/)).not.toBeInTheDocument();
    expect(getHealthEvents).toHaveBeenCalledWith("user-1", "selected-account", 20);
  });

  it.each(["manual_pause", "rate_limited", "unexpected_reason", null])(
    "does not claim authentication failed for a %s pause", async (reason) => {
      selectAccount(reason);

      render(await AccountHealthPage());

      expect(screen.getByText("Automation paused")).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: "Reconnect account" })).not.toBeInTheDocument();
      expect(screen.queryByText(/Instagram needs you to sign in again/)).not.toBeInTheDocument();
    },
  );

  it("keeps the reconnect link for an Instagram challenge", async () => {
    selectAccount(null, "challenged");

    render(await AccountHealthPage());

    expect(screen.getByRole("link", { name: "Reconnect account" })).toHaveAttribute("href", "/accounts/connect");
    expect(screen.queryByRole("link", { name: "View accounts" })).not.toBeInTheDocument();
  });

  it("ignores a stale proxy pause reason after recovery", async () => {
    selectAccount("proxy_unhealthy", "active");

    render(await AccountHealthPage());

    expect(screen.queryByText("Proxy connection needs attention")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Reconnect account" })).not.toBeInTheDocument();
  });
});
