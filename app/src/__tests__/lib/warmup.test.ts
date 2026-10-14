import { describe, it, expect } from "vitest";
import {
  describeCaps,
  effectiveCaps,
  warmupDay,
  warmupStage,
} from "@/lib/warmup";

const NOW = new Date("2026-07-31T12:00:00Z");
const DAY = 24 * 60 * 60 * 1000;

function startedDaysAgo(days: number) {
  return new Date(NOW.getTime() - days * DAY);
}

describe("warmupDay", () => {
  it("counts the first day as day 1", () => {
    expect(warmupDay(NOW, NOW)).toBe(1);
    expect(warmupDay(startedDaysAgo(0.5), NOW)).toBe(1);
  });

  it("counts whole elapsed days", () => {
    expect(warmupDay(startedDaysAgo(1), NOW)).toBe(2);
    expect(warmupDay(startedDaysAgo(30), NOW)).toBe(31);
  });

  it("returns null when warm-up has not started", () => {
    expect(warmupDay(null, NOW)).toBeNull();
    expect(warmupDay(undefined, NOW)).toBeNull();
  });

  it("returns null for a start date in the future", () => {
    expect(warmupDay(new Date(NOW.getTime() + DAY), NOW)).toBeNull();
  });
});

describe("warmupStage", () => {
  it("shows a mature stage only for an exempt established account", () => {
    expect(warmupStage(startedDaysAgo(1),NOW,true)?.label).toBe("Established account");
    expect(warmupStage(startedDaysAgo(1),NOW,true)?.caps.reply).toBe(20);
    expect(warmupStage(startedDaysAgo(1),NOW)?.caps.reply).toBe(0);
    expect(warmupStage(null,NOW,true)).toBeNull();
  });
  const cases: [number, string, { like: number; reply: number; dm: number }][] =
    [
      [0, "Days 1-3", { like: 3, reply: 0, dm: 0 }],
      [2, "Days 1-3", { like: 3, reply: 0, dm: 0 }],
      [3, "Days 4-7", { like: 5, reply: 1, dm: 0 }],
      [6, "Days 4-7", { like: 5, reply: 1, dm: 0 }],
      [7, "Days 8-14", { like: 8, reply: 2, dm: 0 }],
      [13, "Days 8-14", { like: 8, reply: 2, dm: 0 }],
      [14, "Days 15-30", { like: 12, reply: 3, dm: 0 }],
      [29, "Days 15-30", { like: 12, reply: 3, dm: 0 }],
      [30, "Days 31-44", { like: 20, reply: 10, dm: 0 }],
      [400, "Day 101+", { like: 200, reply: 20, dm: 0 }],
    ];

  it.each(cases)(
    "maps %i elapsed days to %s",
    (elapsed, label, caps) => {
      const stage = warmupStage(startedDaysAgo(elapsed), NOW);
      expect(stage?.label).toBe(label);
      expect(stage?.caps).toEqual(caps);
    },
  );

  it("returns null when warm-up has not started", () => {
    expect(warmupStage(null, NOW)).toBeNull();
  });
});

describe("effectiveCaps", () => {
  const configured = { like: 20, reply: 10, dm: 0 };

  it("takes the warm-up cap when it is the lowest", () => {
    const stage = warmupStage(startedDaysAgo(1), NOW);
    expect(effectiveCaps(stage, configured, 20)).toEqual({
      like: 3,
      reply: 0,
      dm: 0,
    });
  });

  it("takes the configured cap when it is the lowest", () => {
    const stage = warmupStage(startedDaysAgo(40), NOW);
    expect(effectiveCaps(stage, { like: 4, reply: 1, dm: 0 }, 30)).toEqual({
      like: 4,
      reply: 1,
      dm: 0,
    });
  });

  it("takes the plan cap when it is the lowest", () => {
    const stage = warmupStage(startedDaysAgo(40), NOW);
    expect(effectiveCaps(stage, configured, 1)).toEqual({
      like: 1,
      reply: 1,
      dm: 0,
    });
  });

  it("grants no budget before warm-up starts", () => {
    expect(effectiveCaps(null, configured, 30)).toEqual({
      like: 0,
      reply: 0,
      dm: 0,
    });
  });

  it("never returns a negative cap", () => {
    const stage = warmupStage(startedDaysAgo(40), NOW);
    expect(effectiveCaps(stage, { like: -5, reply: 0, dm: 0 }, 30)).toEqual({
      like: 0,
      reply: 0,
      dm: 0,
    });
  });
});

describe("describeCaps", () => {
  it("uses singular labels for a cap of one", () => {
    expect(describeCaps({ like: 1, reply: 1, dm: 1 })).toBe(
      "1 like, 1 reply",
    );
  });

  it("uses plural labels otherwise", () => {
    expect(describeCaps({ like: 8, reply: 2, dm: 0 })).toBe(
      "8 likes, 2 replies",
    );
  });
});
