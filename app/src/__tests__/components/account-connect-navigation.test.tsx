import React from "react";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/owner-auth", () => ({ auth: vi.fn() }));
vi.mock("@/lib/queries", () => ({ getUserByOwnerKey: vi.fn() }));
vi.mock("@/lib/selected-account", () => ({ resolveSelectedAccount: vi.fn() }));
vi.mock("@/lib/hosted-login-config", () => ({ hostedLoginEnabled: () => false }));
vi.mock("@/components/platform-login/instagram-connect-wizard", () => ({
  InstagramConnectWizard: ({ selectedAccountId }: { selectedAccountId: string | null }) =>
    <div data-testid="selected-account">{selectedAccountId ?? "none"}</div>,
}));

vi.mock("next/link", () => ({
  default: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) =>
    React.createElement("a", props, props.children),
}));

describe("account connection navigation", () => {
  beforeEach(() => vi.clearAllMocks());
  it("exposes Instagram as a keyboard-accessible link", async () => {
    const { default: ConnectAccountPage } = await import(
      "@/app/(dashboard)/accounts/connect/page"
    );
    render(<ConnectAccountPage />);

    expect(
      screen.getByRole("link", { name: /instagram/i }),
    ).toHaveAttribute("href", "/accounts/connect/instagram");
  });

  it.each(["paused-account", null])("passes the owned selection %s to the connection form", async (id) => {
    const { auth } = await import("@/lib/owner-auth");
    const { getUserByOwnerKey } = await import("@/lib/queries");
    const { resolveSelectedAccount } = await import("@/lib/selected-account");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk-owner" } as never);
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "app-owner" } as never);
    vi.mocked(resolveSelectedAccount).mockResolvedValue({
      accounts: [], selected: id ? { id, healthStatus: "paused" } : null,
    } as never);
    const { default: ConnectInstagramPage } = await import(
      "@/app/(dashboard)/accounts/connect/instagram/page"
    );
    render(await ConnectInstagramPage());
    expect(getUserByOwnerKey).toHaveBeenCalledWith("clerk-owner");
    expect(resolveSelectedAccount).toHaveBeenCalledWith("app-owner");
    expect(screen.getByTestId("selected-account")).toHaveTextContent(id ?? "none");
  });
});
