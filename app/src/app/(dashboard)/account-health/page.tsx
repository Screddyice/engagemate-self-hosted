import { auth } from "@/lib/owner-auth";
import { redirect } from "next/navigation";
import Link from "next/link";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { HealthBadge } from "@/components/dashboard/health-badge";
import { AccountResumeButton } from "@/components/dashboard/account-resume-button";
import {
  getUserByOwnerKey,
  getHealthEvents,
} from "@/lib/queries";
import { resolveSelectedAccount } from "@/lib/selected-account";

const SEVERITY_STYLES: Record<string, string> = {
  info: "bg-muted text-muted-foreground",
  warning: "bg-amber-100 text-amber-800",
  critical: "bg-red-100 text-red-800",
};

const LOGIN_PAUSE_REASONS = new Set([
  "login_required",
  "session_expired",
  "authentication_required",
  "relationship_login_required",
]);

export default async function AccountHealthPage() {
  const { userId } = await auth();
  if (!userId) redirect("/setup");

  const user = await getUserByOwnerKey(userId);
  if (!user) redirect("/setup");

  const { selected: account } = await resolveSelectedAccount(user.id);

  if (!account) {
    return (
      <div>
        <h1 className="text-2xl font-bold">Account Health</h1>
        <Card className="mt-8 p-6 text-center">
          <p className="text-muted-foreground">
            Connect an Instagram account to see its health.
          </p>
          <Button asChild className="mt-4">
            <Link href="/accounts/connect">Connect an account</Link>
          </Button>
        </Card>
      </div>
    );
  }

  const events = await getHealthEvents(user.id, account.id, 20);
  const identityMismatch =
    account.pauseReason === "relationship_identity_mismatch";
  const needsReconnect =
    account.healthStatus === "challenged" ||
    (account.healthStatus === "paused" &&
      (identityMismatch || LOGIN_PAUSE_REASONS.has(account.pauseReason ?? "")));
  const proxyPaused =
    account.healthStatus === "paused" && account.pauseReason === "proxy_unhealthy";
  const canResumeAfterReconnect =
    account.healthStatus === "paused" &&
    (identityMismatch || LOGIN_PAUSE_REASONS.has(account.pauseReason ?? ""));

  return (
    <div>
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Account Health</h1>
          <p className="mt-1 text-muted-foreground">@{account.username}</p>
        </div>
        <HealthBadge status={account.healthStatus} />
      </div>

      <Card className="mt-6 p-6">
        <dl className="grid gap-4 sm:grid-cols-3">
          <div>
            <dt className="text-sm text-muted-foreground">Status</dt>
            <dd className="mt-1 font-medium capitalize">
              {account.healthStatus}
            </dd>
          </div>
          <div>
            <dt className="text-sm text-muted-foreground">Last healthy</dt>
            <dd className="mt-1 font-medium">
              {account.lastHealthyAt
                ? new Date(account.lastHealthyAt).toLocaleString()
                : "Not recorded"}
            </dd>
          </div>
          <div>
            <dt className="text-sm text-muted-foreground">Paused since</dt>
            <dd className="mt-1 font-medium">
              {account.pausedAt
                ? new Date(account.pausedAt).toLocaleString()
                : "Not paused"}
            </dd>
          </div>
        </dl>
      </Card>

      {account.healthStatus === "paused" && !needsReconnect && (
        <Card className="mt-6 border-amber-200 bg-amber-50 p-5">
          <p className="font-semibold text-amber-900">
            {proxyPaused ? "Proxy connection needs attention" : "Automation paused"}
          </p>
          <p className="mt-1 text-sm text-amber-900">
            {proxyPaused
              ? "EngageMate could not verify the proxy connection used for your account. Restore that connection before resuming automation."
              : "Review the recent health events and your account settings before resuming automation."}
          </p>
          <Button asChild className="mt-4">
            <Link href="/accounts">View accounts</Link>
          </Button>
        </Card>
      )}

      {needsReconnect && (
        <Card className="mt-6 border-red-200 bg-red-50 p-5">
          <p className="font-semibold text-red-900">Reconnect this account</p>
          <p className="mt-1 text-sm text-red-900">
            {identityMismatch
              ? `The saved Instagram session belongs to a different account than @${account.username}. Reconnect as @${account.username} before resuming automation.`
              : account.pauseReason
              ? `Reason: ${account.pauseReason.replace(/_/g, " ")}. `
                + "Instagram needs you to sign in again before any Story action can resume."
              : "Instagram needs you to sign in again before any Story action can resume."}
          </p>
          <Button asChild className="mt-4">
            <Link href="/accounts/connect">Reconnect account</Link>
          </Button>
          {canResumeAfterReconnect && (
            <>
              <p className="mt-3 text-sm text-red-900">
                After signing in again, choose Resume account automation below.
                Reconnecting keeps automation paused until you resume it. Your
                saved settings and limits stay the same.
              </p>
              {identityMismatch && (
                <p className="mt-2 text-sm text-red-900">
                  Your existing automatic unfollow setting will make overdue
                  unfollows eligible.
                </p>
              )}
              <AccountResumeButton igAccountId={account.id} />
            </>
          )}
        </Card>
      )}

      <section className="mt-10">
        <h2 className="text-lg font-semibold">Recent health events</h2>
        {events.length === 0 ? (
          <Card className="mt-4 p-6">
            <p className="text-sm text-muted-foreground">
              No health events recorded.
            </p>
          </Card>
        ) : (
          <ul className="mt-4 space-y-3">
            {events.map((event) => (
              <li
                key={event.id}
                className="flex items-center justify-between gap-4 rounded-lg border p-4"
              >
                <div>
                  <p className="text-sm font-medium">
                    {event.eventType.replace(/_/g, " ")}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {new Date(event.createdAt).toLocaleString()}
                  </p>
                </div>
                <span
                  className={`shrink-0 rounded-full px-2 py-1 text-xs capitalize ${
                    SEVERITY_STYLES[event.severity] ??
                    "bg-muted text-muted-foreground"
                  }`}
                >
                  {event.severity}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
