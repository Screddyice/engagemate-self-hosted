/** Local workspace safety ceilings. Warm-up and per-account limits still apply. */
export type Plan = "self_hosted";
export const SELF_HOSTED_LIMITS = { targets: 300, dailyActions: 220, dailyViews: 600, igAccounts: 100, monthlyEngagements: 2_147_483_647 } as const;
export function isPlan(value: string | null | undefined): value is Plan { return value === "self_hosted"; }
export function planLimits(_value?: string | null, _accounts?: number | null) { void _value; void _accounts; return SELF_HOSTED_LIMITS; }
