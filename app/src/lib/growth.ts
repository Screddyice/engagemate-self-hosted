export type GrowthMetricPoint = {
  date: string;
  value: number | null;
};

export type GrowthChartPoint = {
  date: string;
  value: number;
};

export type GrowthTrend = {
  start: number;
  end: number;
  delta: number;
};

export type GrowthActivity = {
  reviewed: number;
  likes: number;
  replies: number;
  dms: number;
  views: number;
  skipped: number;
  gated: number;
  failed: number;
};

export type GrowthDashboard = {
  points: GrowthChartPoint[];
  currentFollowers: number | null;
  currentEngagementRateBps: number | null;
  followerTrend7d: GrowthTrend | null;
  followerTrend30d: GrowthTrend | null;
  engagementTrend7d: GrowthTrend | null;
  engagementTrend30d: GrowthTrend | null;
  growthGoal: number | null;
  activity7d: GrowthActivity;
  estimatedTimeSavedSeconds: number;
};

type TimeSavedCounts = {
  reviewed: number;
  replies: number;
  dms: number;
};

const MAX_SIGNED_64_BIT = BigInt("9223372036854775807");
const MAX_SAFE_INTEGER = BigInt(Number.MAX_SAFE_INTEGER);
const THIRTY_SECONDS = BigInt(30);
const SIXTY_SECONDS = BigInt(60);

function checkedCount(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new RangeError(`${name} must be a nonnegative safe integer`);
  }
  return value;
}

export function postgresCountToSafeInteger(
  value: unknown,
  name: string,
): number {
  if (typeof value === "number") {
    return checkedCount(value, name);
  }

  let parsed: bigint;
  if (typeof value === "bigint") {
    parsed = value;
  } else if (typeof value === "string" && /^\d+$/.test(value)) {
    parsed = BigInt(value);
  } else {
    throw new RangeError(`${name} must be a nonnegative safe integer`);
  }

  if (parsed < BigInt(0) || parsed > MAX_SAFE_INTEGER) {
    throw new RangeError(`${name} must be a nonnegative safe integer`);
  }
  return Number(parsed);
}

export function estimateTimeSavedSeconds({
  reviewed,
  replies,
  dms,
}: TimeSavedCounts): number {
  const reviewedCount = checkedCount(reviewed, "reviewed");
  const replyCount = checkedCount(replies, "replies");
  const dmCount = checkedCount(dms, "dms");
  const seconds =
    BigInt(reviewedCount) * THIRTY_SECONDS +
    (BigInt(replyCount) + BigInt(dmCount)) * SIXTY_SECONDS;

  if (seconds > MAX_SIGNED_64_BIT) {
    throw new RangeError("estimated time exceeds signed 64-bit range");
  }
  if (seconds > MAX_SAFE_INTEGER) {
    throw new RangeError("estimated time exceeds JavaScript safe integer range");
  }
  return Number(seconds);
}

export function buildTrend(
  points: readonly GrowthMetricPoint[],
  boundary: string,
): GrowthTrend | null {
  const validPoints = points
    .filter(
      (point): point is GrowthChartPoint => point.value !== null,
    )
    .map((point) => ({
      ...point,
      value: checkedCount(point.value, "snapshot value"),
    }))
    .sort((left, right) => left.date.localeCompare(right.date));

  if (validPoints.length < 2) return null;

  const start = validPoints.findLast((point) => point.date <= boundary);
  const end = validPoints.at(-1);
  if (!start || !end || start.date === end.date) return null;

  return {
    start: start.value,
    end: end.value,
    delta: end.value - start.value,
  };
}

export function latestMetricValue(
  points: readonly GrowthMetricPoint[],
): number | null {
  const latest = points
    .filter((point): point is GrowthChartPoint => point.value !== null)
    .map((point) => ({
      ...point,
      value: checkedCount(point.value, "snapshot value"),
    }))
    .sort((left, right) => right.date.localeCompare(left.date))[0];

  return latest?.value ?? null;
}
