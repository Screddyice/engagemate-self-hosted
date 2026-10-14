import type { GrowthChartPoint } from "@/lib/growth";

const WIDTH = 520;
const HEIGHT = 150;
const PADDING = 8;

function formatCount(value: number) {
  return value.toLocaleString("en-US");
}

function formatDate(date: string) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${date}T00:00:00Z`));
}

type DatedChartPoint = GrowthChartPoint & { timestamp: number };

function parseSnapshotDate(date: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  const [year, month, day] = date.split("-").map(Number);
  const timestamp = Date.UTC(year, month - 1, day);
  if (!Number.isFinite(timestamp)) return null;
  return new Date(timestamp).toISOString().slice(0, 10) === date
    ? timestamp
    : null;
}

function validatedPoints(
  points: readonly GrowthChartPoint[],
): DatedChartPoint[] {
  const dated: DatedChartPoint[] = [];
  for (const point of points) {
    const timestamp = parseSnapshotDate(point.date);
    if (
      timestamp === null ||
      !Number.isSafeInteger(point.value) ||
      point.value < 0
    ) {
      return [];
    }
    dated.push({ ...point, timestamp });
  }
  dated.sort((left, right) => left.timestamp - right.timestamp);
  if (
    dated.some(
      (point, index) =>
        index > 0 && point.timestamp === dated[index - 1].timestamp,
    )
  ) {
    return [];
  }
  return dated;
}

function chartSummary(points: readonly GrowthChartPoint[]) {
  if (points.length === 0) {
    return "Follower trend is not available yet.";
  }
  if (points.length === 1) {
    const point = points[0];
    return `Follower trend has one observation: ${formatCount(point.value)} followers on ${formatDate(point.date)}. A trend is not available yet.`;
  }

  const first = points[0];
  const last = points.at(-1) ?? first;
  return `Follower trend from ${formatCount(first.value)} followers on ${formatDate(first.date)} to ${formatCount(last.value)} followers on ${formatDate(last.date)}.`;
}

export function FollowerTrendChart({
  points,
}: {
  points: readonly GrowthChartPoint[];
}) {
  const ordered = validatedPoints(points);
  const summary = chartSummary(ordered);
  const values = ordered.map((point) => point.value);
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const range = maximum - minimum;
  const plotWidth = WIDTH - PADDING * 2;
  const plotHeight = HEIGHT - PADDING * 2;
  const firstTimestamp = ordered[0]?.timestamp ?? 0;
  const lastTimestamp = ordered.at(-1)?.timestamp ?? firstTimestamp;
  const elapsedTime = lastTimestamp - firstTimestamp;
  const coordinates = ordered.map((point) => {
    const x =
      elapsedTime === 0
        ? WIDTH / 2
        : PADDING +
          ((point.timestamp - firstTimestamp) / elapsedTime) * plotWidth;
    const y =
      range === 0
        ? HEIGHT / 2
        : PADDING + ((maximum - point.value) / range) * plotHeight;
    return { x, y };
  });
  const polyline = coordinates.map(({ x, y }) => `${x},${y}`).join(" ");

  return (
    <figure>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={summary}
        className="h-36 w-full overflow-visible"
      >
        <title>{summary}</title>
        <line
          x1={PADDING}
          y1={HEIGHT / 2}
          x2={WIDTH - PADDING}
          y2={HEIGHT / 2}
          className="stroke-border"
          strokeDasharray="4 7"
        />
        {coordinates.length > 1 && (
          <polyline
            points={polyline}
            fill="none"
            className="stroke-blue-500"
            strokeWidth="3"
            strokeLinecap="round"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
        )}
        {coordinates.map(({ x, y }, index) => (
          <circle
            key={ordered[index].date}
            cx={x}
            cy={y}
            r={index === coordinates.length - 1 ? 4 : 2.5}
            className="fill-background stroke-blue-500"
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
          />
        ))}
      </svg>
      <p className="sr-only">{summary}</p>
      {ordered.length > 0 && (
        <figcaption className="mt-2 flex justify-between font-mono text-[11px] tabular-nums text-muted-foreground">
          <span>{formatDate(ordered[0].date)}</span>
          <span>{formatDate(ordered.at(-1)?.date ?? ordered[0].date)}</span>
        </figcaption>
      )}
    </figure>
  );
}
