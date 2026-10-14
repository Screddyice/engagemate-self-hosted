import { provisionOwner } from "@/lib/users";
import { db } from "@/db";
import {
  users,
  igAccounts,
  agentConfigs,
  targets,
  storyEngagements,
  relationshipFollows,
  accountHealthEvents,
  runSummaries,
  engagementUsageEvents,
  accountGrowthSnapshots,
  weeklyReportPreferences,
  emailPreferences,
} from "@/db/schema";
import {
  eq,
  and,
  gte,
  desc,
  sql,
  inArray,
  isNotNull,
  lt,
  lte,
} from "drizzle-orm";
import {
  buildTrend,
  estimateTimeSavedSeconds,
  latestMetricValue,
  postgresCountToSafeInteger,
  type GrowthActivity,
  type GrowthDashboard,
  type GrowthMetricPoint,
} from "@/lib/growth";
import { isValidIanaTimezone } from "@/lib/timezone";

export type UserRow = typeof users.$inferSelect;
export type IgAccountRow = typeof igAccounts.$inferSelect;
export type AgentConfigRow = typeof agentConfigs.$inferSelect;
export interface WeeklyReportPreference {
  enabled: boolean;
  timezone: string;
  sendHour: number;
}

const weeklyReportPreferenceSelection = {
  enabled: weeklyReportPreferences.enabled,
  timezone: weeklyReportPreferences.timezone,
  sendHour: weeklyReportPreferences.sendHour,
};

// The `| null` is explicit because destructuring an empty result array yields
// `undefined` at runtime while TypeScript still types it as present.
export async function getUserByOwnerKey(
  ownerKey: string,
): Promise<UserRow | null> {
  const [user] = await db
    .select()
    .from(users)
    .where(eq(users.ownerKey, ownerKey))
    .limit(1);
  return user ?? provisionOwner(ownerKey);
}

export async function getUserAccounts(userId: string): Promise<IgAccountRow[]> {
  return db
    .select()
    .from(igAccounts)
    .where(eq(igAccounts.userId, userId))
    .orderBy(igAccounts.username);
}

export async function getOrCreateWeeklyReportPreference(
  userId: string,
): Promise<WeeklyReportPreference> {
  return db.transaction(async (tx) => {
    async function repairInvalidTimezone(
      preference: WeeklyReportPreference,
    ): Promise<WeeklyReportPreference> {
      if (isValidIanaTimezone(preference.timezone)) return preference;

      const [repaired] = await tx
        .update(weeklyReportPreferences)
        .set({ timezone: "UTC", updatedAt: new Date() })
        .where(eq(weeklyReportPreferences.userId, userId))
        .returning(weeklyReportPreferenceSelection);
      if (!repaired) {
        throw new Error("Weekly report preference was not repaired");
      }
      return repaired;
    }

    const [existing] = await tx
      .select(weeklyReportPreferenceSelection)
      .from(weeklyReportPreferences)
      .where(eq(weeklyReportPreferences.userId, userId))
      .limit(1)
      .for("update");
    if (existing) return repairInvalidTimezone(existing);

    const [firstOwnedAccount] = await tx
      .select({ timezone: agentConfigs.timezone })
      .from(igAccounts)
      .innerJoin(
        agentConfigs,
        and(
          eq(agentConfigs.igAccountId, igAccounts.id),
          eq(agentConfigs.userId, userId),
        ),
      )
      .where(eq(igAccounts.userId, userId))
      .orderBy(igAccounts.createdAt, igAccounts.id)
      .limit(1);
    const timezone = isValidIanaTimezone(firstOwnedAccount?.timezone)
      ? firstOwnedAccount.timezone
      : "UTC";

    const [created] = await tx
      .insert(weeklyReportPreferences)
      .values({ userId, timezone })
      .onConflictDoNothing({ target: weeklyReportPreferences.userId })
      .returning(weeklyReportPreferenceSelection);
    if (created) return created;

    const [concurrentPreference] = await tx
      .select(weeklyReportPreferenceSelection)
      .from(weeklyReportPreferences)
      .where(eq(weeklyReportPreferences.userId, userId))
      .limit(1)
      .for("update");
    if (!concurrentPreference) {
      throw new Error("Weekly report preference was not created");
    }
    return repairInvalidTimezone(concurrentPreference);
  });
}

export async function updateWeeklyReportPreference(
  userId: string,
  preference: WeeklyReportPreference,
): Promise<WeeklyReportPreference> {
  const [updated] = await db
    .update(weeklyReportPreferences)
    .set({ ...preference, updatedAt: new Date() })
    .where(eq(weeklyReportPreferences.userId, userId))
    .returning(weeklyReportPreferenceSelection);
  if (!updated) throw new Error("Weekly report preference was not found");
  return updated;
}

export type EmailPreferences = {
  activityEmails: boolean;
  onboardingEmails: boolean;
};

export async function getEmailPreferences(
  userId: string,
): Promise<EmailPreferences> {
  const [row] = await db
    .select({
      activityEmails: emailPreferences.activityEmails,
      onboardingEmails: emailPreferences.onboardingEmails,
    })
    .from(emailPreferences)
    .where(eq(emailPreferences.userId, userId))
    .limit(1);
  // No row means the user never turned anything off.
  return row ?? { activityEmails: true, onboardingEmails: true };
}

export async function updateEmailPreferences(
  userId: string,
  preferences: EmailPreferences,
): Promise<EmailPreferences> {
  const [saved] = await db
    .insert(emailPreferences)
    .values({ userId, ...preferences })
    .onConflictDoUpdate({
      target: emailPreferences.userId,
      set: { ...preferences, updatedAt: new Date() },
    })
    .returning({
      activityEmails: emailPreferences.activityEmails,
      onboardingEmails: emailPreferences.onboardingEmails,
    });
  if (!saved) throw new Error("Email preferences were not saved");
  return saved;
}

/** Turn off one category from a verified unsubscribe link. False: no such user. */
export async function unsubscribeUserFromCategory(
  userId: string,
  category: "activity" | "onboarding" | "weekly_report",
): Promise<boolean> {
  const [user] = await db
    .select({ id: users.id })
    .from(users)
    .where(eq(users.id, userId))
    .limit(1);
  if (!user) return false;
  if (category === "weekly_report") {
    await getOrCreateWeeklyReportPreference(userId);
    await db
      .update(weeklyReportPreferences)
      .set({ enabled: false, updatedAt: new Date() })
      .where(eq(weeklyReportPreferences.userId, userId));
    return true;
  }
  const column =
    category === "activity" ? "activityEmails" : "onboardingEmails";
  await db
    .insert(emailPreferences)
    .values({ userId, [column]: false })
    .onConflictDoUpdate({
      target: emailPreferences.userId,
      set: { [column]: false, updatedAt: new Date() },
    });
  return true;
}

export async function getAccountById(
  userId: string,
  igAccountId: string,
): Promise<IgAccountRow | null> {
  const [account] = await db
    .select()
    .from(igAccounts)
    .where(and(eq(igAccounts.userId, userId), eq(igAccounts.id, igAccountId)))
    .limit(1);
  return account ?? null;
}

export async function getAgentConfig(
  userId: string,
  igAccountId: string,
): Promise<AgentConfigRow | null> {
  const [config] = await db
    .select()
    .from(agentConfigs)
    .where(
      and(
        eq(agentConfigs.userId, userId),
        eq(agentConfigs.igAccountId, igAccountId),
      ),
    )
    .limit(1);
  return config ?? null;
}

export async function getTargets(userId: string, igAccountId: string) {
  return db
    .select()
    .from(targets)
    .where(
      and(eq(targets.userId, userId), eq(targets.igAccountId, igAccountId)),
    )
    .orderBy(targets.tier, targets.username);
}

export async function hasEnabledTarget(
  userId: string,
  igAccountId: string,
): Promise<boolean> {
  const [target] = await db
    .select({ id: targets.id })
    .from(targets)
    .where(
      and(
        eq(targets.userId, userId),
        eq(targets.igAccountId, igAccountId),
        eq(targets.enabled, true),
        isNotNull(targets.igUserId),
        sql`${targets.source} <> 'source-followers'`,
      ),
    )
    .limit(1);
  return target !== undefined;
}

export async function countTargets(
  userId: string,
  igAccountId: string,
): Promise<number> {
  const [row] = await db
    .select({ total: sql<number>`count(*)::int` })
    .from(targets)
    .where(
      and(eq(targets.userId, userId), eq(targets.igAccountId, igAccountId)),
    );
  return row?.total ?? 0;
}

/**
 * Today's sent actions, split by kind. The dashboard shows these against the
 * warm-up cap, so only `sent` rows count; gated and skipped attempts spend no
 * budget.
 */
export async function getActionCountsSince(
  userId: string,
  igAccountId: string,
  since: Date,
  until = new Date(),
  dailyTimezone?: string,
): Promise<{ like: number; reply: number; dm: number }> {
  const rows = await db
    .select({
      action: storyEngagements.action,
      total: sql<number>`count(*)::int`,
    })
    .from(storyEngagements)
    .where(
      and(
        eq(storyEngagements.userId, userId),
        eq(storyEngagements.igAccountId, igAccountId),
        eq(storyEngagements.status, "sent"),
        gte(storyEngagements.executedAt, dailyTimezone
          ? sql`date_trunc('day', ${until.toISOString()}::timestamptz AT TIME ZONE ${dailyTimezone}) AT TIME ZONE ${dailyTimezone}`
          : since),
        lte(storyEngagements.executedAt, until),
      ),
    )
    .groupBy(storyEngagements.action);

  const counts = { like: 0, reply: 0, dm: 0 };
  for (const row of rows) {
    if (row.action === "like" || row.action === "reply" || row.action === "dm") {
      counts[row.action] = row.total;
    }
  }
  return counts;
}

const RECORDED_STORY_ACTIONS = ["view", "like", "reply", "dm"] as const;

type RecordedStoryAction = (typeof RECORDED_STORY_ACTIONS)[number];

type RecordedStoryActivity = {
  action: RecordedStoryAction;
  total: number;
  today: number;
};

/** Recorded successful Story actions; calendar days follow the account timezone. */
export async function getRecordedStoryActivity(
  userId: string,
  igAccountId: string,
  now: Date,
  timezoneName = "America/Los_Angeles",
): Promise<RecordedStoryActivity[]> {
  const timezone = isValidIanaTimezone(timezoneName) ? timezoneName : "UTC";
  const cutoff = now.toISOString();
  const rows = await db.select({
    action: storyEngagements.action,
    total: sql<number>`count(*)::int`,
    today: sql<number>`count(*) filter (where ${storyEngagements.executedAt} >=
      (date_trunc('day', ${cutoff}::timestamptz at time zone ${timezone})
        at time zone ${timezone}))::int`,
  }).from(storyEngagements).where(and(
    eq(storyEngagements.userId, userId),
    eq(storyEngagements.igAccountId, igAccountId),
    eq(storyEngagements.status, "sent"),
    isNotNull(storyEngagements.executedAt),
    lte(storyEngagements.executedAt, now),
  )).groupBy(storyEngagements.action);
  return RECORDED_STORY_ACTIONS.map((action) => {
    const row = rows.find(row => row.action === action);
    return { action, total: Number(row?.total ?? 0), today: Number(row?.today ?? 0) };
  });
}

/** Relationship receipts are distinct from a profile already being unfollowed. */
export async function getRelationshipSummary(userId: string, igAccountId: string, now: Date) {
  const [row] = await db.select({
    follows: sql<number>`count(*) filter (where ${relationshipFollows.confirmedFollowAt} is not null and ${relationshipFollows.evidence} #>> '{follow,outcome}' = 'confirmed')::int`,
    unfollows: sql<number>`count(*) filter (where ${relationshipFollows.unfollowedAt} is not null and ${relationshipFollows.evidence} #>> '{unfollow,outcome}' = 'confirmed')::int`,
    alreadyAbsent: sql<number>`count(*) filter (where ${relationshipFollows.evidence} #>> '{unfollow,outcome}' = 'already_absent')::int`,
    pending: sql<number>`count(*) filter (where ${relationshipFollows.status} in ('following', 'unfollow_running'))::int`,
    overdue: sql<number>`count(*) filter (where ${relationshipFollows.status} in ('following', 'unfollow_running') and ${relationshipFollows.unfollowDueAt} <= ${now.toISOString()}::timestamptz)::int`,
    followBacks: sql<number>`count(distinct ${relationshipFollows.ownerId}) filter (where
      ${relationshipFollows.confirmedFollowAt} is not null
      and ${relationshipFollows.evidence} #>> '{follow,outcome}' = 'confirmed'
      and ${relationshipFollows.evidence} #>> '{follow,followed_by}' = 'false'
      and ((${relationshipFollows.evidence} #>> '{unfollow,identity_matches}' = 'true'
        and ${relationshipFollows.evidence} #>> '{unfollow,followed_by}' = 'true')
        or (${relationshipFollows.evidence} #>> '{first_follow_back,identity_matches}' = 'true'
          and ${relationshipFollows.evidence} #>> '{first_follow_back,owner_id}' = ${relationshipFollows.ownerId}
          and ${relationshipFollows.evidence} #>> '{first_follow_back,followed_by}' = 'true')))::int`,
    checked: sql<number>`count(*) filter (where
      ${relationshipFollows.confirmedFollowAt} is not null
      and ${relationshipFollows.evidence} #>> '{follow,outcome}' = 'confirmed'
      and ${relationshipFollows.evidence} #>> '{follow,followed_by}' = 'false'
      and ${relationshipFollows.evidence} #>> '{reconciliation,identity_matches}' = 'true'
      and ${relationshipFollows.evidence} #>> '{reconciliation,owner_id}' = ${relationshipFollows.ownerId}
      and ${relationshipFollows.evidence} #>> '{reconciliation,followed_by}' in ('true', 'false')
      and (${relationshipFollows.evidence} #>> '{reconciliation,observed_at}')::timestamptz between ${now.toISOString()}::timestamptz - interval '24 hours' and ${now.toISOString()}::timestamptz)::int`,
  }).from(relationshipFollows).where(and(
    eq(relationshipFollows.userId, userId), eq(relationshipFollows.igAccountId, igAccountId),
  ));
  return row ?? { follows: 0, unfollows: 0, alreadyAbsent: 0, pending: 0, overdue: 0, followBacks: 0, checked: 0 };
}

export async function getEngagementFeed(
  userId: string,
  igAccountId: string,
  limit = 20,
) {
  return db
    .select({
      id: storyEngagements.id,
      action: storyEngagements.action,
      status: storyEngagements.status,
      generatedText: storyEngagements.generatedText,
      failureCode: storyEngagements.failureCode,
      createdAt: storyEngagements.createdAt,
      executedAt: storyEngagements.executedAt,
      targetUsername: targets.username,
      targetTier: targets.tier,
    })
    .from(storyEngagements)
    .innerJoin(targets, eq(storyEngagements.targetId, targets.id))
    .where(
      and(
        eq(storyEngagements.userId, userId),
        eq(storyEngagements.igAccountId, igAccountId),
      ),
    )
    .orderBy(desc(storyEngagements.createdAt))
    .limit(limit);
}

export async function getHealthEvents(
  userId: string,
  igAccountId: string,
  limit = 20,
) {
  return db
    .select()
    .from(accountHealthEvents)
    .where(
      and(
        eq(accountHealthEvents.userId, userId),
        eq(accountHealthEvents.igAccountId, igAccountId),
      ),
    )
    .orderBy(desc(accountHealthEvents.createdAt))
    .limit(limit);
}

// Match the worker: only a reconnect after 30 or more days paused resets warm-up.
export async function getLatestRestrictionAt(
  userId: string,
  igAccountId: string,
): Promise<Date | null> {
  const [event] = await db
    .select({ createdAt: accountHealthEvents.createdAt })
    .from(accountHealthEvents)
    .where(and(
      eq(accountHealthEvents.userId, userId),
      eq(accountHealthEvents.igAccountId, igAccountId),
      eq(accountHealthEvents.eventType, "warmup_restart"),
    ))
    .orderBy(desc(accountHealthEvents.createdAt))
    .limit(1);
  return event?.createdAt ?? null;
}

const DAY_IN_MS = 24 * 60 * 60 * 1000;

function localDate(now: Date, timezone: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function shiftDate(date: string, days: number): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + days))
    .toISOString()
    .slice(0, 10);
}

type SnapshotMetricColumn =
  | typeof accountGrowthSnapshots.followerCount
  | typeof accountGrowthSnapshots.engagementRateBps;

function latestMetricDateSql(column: SnapshotMetricColumn, boundary: string) {
  return sql<string | null>`(
    array_agg(
      ${accountGrowthSnapshots.snapshotDate}
      order by ${accountGrowthSnapshots.snapshotDate} desc,
        ${accountGrowthSnapshots.capturedAt} desc,
        ${accountGrowthSnapshots.id} desc
    ) filter (
      where ${accountGrowthSnapshots.snapshotDate} <= ${boundary}
        and ${column} is not null
    )
  )[1]`;
}

function latestMetricValueSql(column: SnapshotMetricColumn, boundary: string) {
  return sql<number | null>`(
    array_agg(
      ${column}
      order by ${accountGrowthSnapshots.snapshotDate} desc,
        ${accountGrowthSnapshots.capturedAt} desc,
        ${accountGrowthSnapshots.id} desc
    ) filter (
      where ${accountGrowthSnapshots.snapshotDate} <= ${boundary}
        and ${column} is not null
    )
  )[1]`;
}

function metricPoint(
  date: string | null | undefined,
  value: number | null | undefined,
): GrowthMetricPoint | null {
  return date !== null &&
    date !== undefined &&
    value !== null &&
    value !== undefined
    ? { date, value }
    : null;
}

function trendFromAnchors(
  start: GrowthMetricPoint | null,
  end: GrowthMetricPoint | null,
  boundary: string,
) {
  return buildTrend(
    [start, end].filter((point): point is GrowthMetricPoint => point !== null),
    boundary,
  );
}

export async function getGrowthDashboard(
  userId: string,
  igAccountId: string,
  now: Date,
): Promise<GrowthDashboard> {
  if (!(now instanceof Date) || !Number.isFinite(now.getTime())) {
    throw new TypeError("now must be a valid Date");
  }

  const activityStart = new Date(now.getTime() - 7 * DAY_IN_MS);
  const [[config], [runActivity], sentRows] = await Promise.all([
    db
      .select({
        growthGoal: agentConfigs.growthGoal,
        timezone: agentConfigs.timezone,
      })
      .from(agentConfigs)
      .where(
        and(
          eq(agentConfigs.userId, userId),
          eq(agentConfigs.igAccountId, igAccountId),
        ),
      )
      .limit(1),
    db
      .select({
        reviewed: sql<string | number | bigint>`coalesce(sum(${runSummaries.opportunitiesFound}), 0)::bigint`,
        skipped: sql<string | number | bigint>`coalesce(sum(${runSummaries.skipped}), 0)::bigint`,
        gated: sql<string | number | bigint>`coalesce(sum(${runSummaries.gated}), 0)::bigint`,
        failed: sql<string | number | bigint>`coalesce(sum(${runSummaries.failed}), 0)::bigint`,
      })
      .from(runSummaries)
      .where(
        and(
          eq(runSummaries.userId, userId),
          eq(runSummaries.igAccountId, igAccountId),
          isNotNull(runSummaries.completedAt),
          gte(runSummaries.completedAt, activityStart),
          lt(runSummaries.completedAt, now),
        ),
      ),
    db
      .select({
        action: storyEngagements.action,
        total: sql<string | number | bigint>`count(*)::bigint`,
      })
      .from(storyEngagements)
      .where(
        and(
          eq(storyEngagements.userId, userId),
          eq(storyEngagements.igAccountId, igAccountId),
          eq(storyEngagements.status, "sent"),
          isNotNull(storyEngagements.executedAt),
          gte(storyEngagements.executedAt, activityStart),
          lt(storyEngagements.executedAt, now),
        ),
      )
      .groupBy(storyEngagements.action),
  ]);

  const today = localDate(now, config?.timezone ?? "UTC");
  const boundary7d = shiftDate(today, -7);
  const boundary30d = shiftDate(today, -30);
  const [[anchors], chartSnapshots] = await Promise.all([
    db
      .select({
        followerCurrentDate: latestMetricDateSql(
          accountGrowthSnapshots.followerCount,
          today,
        ),
        followerCurrent: latestMetricValueSql(
          accountGrowthSnapshots.followerCount,
          today,
        ),
        follower7dDate: latestMetricDateSql(
          accountGrowthSnapshots.followerCount,
          boundary7d,
        ),
        follower7d: latestMetricValueSql(
          accountGrowthSnapshots.followerCount,
          boundary7d,
        ),
        follower30dDate: latestMetricDateSql(
          accountGrowthSnapshots.followerCount,
          boundary30d,
        ),
        follower30d: latestMetricValueSql(
          accountGrowthSnapshots.followerCount,
          boundary30d,
        ),
        engagementCurrentDate: latestMetricDateSql(
          accountGrowthSnapshots.engagementRateBps,
          today,
        ),
        engagementCurrent: latestMetricValueSql(
          accountGrowthSnapshots.engagementRateBps,
          today,
        ),
        engagement7dDate: latestMetricDateSql(
          accountGrowthSnapshots.engagementRateBps,
          boundary7d,
        ),
        engagement7d: latestMetricValueSql(
          accountGrowthSnapshots.engagementRateBps,
          boundary7d,
        ),
        engagement30dDate: latestMetricDateSql(
          accountGrowthSnapshots.engagementRateBps,
          boundary30d,
        ),
        engagement30d: latestMetricValueSql(
          accountGrowthSnapshots.engagementRateBps,
          boundary30d,
        ),
      })
      .from(accountGrowthSnapshots)
      .where(
        and(
          eq(accountGrowthSnapshots.userId, userId),
          eq(accountGrowthSnapshots.igAccountId, igAccountId),
          inArray(accountGrowthSnapshots.status, ["complete", "partial"]),
          lte(accountGrowthSnapshots.snapshotDate, today),
        ),
      ),
    db
      .select({
        date: accountGrowthSnapshots.snapshotDate,
        followerCount: accountGrowthSnapshots.followerCount,
      })
      .from(accountGrowthSnapshots)
      .where(
        and(
          eq(accountGrowthSnapshots.userId, userId),
          eq(accountGrowthSnapshots.igAccountId, igAccountId),
          inArray(accountGrowthSnapshots.status, ["complete", "partial"]),
          gte(accountGrowthSnapshots.snapshotDate, boundary30d),
          lte(accountGrowthSnapshots.snapshotDate, today),
        ),
      )
      .orderBy(
        accountGrowthSnapshots.snapshotDate,
        accountGrowthSnapshots.capturedAt,
        accountGrowthSnapshots.id,
      ),
  ]);

  const followerCurrent = metricPoint(
    anchors?.followerCurrentDate,
    anchors?.followerCurrent,
  );
  const follower7d = metricPoint(anchors?.follower7dDate, anchors?.follower7d);
  const follower30d = metricPoint(
    anchors?.follower30dDate,
    anchors?.follower30d,
  );
  const engagementCurrent = metricPoint(
    anchors?.engagementCurrentDate,
    anchors?.engagementCurrent,
  );
  const engagement7d = metricPoint(
    anchors?.engagement7dDate,
    anchors?.engagement7d,
  );
  const engagement30d = metricPoint(
    anchors?.engagement30dDate,
    anchors?.engagement30d,
  );
  const activity: GrowthActivity = {
    reviewed: postgresCountToSafeInteger(runActivity?.reviewed ?? 0, "reviewed"),
    likes: 0,
    replies: 0,
    dms: 0,
    views: 0,
    skipped: postgresCountToSafeInteger(runActivity?.skipped ?? 0, "skipped"),
    gated: postgresCountToSafeInteger(runActivity?.gated ?? 0, "gated"),
    failed: postgresCountToSafeInteger(runActivity?.failed ?? 0, "failed"),
  };

  for (const row of sentRows) {
    if (
      row.action === "like" ||
      row.action === "reply" ||
      row.action === "dm" ||
      row.action === "view"
    ) {
      const key: "likes" | "replies" | "dms" | "views" =
        row.action === "like"
          ? "likes"
          : row.action === "reply"
            ? "replies"
            : row.action === "dm"
              ? "dms"
              : "views";
      activity[key] = postgresCountToSafeInteger(row.total, `${key} sent`);
    }
  }

  return {
    points: chartSnapshots.map((snapshot) => ({
      date: snapshot.date,
      value: postgresCountToSafeInteger(
        snapshot.followerCount,
        "snapshot follower count",
      ),
    })),
    currentFollowers: followerCurrent
      ? latestMetricValue([followerCurrent])
      : null,
    currentEngagementRateBps: engagementCurrent
      ? latestMetricValue([engagementCurrent])
      : null,
    followerTrend7d: trendFromAnchors(
      follower7d,
      followerCurrent,
      boundary7d,
    ),
    followerTrend30d: trendFromAnchors(
      follower30d,
      followerCurrent,
      boundary30d,
    ),
    engagementTrend7d: trendFromAnchors(
      engagement7d,
      engagementCurrent,
      boundary7d,
    ),
    engagementTrend30d: trendFromAnchors(
      engagement30d,
      engagementCurrent,
      boundary30d,
    ),
    growthGoal: config?.growthGoal ?? null,
    activity7d: activity,
    estimatedTimeSavedSeconds: estimateTimeSavedSeconds(activity),
  };
}

export async function getEnabledAccounts(userId: string) {
  return db
    .select({ account: igAccounts })
    .from(igAccounts)
    .innerJoin(agentConfigs, eq(agentConfigs.igAccountId, igAccounts.id))
    .where(
      and(
        eq(igAccounts.userId, userId),
        eq(igAccounts.healthStatus, "active"),
        eq(agentConfigs.enabled, true),
      ),
    )
    .then((rows) => rows.map((row) => row.account));
}

export async function getUsageByAccount(
  userId: string,
  periodStart: Date,
  periodEnd: Date,
) {
  const periodStartIso = periodStart.toISOString();
  const periodEndIso = periodEnd.toISOString();
  return db
    .select({
      igAccountId: engagementUsageEvents.igAccountId,
      billable: sql<number>`coalesce(sum(case
        when ${engagementUsageEvents.status} = 'billable'
         and ${engagementUsageEvents.occurredAt} >= ${periodStartIso}::timestamptz
         and ${engagementUsageEvents.occurredAt} < ${periodEndIso}::timestamptz
        then ${engagementUsageEvents.quantity} else 0 end), 0)::int`,
      reserved: sql<number>`coalesce(sum(case
        when ${engagementUsageEvents.status} = 'reserved'
         and ${engagementUsageEvents.reservedAt} >= ${periodStartIso}::timestamptz
         and ${engagementUsageEvents.reservedAt} < ${periodEndIso}::timestamptz
        then ${engagementUsageEvents.quantity} else 0 end), 0)::int`,
    })
    .from(engagementUsageEvents)
    .where(eq(engagementUsageEvents.userId, userId))
    .groupBy(engagementUsageEvents.igAccountId);
}
