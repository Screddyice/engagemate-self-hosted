import React from "react";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/link", () => ({
  default: (props: { children: React.ReactNode; href: string; className?: string }) =>
    React.createElement("a", props, props.children),
}));

vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(),
  getUserAccounts: vi.fn(),
  getUsageByAccount: vi.fn(),
}));

vi.mock("@/lib/usage", () => ({
  getUsageSummary: vi.fn(),
  eligibleAccountIds: vi.fn(() => new Set(["account-1"])),
}));

describe("agency accounts overview", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const queries = await import("@/lib/queries");
    vi.mocked(queries.getUserByOwnerKey).mockResolvedValue({
      id: "user-1",
      plan: "starter",
    } as never);
    vi.mocked(queries.getUserAccounts).mockResolvedValue([
      {
        id: "account-1",
        username: "eligible",
        healthStatus: "active",
        pauseReason: null,
        createdAt: new Date("2026-08-01T00:00:00Z"),
      },
      {
        id: "account-2",
        username: "held",
        healthStatus: "active",
        pauseReason: null,
        createdAt: new Date("2026-08-02T00:00:00Z"),
      },
    ] as never);
    vi.mocked(queries.getUsageByAccount).mockResolvedValue([
      { igAccountId: "account-1", billable: 120, reserved: 2 },
      { igAccountId: "account-2", billable: 80, reserved: 0 },
    ]);
    const usage = await import("@/lib/usage");
    vi.mocked(usage.getUsageSummary).mockResolvedValue({
      included: 300,
      billable: 200,
      reserved: 2,
      remaining: 98,
      overage: 0,
      projectedOverageCents: 0,
      periodStart: new Date("2026-08-01T00:00:00Z"),
      periodEnd: new Date("2026-09-01T00:00:00Z"),
    });
  });

  it("shows shared usage and preserves over-limit accounts on hold", async () => {
    const { default: AccountsPage } = await import(
      "@/app/(dashboard)/accounts/page"
    );
    render(await AccountsPage());

    expect(screen.getByText("200 recorded")).toBeInTheDocument();
    expect(screen.getByText("@eligible")).toBeInTheDocument();
    expect(screen.getByText("120 recorded · 2 reserved")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Workspace limit")).toBeInTheDocument();
    expect(screen.getByText(/remains connected/i)).toBeInTheDocument();
  });

  it("does not send account cards to an unimplemented detail route", async () => {
    const { default: AccountsPage } = await import(
      "@/app/(dashboard)/accounts/page"
    );
    render(await AccountsPage());

    expect(
      screen.queryByRole("link", { name: /@eligible/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /connect account/i }),
    ).toHaveAttribute("href", "/accounts/connect");
  });
});
