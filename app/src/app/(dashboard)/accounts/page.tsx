import { auth } from "@/lib/owner-auth";
import { redirect } from "next/navigation";
import Link from "next/link";
import {
  getUserByOwnerKey,
  getUserAccounts,
  getUsageByAccount,
} from "@/lib/queries";
import { Button } from "@/components/ui/button";
import { eligibleAccountIds, getUsageSummary } from "@/lib/usage";

const HEALTH_STYLES: Record<string, string> = {
  active: "bg-green-100 text-green-800",
  degraded: "bg-amber-100 text-amber-800",
  paused: "bg-amber-100 text-amber-800",
  challenged: "bg-red-100 text-red-800",
  banned: "bg-red-100 text-red-800",
};

export default async function AccountsPage() {
  const { userId } = await auth();
  if (!userId) redirect("/setup");

  const user = await getUserByOwnerKey(userId);
  if (!user) redirect("/setup");

  const accounts = await getUserAccounts(user.id);
  const usage = await getUsageSummary(user);
  const accountUsage = await getUsageByAccount(
    user.id,
    usage.periodStart,
    usage.periodEnd,
  );
  const usageById = new Map(
    accountUsage.map((row) => [row.igAccountId, row]),
  );
  const eligibleIds = eligibleAccountIds(accounts, user.plan);

  return (
    <div>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Connected Accounts</h1>
        <Button asChild>
          <Link href="/accounts/connect">Connect Account</Link>
        </Button>
      </div>

      <div className="mt-6 flex flex-col gap-3 rounded-lg border p-5 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            Shared monthly usage
          </p>
          <p className="mt-2 font-mono text-3xl font-bold tabular-nums">
            {`${usage.billable.toLocaleString("en-US")} recorded`}
          </p>
        </div>
        <p className="font-mono text-sm tabular-nums text-muted-foreground">
          {`${usage.reserved.toLocaleString("en-US")} reserved`}
        </p>
      </div>

      {accounts.length === 0 ? (
        <div className="mt-12 text-center">
          <p className="text-muted-foreground">No accounts connected yet.</p>
          <Button asChild className="mt-4">
            <Link href="/accounts/connect">Connect your first account</Link>
          </Button>
        </div>
      ) : (
        <div className="mt-8 space-y-4">
          {accounts.map((account) => {
            const row = usageById.get(account.id);
            const eligible = eligibleIds.has(account.id);
            return (
              <div
                key={account.id}
                className="flex flex-col gap-4 rounded-lg border p-4 sm:flex-row sm:items-center sm:justify-between"
              >
                <div>
                  <p className="font-medium">@{account.username}</p>
                  <p className="mt-1 font-mono text-xs tabular-nums text-muted-foreground">
                    {`${Number(row?.billable ?? 0).toLocaleString("en-US")} recorded · ${Number(row?.reserved ?? 0).toLocaleString("en-US")} reserved`}
                  </p>
                  {!eligible && (
                    <p className="mt-1 max-w-xl text-xs text-amber-700">
                      This account remains connected but is held by the current
                      workspace limit.
                    </p>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className={`rounded-full px-2 py-1 text-xs ${
                      eligible
                        ? "bg-blue-100 text-blue-800"
                        : "bg-amber-100 text-amber-800"
                    }`}
                  >
                    {eligible ? "Ready" : "Workspace limit"}
                  </span>
                  <span
                    className={`rounded-full px-2 py-1 text-xs capitalize ${
                      HEALTH_STYLES[account.healthStatus] ??
                      "bg-muted text-muted-foreground"
                    }`}
                  >
                    {account.healthStatus}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
