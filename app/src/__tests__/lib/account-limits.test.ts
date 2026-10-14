import { beforeEach, describe, expect, it, vi } from "vitest";

const selectMock = vi.fn();
const insertMock = vi.fn();
const updateMock = vi.fn();
const executeMock = vi.fn();
const transactionMock = vi.fn(async (callback: (tx: unknown) => unknown) =>
  callback({
    select: selectMock,
    insert: insertMock,
    update: updateMock,
    execute: executeMock,
  }),
);

vi.mock("@/db", () => ({
  db: { transaction: transactionMock },
}));

function limited(rows: unknown[]) {
  return {
    from: () => ({
      where: () => ({ limit: () => Promise.resolve(rows) }),
    }),
  };
}

function counted(count: number) {
  return {
    from: () => ({
      where: () => Promise.resolve([{ count }]),
    }),
  };
}

describe("connectAccount", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("keeps existing usernames idempotent at the plan cap", async () => {
    const existing = { id: "account-1", userId: "user-1", username: "owned" };
    selectMock
      .mockReturnValueOnce(limited([{ plan: "pro" }]))
      .mockReturnValueOnce(limited([existing]));

    const { connectAccount } = await import("@/lib/account-limits");
    await expect(
      connectAccount({ userId: "user-1", username: "owned" }),
    ).resolves.toEqual({ account: existing, created: false });

    expect(insertMock).not.toHaveBeenCalled();
    expect(executeMock).toHaveBeenCalledOnce();
  });

  it("rejects a new account when the manager reached the plan cap", async () => {
    selectMock
      .mockReturnValueOnce(limited([{ plan: "pro" }]))
      .mockReturnValueOnce(limited([]))
      .mockReturnValueOnce(counted(100));

    const { AccountLimitReached, connectAccount } = await import(
      "@/lib/account-limits"
    );
    await expect(
      connectAccount({ userId: "user-1", username: "new.account" }),
    ).rejects.toMatchObject({
      name: AccountLimitReached.name,
      code: "account_limit_reached",
      limit: 100,
    });
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("creates the account and its default config in one transaction", async () => {
    const account = {
      id: "account-2",
      userId: "user-1",
      username: "new.account",
      igUserId: "123",
    };
    selectMock
      .mockReturnValueOnce(limited([{ plan: "agency" }]))
      .mockReturnValueOnce(limited([]))
      .mockReturnValueOnce(counted(9));
    insertMock
      .mockReturnValueOnce({
        values: () => ({ returning: () => Promise.resolve([account]) }),
      })
      .mockReturnValueOnce({ values: () => Promise.resolve() });

    const { connectAccount } = await import("@/lib/account-limits");
    await expect(
      connectAccount({
        userId: "user-1",
        username: "new.account",
        igUserId: "123",
      }),
    ).resolves.toEqual({ account, created: true });
    expect(insertMock).toHaveBeenCalledTimes(2);
  });

  it.each([["self_hosted", 20], ["pro", 20]])("caps initial likes at %s plan limits", async (plan, likes) => {
    const configValues = vi.fn(() => Promise.resolve());
    selectMock.mockReturnValueOnce(limited([{ plan }]))
      .mockReturnValueOnce(limited([])).mockReturnValueOnce(counted(0));
    insertMock.mockReturnValueOnce({ values: () => ({ returning: () => Promise.resolve([{ id: "new" }]) }) })
      .mockReturnValueOnce({ values: configValues });
    const { connectAccount } = await import("@/lib/account-limits");
    await connectAccount({ userId: "user-1", username: "new.account" });
    expect(configValues).toHaveBeenCalledWith({ userId: "user-1", igAccountId: "new", dailyLikeLimit: likes });
  });

  it("does not read an unscoped legacy profile when connecting the first account", async () => {
    const account = {
      id: "account-2",
      userId: "user-1",
      username: "new.account",
      igUserId: "123",
    };
    const configValues = vi.fn(() => Promise.resolve());
    selectMock
      .mockReturnValueOnce(limited([{ plan: "pro" }]))
      .mockReturnValueOnce(limited([]))
      .mockReturnValueOnce(counted(0));
    insertMock
      .mockReturnValueOnce({
        values: () => ({ returning: () => Promise.resolve([account]) }),
      })
      .mockReturnValueOnce({ values: configValues });

    const { connectAccount } = await import("@/lib/account-limits");
    await connectAccount({
      userId: "user-1",
      username: "new.account",
      igUserId: "123",
    });

    expect(configValues).toHaveBeenCalledWith({
      userId: "user-1",
      igAccountId: "account-2",
      dailyLikeLimit: 20,
    });
    expect(selectMock).toHaveBeenCalledTimes(3);
  });
});
