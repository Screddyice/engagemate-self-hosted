import { eq, sql } from "drizzle-orm";
import { db } from "@/db";
import { engagementUsageEvents } from "@/db/schema";
import type { UserRow } from "@/lib/queries";
import { planLimits } from "@/lib/plans";

export type UsageSummary = {
  included: number;
  billable: number;
  reserved: number;
  remaining: number;
  overage: number;
  projectedOverageCents: number;
};

export type UsageSummaryWithPeriod = UsageSummary & {
  periodStart: Date;
  periodEnd: Date;
};

export function projectedOverageCents(used: number, included: number) {
  void used; void included; return 0;
}

export function summarizeUsage(input: {
  included: number;
  billable: number;
  reserved: number;
}): UsageSummary {
  const billable = Math.max(0, input.billable);
  const reserved = Math.max(0, input.reserved);
  return {
    included: input.included,
    billable,
    reserved,
    remaining: Math.max(0, input.included - billable - reserved),
    overage: Math.max(0, billable - input.included),
    projectedOverageCents: projectedOverageCents(
      billable + reserved,
      input.included,
    ),
  };
}

export function usagePeriod(
  _user: UserRow,
  now: Date,
) {
  return {
    start: new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)),
    end: new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)),
  };
}

export async function getUsageSummary(
  user: UserRow,
  now = new Date(),
): Promise<UsageSummaryWithPeriod> {
  const period = usagePeriod(user, now);
  // Bound as ISO strings with an explicit cast. Inside a raw `sql` fragment the
  // column's type mapper does not apply to the value side, so a JS Date reaches
  // postgres.js unconverted and throws in Buffer.byteLength. Typed operators
  // like gte() are unaffected, which is why only this query failed.
  const periodStart = period.start.toISOString();
  const periodEnd = period.end.toISOString();
  const [row] = await db
    .select({
      billable: sql<number>`coalesce(sum(case
        when ${engagementUsageEvents.status} = 'billable'
         and ${engagementUsageEvents.occurredAt} >= ${periodStart}::timestamptz
         and ${engagementUsageEvents.occurredAt} < ${periodEnd}::timestamptz
        then ${engagementUsageEvents.quantity} else 0 end), 0)::int`,
      reserved: sql<number>`coalesce(sum(case
        when ${engagementUsageEvents.status} = 'reserved'
         and ${engagementUsageEvents.reservedAt} >= ${periodStart}::timestamptz
         and ${engagementUsageEvents.reservedAt} < ${periodEnd}::timestamptz
        then ${engagementUsageEvents.quantity} else 0 end), 0)::int`,
    })
    .from(engagementUsageEvents)
    .where(eq(engagementUsageEvents.userId, user.id));
  const limits = planLimits(user.plan);
  return {
    ...summarizeUsage({
      included: limits.monthlyEngagements,
      billable: Number(row?.billable ?? 0),
      reserved: Number(row?.reserved ?? 0),
    }),
    periodStart: period.start,
    periodEnd: period.end,
  };
}

export function eligibleAccountIds<T extends { id: string; createdAt: Date }>(
  accounts: readonly T[],
  planValue: string,
) {
  void planValue;
  const plan = "self_hosted";
  const ordered = [...accounts].sort(
    (left, right) =>
      left.createdAt.getTime() - right.createdAt.getTime() ||
      left.id.localeCompare(right.id),
  );
  return new Set(
    ordered.slice(0, planLimits(plan).igAccounts).map((account) => account.id),
  );
}
