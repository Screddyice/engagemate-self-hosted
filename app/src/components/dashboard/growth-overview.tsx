import { FollowerTrendChart } from "@/components/dashboard/follower-trend-chart";
import { Card } from "@/components/ui/card";
import type { GrowthDashboard, GrowthTrend } from "@/lib/growth";

function formatCount(value: number) {
  return value.toLocaleString("en-US");
}

function formatSigned(value: number) {
  return `${value >= 0 ? "+" : ""}${formatCount(value)}`;
}

function formatRate(basisPoints: number) {
  return `${(basisPoints / 100).toFixed(2)}%`;
}

function formatRateChange(basisPoints: number) {
  const percentagePoints = basisPoints / 100;
  return `${percentagePoints >= 0 ? "+" : ""}${percentagePoints.toFixed(2)} pp`;
}

function formatDuration(seconds: number) {
  if (seconds === 0) return "0 min";
  if (seconds < 60) return "<1 min";

  const totalMinutes = Math.floor(seconds / 60);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours === 0) return `${totalMinutes} min`;
  if (minutes === 0) return `${hours} hr`;
  return `${hours} hr ${minutes} min`;
}

function TrendValue({
  label,
  trend,
  format,
}: {
  label: string;
  trend: GrowthTrend | null;
  format: (value: number) => string;
}) {
  const tone =
    trend === null
      ? "text-muted-foreground"
      : trend.delta > 0
        ? "text-green-600 dark:text-green-400"
        : trend.delta < 0
          ? "text-red-600 dark:text-red-400"
          : "text-foreground";

  return (
    <div>
      <dt className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        {label}
      </dt>
      <dd className={`mt-1 font-mono text-sm font-semibold tabular-nums ${tone}`}>
        {trend === null ? "Not available yet" : format(trend.delta)}
      </dd>
    </div>
  );
}

export function GrowthOverview({ data }: { data: GrowthDashboard }) {
  const growthGoal = data.growthGoal;
  const hasGoal = growthGoal !== null;
  const progress =
    data.growthGoal !== null &&
    data.growthGoal > 0 &&
    data.currentFollowers !== null
      ? Math.min(
          100,
          Math.round((data.currentFollowers / data.growthGoal) * 100),
        )
      : null;
  const activity = data.activity7d;

  return (
    <section className="mt-10" aria-labelledby="growth-heading">
      <div className="flex items-end justify-between gap-4">
        <h2 id="growth-heading" className="text-lg font-semibold">
          Growth
        </h2>
        <p className="text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground">
          30-day account brief
        </p>
      </div>

      <Card className="mt-4 overflow-hidden border-blue-500/20 p-0">
        <div className="grid lg:grid-cols-[minmax(0,1.5fr)_minmax(16rem,0.7fr)]">
          <div className="border-b p-5 sm:p-6 lg:border-b-0 lg:border-r">
            <div className="flex flex-col gap-5 sm:flex-row sm:items-end sm:justify-between">
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                  Current followers
                </p>
                <p className="mt-2 font-mono text-4xl font-bold tracking-tight tabular-nums">
                  {data.currentFollowers === null
                    ? "Not available yet"
                    : formatCount(data.currentFollowers)}
                </p>
              </div>
              <dl className="grid grid-cols-2 gap-x-8 gap-y-3">
                <TrendValue
                  label="7-day change"
                  trend={data.followerTrend7d}
                  format={formatSigned}
                />
                <TrendValue
                  label="30-day change"
                  trend={data.followerTrend30d}
                  format={formatSigned}
                />
              </dl>
            </div>

            <div className="mt-6 rounded-lg border bg-muted/20 px-3 py-4">
              <FollowerTrendChart points={data.points} />
            </div>
          </div>

          <div className="p-5 sm:p-6">
            <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Public post engagement
            </p>
            <p className="mt-2 font-mono text-3xl font-bold tabular-nums">
              {data.currentEngagementRateBps === null
                ? "Not available yet"
                : formatRate(data.currentEngagementRateBps)}
            </p>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              Average likes and comments on recent public posts
            </p>
            <dl className="mt-6 grid grid-cols-2 gap-4 border-t pt-5 lg:grid-cols-1">
              <TrendValue
                label="7-day change"
                trend={data.engagementTrend7d}
                format={formatRateChange}
              />
              <TrendValue
                label="30-day change"
                trend={data.engagementTrend30d}
                format={formatRateChange}
              />
            </dl>
          </div>
        </div>

        <div
          className={`grid border-t bg-muted/10 md:grid-cols-2 ${hasGoal ? "xl:grid-cols-3" : "xl:grid-cols-2"}`}
        >
          <div className="border-b p-5 md:border-b-0 md:border-r sm:p-6">
            <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Agent activity · 7 days
            </p>
            <p className="mt-3 font-mono text-xl font-semibold tabular-nums">
              {`${formatCount(activity.reviewed)} opportunities reviewed`}
            </p>
            <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
              {`${formatCount(activity.likes)} likes · ${formatCount(activity.replies)} ${activity.replies === 1 ? "reply" : "replies"} · ${formatCount(activity.dms)} DMs · ${formatCount(activity.views)} views`}
            </p>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              {`${formatCount(activity.skipped)} skipped · ${formatCount(activity.gated)} gated · ${formatCount(activity.failed)} failed`}
            </p>
          </div>

          <div
            className={`border-b p-5 md:border-b-0 sm:p-6 ${hasGoal ? "xl:border-r" : ""}`}
          >
            <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Estimated manual time saved
            </p>
            <p className="mt-3 font-mono text-2xl font-bold tabular-nums">
              {formatDuration(data.estimatedTimeSavedSeconds)}
            </p>
            <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
              30 seconds per opportunity reviewed + 60 seconds per sent reply or
              DM.
            </p>
          </div>

          {growthGoal !== null && (
            <div className="p-5 sm:p-6 md:col-span-2 xl:col-span-1">
              <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                Growth goal
              </p>
              {progress === null ? (
                <p className="mt-3 text-sm text-muted-foreground">
                  {`Goal set to ${formatCount(growthGoal)} followers. Progress appears after the first valid snapshot.`}
                </p>
              ) : (
                <>
                  <p className="mt-3 text-sm font-medium">
                    {`${formatCount(data.currentFollowers ?? 0)} of ${formatCount(growthGoal)} followers`}
                  </p>
                  <div
                    role="progressbar"
                    aria-label="Growth goal"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={progress}
                    aria-valuetext={`${progress}% of follower goal`}
                    className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted"
                  >
                    <div
                      className="h-full rounded-full bg-blue-500"
                      style={{ width: `${progress}%` }}
                    />
                  </div>
                  <p className="mt-2 font-mono text-xs font-semibold tabular-nums text-muted-foreground">
                    {`${progress}%`}
                  </p>
                </>
              )}
            </div>
          )}
        </div>

        <p className="border-t px-5 py-4 text-xs leading-relaxed text-muted-foreground sm:px-6">
          Account trends are observational. EngageMate does not attribute
          follower or engagement changes to any individual target or action.
        </p>
      </Card>
    </section>
  );
}
