import { ensureUser } from "@/lib/ensure-user";
import { redirect } from "next/navigation";
import Link from "next/link";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { HealthBadge } from "@/components/dashboard/health-badge";
import { GrowthOverview } from "@/components/dashboard/growth-overview";
import { AgentControlButton } from "@/components/dashboard/agent-control-button";
import { FollowStatus } from "@/components/dashboard/follow-status";
import { FollowSettings } from "@/components/dashboard/follow-settings";
import { WarmupOptIn } from "@/components/dashboard/warmup-opt-in";
import {
  getAgentConfig,
  getActionCountsSince,
  getRecordedStoryActivity,
  getRelationshipSummary,
  getLatestRestrictionAt,
  getEngagementFeed,
  getGrowthDashboard,
} from "@/lib/queries";
import { resolveSelectedAccount } from "@/lib/selected-account";
import { isPlan, planLimits } from "@/lib/plans";
import { describeCaps, effectiveCaps, warmupStage } from "@/lib/warmup";
import { getUsageSummary } from "@/lib/usage";
import { isValidIanaTimezone } from "@/lib/timezone";

const ACTION_LABELS: Record<string, string> = {
  view: "Viewed a Story",
  like: "Liked a Story",
  reply: "Replied to a Story",
  dm: "DMed from a Story",
};

const STATUS_STYLES: Record<string, string> = {
  sent: "bg-green-100 text-green-800",
  gated: "bg-amber-100 text-amber-800",
  skipped: "bg-muted text-muted-foreground",
  failed: "bg-red-100 text-red-800",
};

function startOfUtcDay(now: Date) {
  return new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()),
  );
}

function formatCount(value: number) {
  return value.toLocaleString("en-US");
}

function formatTimezoneLabel(timezone: string) {
  return timezone === "America/Los_Angeles" ? "Pacific" : timezone;
}

function ActionTile({
  label,
  sent,
  cap,
  timezoneLabel,
}: {
  label: string;
  sent: number;
  cap: number;
  timezoneLabel: string;
}) {
  return (
    <Card className="p-5">
      <p className="text-sm font-medium text-muted-foreground">{label}</p>
      <p className="mt-2 text-3xl font-bold">{`${sent} / ${cap}`}</p>
      <p className="mt-1 text-xs text-muted-foreground">
        {`recorded this ${timezoneLabel} day / daily limit`}
      </p>
    </Card>
  );
}

export default async function DashboardPage() {
  const user = await ensureUser();
  if (!user) redirect("/setup");

  const { selected: account } = await resolveSelectedAccount(user.id);

  if (!account) {
    return (
      <div>
        <h1 className="text-2xl font-bold">Home</h1>
        <p className="mt-1 text-muted-foreground">
          EngageMate engages from your targets&apos; active Instagram Stories.
        </p>
        <Card className="mt-8 p-6 text-center">
          <p className="text-muted-foreground">
            No Instagram account connected yet.
          </p>
          <Button asChild className="mt-4">
            <Link href="/accounts/connect">Connect your first account</Link>
          </Button>
        </Card>
      </div>
    );
  }

  const now = new Date();
  const config = await getAgentConfig(user.id, account.id);
  const accountTimezone = isValidIanaTimezone(config?.timezone)
    ? config.timezone
    : "UTC";
  const timezoneLabel = formatTimezoneLabel(accountTimezone);
  const [counts, feed, growth, usage, activity, relationships, lastRestrictedAt] = await Promise.all([
    getActionCountsSince(user.id, account.id, startOfUtcDay(now), now, accountTimezone),
    getEngagementFeed(user.id, account.id, 10),
    getGrowthDashboard(user.id, account.id, now),
    getUsageSummary(user, now),
    getRecordedStoryActivity(user.id, account.id, now, accountTimezone),
    getRelationshipSummary(user.id, account.id, now),
    getLatestRestrictionAt(user.id, account.id),
  ]);

  const plan = isPlan(user.plan) ? user.plan : "self_hosted";
  const limits = planLimits(plan);
  const warmupStart = account.warmupStartedAt && lastRestrictedAt
    && lastRestrictedAt > account.warmupStartedAt
    ? lastRestrictedAt : account.warmupStartedAt;
  const stage = warmupStage(warmupStart, now, config?.warmupExempt ?? false);
  const caps = effectiveCaps(
    stage,
    {
      like: config?.dailyLikeLimit ?? 0,
      reply: config?.dailyReplyLimit ?? 0,
      dm: config?.dailyDmLimit ?? 0,
    },
    limits.dailyActions,
  );
  const reelTarget = config?.followAfterEngagement && config?.followReelMessageEnabled
    ? config.dailyReelMessageLimit : null;
  const reelCap = reelTarget == null ? null
    : Math.min(reelTarget, Math.max((stage?.caps.reply ?? 0) - caps.reply, 0),
               Math.max(limits.dailyActions - caps.reply, 0));
  if (reelCap !== null) caps.like = Math.min(caps.like, Math.max(limits.dailyActions - caps.reply - reelCap, 0));

  const needsReconnect =
    account.healthStatus === "paused" ||
    account.healthStatus === "challenged" ||
    account.healthStatus === "banned";
  const identityMismatch =
    account.pauseReason === "relationship_identity_mismatch";
  const isDegraded = account.healthStatus === "degraded";
  const agentOff = config ? !config.enabled : true;

  return (
    <div>
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Home</h1>
          <p className="mt-1 text-muted-foreground">@{account.username}</p>
        </div>
        <HealthBadge status={account.healthStatus} />
      </div>

      <Card className="mt-6 overflow-hidden p-0">
        <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Story engagements this month
            </p>
            <p className="mt-2 font-mono text-3xl font-bold tabular-nums">
              {`${formatCount(usage.billable)} recorded`}
            </p>
            <p className="mt-1 text-sm text-muted-foreground">
              Shared across every connected account. Counts successful likes, replies, and DMs; excludes views, follows, and unfollows.
            </p>
          </div>
          <div className="text-left sm:text-right">
            <p className="font-mono text-sm font-semibold tabular-nums">
              {`${formatCount(usage.reserved)} reserved`}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {"Your daily limits and warm-up apply"}
            </p>
          </div>
        </div>
        <div className="h-1.5 bg-muted">
          <div
            className="h-full bg-violet-500"
            style={{
              width: `${Math.min(100, (usage.billable / usage.included) * 100)}%`,
            }}
          />
        </div>
      </Card>

      {needsReconnect && (
        <Card className="mt-6 border-red-200 bg-red-50 p-5">
          <p className="font-semibold text-red-900">
            Instagram stopped this account
          </p>
          <p className="mt-1 text-sm text-red-900">
            {identityMismatch
              ? `The saved Instagram session belongs to a different account than @${account.username}. Reconnect it from Account Health before resuming.`
              : account.pauseReason
              ? `Reason: ${account.pauseReason.replace(/_/g, " ")}. `
                + "No Story actions run until the session is reconnected."
              : "No Story actions run until the session is reconnected."}
          </p>
          <Button asChild variant="outline" className="mt-4">
            <Link href="/account-health">Review account health</Link>
          </Button>
        </Card>
      )}

      {isDegraded && (
        <Card className="mt-6 border-amber-200 bg-amber-50 p-5">
          <p className="font-semibold text-amber-900">
            Running degraded
          </p>
          <p className="mt-1 text-sm text-amber-900">
            Recent runs hit resistance, so the worker is holding back. Check the
            health events before raising any limit.
          </p>
          <Button asChild variant="outline" className="mt-4">
            <Link href="/account-health">Review account health</Link>
          </Button>
        </Card>
      )}

      {!needsReconnect && !isDegraded && agentOff && (
        <Card className="mt-6 border-amber-200 bg-amber-50 p-5">
          <p className="font-semibold text-amber-900">Agent is paused</p>
          <p className="mt-1 text-sm text-amber-900">
            Nothing is scheduled until you enable this account.
          </p>
          <AgentControlButton igAccountId={account.id} />
        </Card>
      )}

      <section className="mt-10">
        <h2 className="text-lg font-semibold">Recorded Story activity</h2>
        <p className="mt-1 text-sm text-muted-foreground">{`For @${account.username}. Only actions recorded as successful with an execution time count. Today uses ${timezoneLabel} time.`}</p>
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead><tr className="border-b"><th className="py-3">Action</th><th>{`Today (${timezoneLabel})`}</th><th>All time</th></tr></thead>
            <tbody>{activity.map(row => <tr key={row.action} className="border-b">
              <th className="py-3 font-medium">{{ view: "Story views", like: "Story likes", reply: "Story replies", dm: "Story DMs" }[row.action]}</th>
              <td>{formatCount(row.today)}</td><td>{formatCount(row.total)}</td>
            </tr>)}</tbody>
          </table>
        </div>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold">Warm-up limits</h2>
        {stage && lastRestrictedAt && warmupStart === lastRestrictedAt && caps.reply === 0
          && (config?.dailyReplyLimit ?? 0) > 0 && (
          <Card className="mt-4 border-amber-200 bg-amber-50 p-5">
            <p className="font-semibold text-amber-900">Story replies are waiting for warm-up</p>
            <p className="mt-1 text-sm text-amber-900">
              This account reconnected after at least 30 days paused, so warm-up restarted. EngageMate can view and like Stories,
              but it will hold Story replies {config?.followReelMessageEnabled ? "and post-follow reel messages " : ""}until the message
              allowance returns. The next message stage begins {new Date(lastRestrictedAt.getTime()
                + 3 * 24 * 60 * 60 * 1000).toLocaleString("en-US", {
                  timeZone: accountTimezone, timeZoneName: "short", year: "numeric",
                  month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
                })}. Pending follows with reel messages become eligible then.
            </p>
          </Card>
        )}
        {stage ? (
          <Card className="mt-4 p-6">
            <p className="text-sm text-muted-foreground">
              {`${stage.label} (day ${stage.day})`}
            </p>
            <p className="mt-2 text-xl font-semibold">{describeCaps(caps)}</p>
            {reelCap !== null && <p className="mt-2 text-sm">Up to {reelCap} reel DMs separately per day. Pending and uncertain sends reserve capacity.</p>}
            <p className="mt-2 text-sm text-muted-foreground">
              {`Today's cap is the lower of the warm-up stage, your workspace ceiling of ${limits.dailyActions} actions a day, and your configured limits.`}
            </p>
          </Card>
        ) : (
          <Card className="mt-4 p-6">
            <p className="text-sm text-muted-foreground">
              Warm-up has not started. It begins when you complete Launch.
            </p>
          </Card>
        )}
        {config && (
          <WarmupOptIn key={account.id} accountId={account.id}
            planDailyActions={limits.dailyActions}
            current={{ like: config.dailyLikeLimit, reply: config.dailyReplyLimit, dm: config.dailyDmLimit }} />
        )}
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold">{`Daily quota (${timezoneLabel})`}</h2>
        <p className="mt-1 text-sm text-muted-foreground">{`Quota resets at midnight in ${timezoneLabel}. These limits and activity totals use the same account timezone.`}</p>
        <div className="mt-4 grid gap-4 md:grid-cols-3">
          <ActionTile label="Likes" sent={counts.like} cap={caps.like} timezoneLabel={timezoneLabel} />
          <ActionTile label="Replies" sent={counts.reply} cap={caps.reply} timezoneLabel={timezoneLabel} />
        </div>
      </section>

      {config && <section className="mt-10"><h2 className="text-lg font-semibold">Follow settings</h2>
        <FollowSettings key={account.id} accountId={account.id} enabled={config.followAfterEngagement} followReelMessageEnabled={config.followReelMessageEnabled} followReelMessageTemplate={config.followReelMessageTemplate} autoUnfollowEnabled={config.autoUnfollowEnabled} dailyLimit={config.dailyFollowLimit} />
        <FollowStatus key={`status-${account.id}`} accountId={account.id} autoUnfollowEnabled={config.autoUnfollowEnabled} timezone={accountTimezone} />
      </section>}
      <section className="mt-10">
        <h2 className="text-lg font-semibold">Follow activity and outcomes</h2>
        <p className="mt-1 text-sm text-muted-foreground">All time for @{account.username}, based on saved Instagram observations.</p>
        <dl className="mt-4 grid gap-4 sm:grid-cols-2">
          <div><dt>Follows completed</dt><dd className="text-2xl font-semibold">{relationships.follows}</dd></div>
          <div><dt>Unfollows completed by EngageMate</dt><dd className="text-2xl font-semibold">{relationships.unfollows}</dd></div>
          <div><dt>Already not following when checked</dt><dd className="text-2xl font-semibold">{relationships.alreadyAbsent}</dd></div>
          <div><dt>Scheduled unfollows</dt><dd className="text-2xl font-semibold">{relationships.pending}</dd><p className="text-sm text-muted-foreground">{relationships.overdue} due now</p></div>
          <div><dt>Observed follow-backs</dt><dd className="text-2xl font-semibold">{relationships.followBacks > 0 ? `${relationships.checked < relationships.follows ? "At least " : ""}${relationships.followBacks}` : relationships.checked === relationships.follows && relationships.follows > 0 ? "0 observed" : "Not fully checked"}</dd>
            <p className="text-sm text-muted-foreground">{relationships.checked} of {relationships.follows} followed accounts checked in the last 24 hours. Counts each observed follow-back once and retains it if they later unfollow. Checks show a relationship change after our follow, not proof of causation. {relationships.checked < relationships.follows && "Incomplete coverage: the total may be higher."}</p></div>
          <div><dt>Message responses</dt><dd>Not tracked yet</dd><p className="text-sm text-muted-foreground">Inbound message collection is not connected.</p></div>
          <div><dt>Comments from engaged accounts</dt><dd>Not tracked yet</dd><p className="text-sm text-muted-foreground">Individual comment collection is not connected. Repeat comments will count separately; repeated collection of the same comment will not.</p></div>
        </dl>
      </section>

      <GrowthOverview data={growth} />

      <section className="mt-10">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Recent engagement</h2>
          <Link
            href="/targets"
            className="text-sm text-muted-foreground hover:text-foreground"
          >
            Manage targets
          </Link>
        </div>
        {feed.length === 0 ? (
          <Card className="mt-4 p-6">
            <p className="text-sm text-muted-foreground">
              No Story engagement yet. Actions appear here after the first run.
            </p>
          </Card>
        ) : (
          <ul className="mt-4 space-y-3">
            {feed.map((item) => (
              <li
                key={item.id}
                className="flex items-start justify-between gap-4 rounded-lg border p-4"
              >
                <div>
                  <p className="text-sm font-medium">
                    {`${ACTION_LABELS[item.action] ?? item.action} from @${item.targetUsername}`}
                  </p>
                  {item.generatedText && (
                    <p className="mt-1 text-sm text-muted-foreground">
                      {item.generatedText}
                    </p>
                  )}
                  <p className="mt-1 text-xs text-muted-foreground">
                    {new Date(
                      item.executedAt ?? item.createdAt,
                    ).toLocaleString("en-US", { timeZone: accountTimezone, timeZoneName: "short" })}
                  </p>
                </div>
                <span
                  className={`shrink-0 rounded-full px-2 py-1 text-xs capitalize ${
                    STATUS_STYLES[item.status] ?? "bg-muted text-muted-foreground"
                  }`}
                >
                  {item.failureCode ?? item.status}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
