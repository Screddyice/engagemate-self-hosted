import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen } from "@testing-library/react";
import { Sidebar } from "@/components/dashboard/sidebar";

vi.mock("next/link", () => ({
  default: (props: { children: React.ReactNode; href: string }) =>
    React.createElement("a", { href: props.href }, props.children),
}));

vi.mock("next/navigation", () => ({
  usePathname: vi.fn(() => "/dashboard"),
  useRouter: vi.fn(() => ({ refresh: vi.fn() })),
}));

vi.mock("@clerk/nextjs", () => ({
  UserButton: () => React.createElement("div", { "data-testid": "user-button" }),
}));

describe("Sidebar Component", () => {
  // Test 94
  it("should render EngageMate brand name", () => {
    render(React.createElement(Sidebar));
    expect(screen.getByText("EngageMate")).toBeInTheDocument();
  });

  // Test 95
  it("should render Overview nav item", () => {
    render(React.createElement(Sidebar));
    expect(screen.getByText("Overview")).toBeInTheDocument();
  });

  // Test 96
  it("should render Accounts nav item", () => {
    render(React.createElement(Sidebar));
    expect(screen.getByText("Accounts")).toBeInTheDocument();
  });

  // Test 97
  it("should omit retired content and analytics nav items", () => {
    render(React.createElement(Sidebar));
    expect(screen.queryByText("Content")).not.toBeInTheDocument();
    expect(screen.queryByText("Analytics")).not.toBeInTheDocument();
  });

  // Test 98
  it("should render Settings nav item", () => {
    render(React.createElement(Sidebar));
    expect(screen.getByText("Settings")).toBeInTheDocument();
  });

  // Test 99
  it("should omit removed voice and content-pillar sub-nav items", () => {
    render(React.createElement(Sidebar));
    expect(screen.queryByText("Voice & Tone")).not.toBeInTheDocument();
    expect(screen.queryByText("Content Pillars")).not.toBeInTheDocument();
  });

  // Test 100
  it("should omit the retired notifications sub-nav", () => {
    render(React.createElement(Sidebar));
    expect(screen.queryByText("Notifications")).not.toBeInTheDocument();
  });

  it("shows an account switcher when the manager owns several accounts", () => {
    render(
      React.createElement(Sidebar, {
        accounts: [
          { id: "account-1", username: "first", healthStatus: "active" },
          { id: "account-2", username: "second", healthStatus: "paused" },
        ] as never,
        selectedAccountId: "account-2",
      }),
    );

    expect(screen.getByLabelText("Working account")).toHaveValue("account-2");
    expect(screen.getByText("@second · paused")).toBeInTheDocument();
  });

  it("uses the phone width for navigation before the desktop breakpoint", () => {
    render(React.createElement(Sidebar));

    expect(screen.getByRole("complementary")).toHaveClass(
      "h-auto",
      "w-full",
      "md:h-screen",
      "md:w-64",
    );
    expect(screen.getByRole("navigation")).toHaveClass(
      "flex",
      "overflow-x-auto",
      "md:block",
    );
  });
});
