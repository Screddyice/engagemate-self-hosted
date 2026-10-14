export type ActionCaps = {
  like: number;
  reply: number;
  dm: number;
};

export type WarmupStage = {
  label: string;
  day: number;
  caps: ActionCaps;
};

const MS_PER_DAY = 24 * 60 * 60 * 1000;

/**
 * The launch warm-up ladder, mirroring the worker's brackets in
 * `api/engagemate/domain/budgets.py`. Post-warm-up stages raise the action ceiling;
 * the plan cap is applied separately.
 */
export const WARMUP_BRACKETS: {
  throughDay: number | null;
  label: string;
  caps: ActionCaps;
}[] = [
  { throughDay: 3, label: "Days 1-3", caps: { like: 3, reply: 0, dm: 0 } },
  { throughDay: 7, label: "Days 4-7", caps: { like: 5, reply: 1, dm: 0 } },
  { throughDay: 14, label: "Days 8-14", caps: { like: 8, reply: 2, dm: 0 } },
  { throughDay: 30, label: "Days 15-30", caps: { like: 12, reply: 3, dm: 0 } },
  { throughDay: 44, label: "Days 31-44", caps: { like: 20, reply: 10, dm: 0 } },
  { throughDay: 58, label: "Days 45-58", caps: { like: 40, reply: 12, dm: 0 } },
  { throughDay: 72, label: "Days 59-72", caps: { like: 80, reply: 15, dm: 0 } },
  { throughDay: 86, label: "Days 73-86", caps: { like: 120, reply: 18, dm: 0 } },
  { throughDay: 100, label: "Days 87-100", caps: { like: 160, reply: 20, dm: 0 } },
  { throughDay: null, label: "Day 101+", caps: { like: 200, reply: 20, dm: 0 } },
];

export function warmupDay(
  startedAt: Date | null | undefined,
  now: Date,
): number | null {
  if (!startedAt) return null;
  const elapsed = now.getTime() - startedAt.getTime();
  if (!Number.isFinite(elapsed) || elapsed < 0) return null;
  return Math.floor(elapsed / MS_PER_DAY) + 1;
}

export function warmupStage(
  startedAt: Date | null | undefined,
  now: Date,
  exempt = false,
): WarmupStage | null {
  const day = warmupDay(startedAt, now);
  if (day === null) return null;

  if (exempt) return { label: "Established account", day, caps: WARMUP_BRACKETS[WARMUP_BRACKETS.length - 1].caps };
  const bracket =
    WARMUP_BRACKETS.find(
      (candidate) => candidate.throughDay === null || day <= candidate.throughDay,
    ) ?? WARMUP_BRACKETS[WARMUP_BRACKETS.length - 1];

  return { label: bracket.label, day, caps: bracket.caps };
}

/**
 * The cap the account actually runs at: the lowest of the warm-up bracket, the
 * plan's daily action ceiling, and the user's own configured limits. An account
 * with no warm-up start has no budget at all.
 */
export function effectiveCaps(
  stage: WarmupStage | null,
  configured: ActionCaps,
  planDailyActions: number,
): ActionCaps {
  if (!stage) return { like: 0, reply: 0, dm: 0 };
  return {
    like: Math.max(
      Math.min(stage.caps.like, configured.like, planDailyActions),
      0,
    ),
    reply: Math.max(
      Math.min(stage.caps.reply, configured.reply, planDailyActions),
      0,
    ),
    dm: Math.max(Math.min(stage.caps.dm, configured.dm, planDailyActions), 0),
  };
}

export function describeCaps(caps: ActionCaps): string {
  return [
    `${caps.like} ${caps.like === 1 ? "like" : "likes"}`,
    `${caps.reply} ${caps.reply === 1 ? "reply" : "replies"}`,
  ].join(", ");
}
