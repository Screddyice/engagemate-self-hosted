import { describe, expect, it } from "vitest";
import {
  buildTrend,
  estimateTimeSavedSeconds,
  latestMetricValue,
  postgresCountToSafeInteger,
} from "@/lib/growth";

describe("growth calculations", () => {
  it("matches the published manual-time estimate", () => {
    expect(
      estimateTimeSavedSeconds({ reviewed: 4, replies: 1, dms: 2 }),
    ).toBe(300);
  });

  it.each([
    { reviewed: -1, replies: 0, dms: 0 },
    { reviewed: 1.5, replies: 0, dms: 0 },
    { reviewed: true as unknown as number, replies: 0, dms: 0 },
  ])("rejects invalid activity counts: $reviewed", (counts) => {
    expect(() => estimateTimeSavedSeconds(counts)).toThrow(
      /nonnegative safe integer/,
    );
  });

  it("rejects estimates that cannot be represented exactly", () => {
    expect(() =>
      estimateTimeSavedSeconds({
        reviewed: Number.MAX_SAFE_INTEGER,
        replies: 0,
        dms: 0,
      }),
    ).toThrow(/safe integer range/);
  });

  it("converts PostgreSQL bigint counts through the safe-integer boundary", () => {
    expect(postgresCountToSafeInteger("4", "reviewed")).toBe(4);
    expect(
      postgresCountToSafeInteger(
        String(Number.MAX_SAFE_INTEGER),
        "reviewed",
      ),
    ).toBe(Number.MAX_SAFE_INTEGER);
    expect(
      postgresCountToSafeInteger(BigInt(3), "reviewed"),
    ).toBe(3);
  });

  it.each(["9007199254740992", "-1", "1.5", true])(
    "rejects an unsafe PostgreSQL count: %s",
    (value) => {
      expect(() => postgresCountToSafeInteger(value, "reviewed")).toThrow(
        /nonnegative safe integer/,
      );
    },
  );

  it("does not invent a trend from one valid snapshot", () => {
    expect(
      buildTrend([{ date: "2026-08-14", value: 100 }], "2026-08-07"),
    ).toBeNull();
  });

  it("builds a signed trend from snapshots at the boundary and period end", () => {
    expect(
      buildTrend(
        [
          { date: "2026-08-07", value: 100 },
          { date: "2026-08-14", value: 108 },
        ],
        "2026-08-07",
      ),
    ).toEqual({ start: 100, end: 108, delta: 8 });
  });

  it("uses the nearest valid snapshot at or before the boundary", () => {
    expect(
      buildTrend(
        [
          { date: "2026-08-14", value: 108 },
          { date: "2026-08-08", value: 101 },
          { date: "2026-08-05", value: 90 },
        ],
        "2026-08-07",
      ),
    ).toEqual({ start: 90, end: 108, delta: 18 });
  });

  it("ignores missing metric values without turning them into zero", () => {
    expect(
      buildTrend(
        [
          { date: "2026-08-07", value: null },
          { date: "2026-08-14", value: 108 },
        ],
        "2026-08-07",
      ),
    ).toBeNull();
  });

  it("keeps the latest valid metric when a newer partial snapshot omits it", () => {
    expect(
      latestMetricValue([
        { date: "2026-08-07", value: 425 },
        { date: "2026-08-14", value: null },
      ]),
    ).toBe(425);
  });
});
