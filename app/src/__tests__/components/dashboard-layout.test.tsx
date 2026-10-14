import { describe, expect, it, vi } from "vitest";
import React from "react";
import { render, screen } from "@testing-library/react";
import DashboardLayout from "@/app/(dashboard)/layout";

vi.mock("@/lib/owner-auth", () => ({
  auth: vi.fn(async () => ({ userId: null })),
}));

vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(),
}));

vi.mock("@/lib/selected-account", () => ({
  resolveSelectedAccount: vi.fn(),
}));

vi.mock("@/components/dashboard/sidebar", () => ({
  Sidebar: () => React.createElement("aside", { "aria-label": "Primary" }),
}));

describe("Dashboard layout", () => {
  it("stacks navigation above a full-width main region until desktop widths", async () => {
    render(
      await DashboardLayout({
        children: React.createElement("div", null, "Dashboard content"),
      }),
    );

    const main = screen.getByRole("main");
    expect(main.parentElement).toHaveClass("flex-col", "md:flex-row");
    expect(main).toHaveClass(
      "min-w-0",
      "p-4",
      "sm:p-6",
      "md:p-8",
    );
  });
});
