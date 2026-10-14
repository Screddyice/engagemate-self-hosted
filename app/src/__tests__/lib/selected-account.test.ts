import { beforeEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: () => ({ get: getMock }),
}));

vi.mock("@/lib/queries", () => ({
  getUserAccounts: vi.fn(),
}));

const accounts = [
  { id: "account-1", username: "first" },
  { id: "account-2", username: "second" },
];

describe("resolveSelectedAccount", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("falls back to the first owned account when the cookie is missing", async () => {
    const { getUserAccounts } = await import("@/lib/queries");
    vi.mocked(getUserAccounts).mockResolvedValue(accounts as never);
    getMock.mockReturnValue(undefined);

    const { resolveSelectedAccount } = await import("@/lib/selected-account");
    await expect(resolveSelectedAccount("user-1")).resolves.toEqual({
      accounts,
      selected: accounts[0],
    });
  });

  it("uses an owned cookie and rejects stale or cross-tenant values", async () => {
    const { getUserAccounts } = await import("@/lib/queries");
    vi.mocked(getUserAccounts).mockResolvedValue(accounts as never);
    const { resolveSelectedAccount } = await import("@/lib/selected-account");

    getMock.mockReturnValue({ value: "account-2" });
    await expect(resolveSelectedAccount("user-1")).resolves.toMatchObject({
      selected: accounts[1],
    });

    getMock.mockReturnValue({ value: "someone-elses-account" });
    await expect(resolveSelectedAccount("user-1")).resolves.toMatchObject({
      selected: accounts[0],
    });
  });

  it("returns a null selection when the manager has no accounts", async () => {
    const { getUserAccounts } = await import("@/lib/queries");
    vi.mocked(getUserAccounts).mockResolvedValue([]);
    getMock.mockReturnValue({ value: "deleted-account" });

    const { resolveSelectedAccount } = await import("@/lib/selected-account");
    await expect(resolveSelectedAccount("user-1")).resolves.toEqual({
      accounts: [],
      selected: null,
    });
  });
});
