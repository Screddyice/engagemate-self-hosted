import { auth } from "@/lib/owner-auth";
import { redirect } from "next/navigation";
import Link from "next/link";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { getUserByOwnerKey, getTargets } from "@/lib/queries";
import { planLimits } from "@/lib/plans";
import { resolveSelectedAccount } from "@/lib/selected-account";

const TIER_STYLES: Record<string, string> = {
  vip: "bg-violet-100 text-violet-800",
  broad: "bg-blue-100 text-blue-800",
  discovery: "bg-muted text-muted-foreground",
};

export default async function TargetsPage() {
  const { userId } = await auth();
  if (!userId) redirect("/setup");

  const user = await getUserByOwnerKey(userId);
  if (!user) redirect("/setup");

  const { selected: account } = await resolveSelectedAccount(user.id);

  if (!account) {
    return (
      <div>
        <h1 className="text-2xl font-bold">Targets</h1>
        <Card className="mt-8 p-6 text-center">
          <p className="text-muted-foreground">
            Connect an Instagram account before choosing targets.
          </p>
          <Button asChild className="mt-4">
            <Link href="/accounts/connect">Connect an account</Link>
          </Button>
        </Card>
      </div>
    );
  }

  const rows = await getTargets(user.id, account.id);
  const limits = planLimits(user.plan);

  return (
    <div>
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Targets</h1>
          <p className="mt-1 text-muted-foreground">@{account.username}</p>
        </div>
        <span className="text-sm text-muted-foreground">
          {`${rows.length} of ${limits.targets} targets used`}
        </span>
      </div>

      {rows.length === 0 ? (
        <Card className="mt-8 p-6 text-center">
          <p className="text-muted-foreground">
            No targets yet. EngageMate only acts on an active Story from a
            target you have chosen.
          </p>
        </Card>
      ) : (
        <div className="mt-8 space-y-3">
          {rows.map((target) => (
            <div
              key={target.id}
              className="flex items-center justify-between rounded-lg border p-4"
            >
              <div>
                <p className="font-medium">@{target.username}</p>
                <p className="text-sm text-muted-foreground capitalize">
                  {`Added from ${target.source.replace(/_/g, " ")}`}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <span
                  className={`rounded-full px-2 py-1 text-xs uppercase ${
                    TIER_STYLES[target.tier] ?? "bg-muted text-muted-foreground"
                  }`}
                >
                  {target.tier}
                </span>
                <span
                  className={`rounded-full px-2 py-1 text-xs ${
                    target.enabled
                      ? "bg-green-100 text-green-800"
                      : "bg-muted text-muted-foreground"
                  }`}
                >
                  {target.enabled ? "Enabled" : "Disabled"}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
