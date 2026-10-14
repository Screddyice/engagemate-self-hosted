import { and, eq, sql } from "drizzle-orm";
import { db } from "@/db";
import { agentConfigs, agentConfigVersions, igAccounts, mcpAuditEvents } from "@/db/schema";

type ConfigPatch = Partial<Pick<typeof agentConfigs.$inferInsert,
  "enabled" | "timezone" | "scheduleHour" | "dailyLikeLimit" | "dailyReplyLimit" |
  "dailyDmLimit" | "dailyReelMessageLimit" | "dailyViewLimit" | "growthGoal" | "persona" | "bannedPhrases" |
  "expectedLanguage" | "followAfterEngagement" | "dailyFollowLimit" | "autoUnfollowEnabled" | "followReelMessageEnabled" | "followReelMessageTemplate">>;
const snapshot = (value: unknown) => JSON.parse(JSON.stringify(value));

export async function saveAccountConfig(userId: string, accountId: string, actorId: string, patch: ConfigPatch) {
  return db.transaction(async (tx) => {
    // Serialize first creation as well as concurrent enable/disable transitions.
    const [account] = await tx.select({ id: igAccounts.id }).from(igAccounts)
      .where(and(eq(igAccounts.id, accountId), eq(igAccounts.userId, userId))).for("update");
    if (!account) throw new Error("Account is unavailable");
    const [before] = await tx.select().from(agentConfigs)
      .where(and(eq(agentConfigs.igAccountId, accountId), eq(agentConfigs.userId, userId))).for("update");
    const followEnabledAt = patch.followAfterEngagement === false ? null
      : patch.followAfterEngagement === true && !before?.followAfterEngagement ? sql`clock_timestamp()`
      : before?.followEnabledAt ?? null;
    const followReelMessageEnabledAt = patch.followReelMessageEnabled === false ? null
      : patch.followReelMessageEnabled === true && !before?.followReelMessageEnabled ? sql`clock_timestamp()`
      : before?.followReelMessageEnabledAt ?? null;
    const values = { ...patch, followReelMessageEnabledAt, followEnabledAt, version: (before?.version ?? 0) + 1, updatedAt: sql`now()` };
    const [after] = before
      ? await tx.update(agentConfigs).set(values).where(and(eq(agentConfigs.id, before.id), eq(agentConfigs.userId, userId))).returning()
      : await tx.insert(agentConfigs).values({ ...values, userId, igAccountId: accountId }).returning();
    if (!after) throw new Error("Configuration update failed");
    if (patch.followReelMessageEnabled === false) {
      await tx.execute(sql`UPDATE follow_reel_messages SET status = 'cancelled', updated_at = now()
        WHERE user_id = ${userId} AND ig_account_id = ${accountId}
          AND status = 'pending' AND attempted_at IS NULL`);
    }
    await tx.insert(agentConfigVersions).values({ userId, igAccountId: accountId, version: after.version,
      snapshot: snapshot(after), actorType: "web", actorId });
    await tx.insert(mcpAuditEvents).values({ userId, igAccountId: accountId, toolName: "web.config.update",
      outcome: "updated", beforeState: before ? snapshot(before) : null, afterState: snapshot(after),
      safeMetadata: { actorType: "web", actorId } });
    return after;
  });
}
