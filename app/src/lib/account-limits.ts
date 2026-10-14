import { and, count, eq, sql } from "drizzle-orm";
import { db } from "@/db";
import { agentConfigs, igAccounts, users } from "@/db/schema";
import { planLimits } from "@/lib/plans";

export type IgAccountRow = typeof igAccounts.$inferSelect;

export class AccountLimitReached extends Error {
  readonly code = "account_limit_reached" as const;

  constructor(readonly limit: number) {
    super("Instagram account limit reached");
    this.name = "AccountLimitReached";
  }
}

export async function connectAccount(input: {
  userId: string;
  username: string;
  igUserId?: string | null;
}): Promise<{ account: IgAccountRow; created: boolean }> {
  const username = input.username.trim().toLowerCase();

  return db.transaction(async (tx) => {
    await tx.execute(
      sql`select pg_advisory_xact_lock(hashtextextended(${input.userId}::text, 0))`,
    );

    const [owner] = await tx
      .select({ plan: users.plan })
      .from(users)
      .where(eq(users.id, input.userId))
      .limit(1);
    if (!owner) {
      throw new Error("Account owner not found");
    }

    const [existing] = await tx
      .select()
      .from(igAccounts)
      .where(
        and(
          eq(igAccounts.userId, input.userId),
          eq(igAccounts.username, username),
        ),
      )
      .limit(1);

    if (existing) {
      if (input.igUserId && input.igUserId !== existing.igUserId) {
        const [updated] = await tx
          .update(igAccounts)
          .set({ igUserId: input.igUserId, updatedAt: new Date() })
          .where(
            and(
              eq(igAccounts.id, existing.id),
              eq(igAccounts.userId, input.userId),
            ),
          )
          .returning();
        return { account: updated ?? existing, created: false };
      }
      return { account: existing, created: false };
    }

    const limit = planLimits(owner.plan).igAccounts;
    const [owned] = await tx
      .select({ count: count(igAccounts.id) })
      .from(igAccounts)
      .where(eq(igAccounts.userId, input.userId));
    const ownedCount = Number(owned?.count ?? 0);
    if (ownedCount >= limit) {
      throw new AccountLimitReached(limit);
    }

    const [account] = await tx
      .insert(igAccounts)
      .values({
        userId: input.userId,
        username,
        igUserId: input.igUserId ?? null,
      })
      .returning();
    if (!account) {
      throw new Error("Instagram account was not created");
    }

    await tx.insert(agentConfigs).values({
      userId: input.userId,
      igAccountId: account.id,
      dailyLikeLimit: Math.min(20, planLimits(owner.plan).dailyActions),
    });

    return { account, created: true };
  });
}
