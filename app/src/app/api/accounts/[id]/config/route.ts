import { DEFAULT_FOLLOW_REEL_MESSAGE, validFollowReelTemplate } from "@/lib/follow-reel-settings";
import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import { agentConfigs } from "@/db/schema";
import { saveAccountConfig } from "@/lib/account-config-write";
import {
  getUserByOwnerKey,
  getAccountById,
  getAgentConfig,
} from "@/lib/queries";
import { planLimits } from "@/lib/plans";
import { isValidIanaTimezone } from "@/lib/timezone";

type ConfigUpdate = Partial<typeof agentConfigs.$inferInsert>;

const DAILY_LIMIT_FIELDS = [
  "dailyLikeLimit",
  "dailyReplyLimit",
  "dailyDmLimit",
] as const;

function isIntegerInRange(
  value: unknown,
  min: number,
  max: number
): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= min &&
    value <= max
  );
}

function badRequest(message: string) {
  return NextResponse.json({ error: message }, { status: 400 });
}

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { userId } = await auth();
  if (!userId) return new NextResponse("Unauthorized", { status: 401 });
  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });
  const { id } = await params;
  const account = await getAccountById(user.id, id);
  if (!account) return new NextResponse("Not found", { status: 404 });
  const config = await getAgentConfig(user.id, account.id);
  if (!config) return NextResponse.json({ error: "Saved allocations are unavailable" }, { status: 404 });
  return NextResponse.json({
    igAccountId: account.id,
    planDailyActions: planLimits(user.plan).dailyActions,
    planDailyViews: planLimits(user.plan).dailyViews,
    dailyViewLimit: config.dailyViewLimit,
    dailyLikeLimit: config.dailyLikeLimit,
    dailyReplyLimit: config.dailyReplyLimit,
    dailyDmLimit: config.dailyDmLimit,
    dailyReelMessageLimit: config.dailyReelMessageLimit ?? null,
    warmupExempt: config.warmupExempt ?? false,
    followAfterEngagement: config.followAfterEngagement,
    dailyFollowLimit: config.dailyFollowLimit,
    autoUnfollowEnabled: config.autoUnfollowEnabled,
    followEnabledAt: config.followEnabledAt,
    followReelMessageEnabled: config.followReelMessageEnabled ?? false,
    followReelMessageEnabledAt: config.followReelMessageEnabledAt ?? null,
    followReelMessageTemplate: config.followReelMessageTemplate ?? DEFAULT_FOLLOW_REEL_MESSAGE,
  }, { headers: { "Cache-Control": "private, no-store" } });
}

export async function PUT(
  req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { userId } = await auth();
  if (!userId) return new NextResponse("Unauthorized", { status: 401 });

  const { id } = await params;

  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });

  // Verify the account belongs to the caller
  const account = await getAccountById(user.id, id);
  if (!account) {
    return new NextResponse("Not found", { status: 404 });
  }

  let parsed: unknown;
  try {
    parsed = await req.json();
  } catch {
    return badRequest("Request body must be a JSON object");
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return badRequest("Request body must be a JSON object");
  }
  const body = parsed as Record<string, unknown>;
  const writable = new Set(["enabled", "timezone", "scheduleHour", "dailyLikeLimit", "dailyReplyLimit", "dailyDmLimit", "dailyReelMessageLimit", "dailyViewLimit", "growthGoal", "persona", "bannedPhrases", "expectedLanguage", "followAfterEngagement", "dailyFollowLimit", "autoUnfollowEnabled", "followReelMessageEnabled", "followReelMessageTemplate"]);
  if (Object.keys(body).some((key) => !writable.has(key))) return badRequest("Unknown or read-only setting");
  if (body.dailyDmLimit !== undefined && body.dailyDmLimit !== 0) return badRequest("Standalone DMs are no longer supported. Use Story replies.");
  const limits = planLimits(user.plan);
  const updates: ConfigUpdate = {};
  if (body.dailyReelMessageLimit !== undefined) {
    if (body.dailyReelMessageLimit !== null && !isIntegerInRange(body.dailyReelMessageLimit, 0, Math.min(20, limits.dailyActions))) {
      return badRequest(`dailyReelMessageLimit must be null or an integer between 0 and ${Math.min(20, limits.dailyActions)}`);
    }
    updates.dailyReelMessageLimit = body.dailyReelMessageLimit as number | null;
  }

  if (body.enabled !== undefined) {
    if (typeof body.enabled !== "boolean") {
      return badRequest("enabled must be a boolean");
    }
    if (body.enabled) {
      return badRequest("Use onboarding launch to enable an account");
    }
    updates.enabled = body.enabled;
  }

  if (body.timezone !== undefined) {
    if (typeof body.timezone !== "string" || !isValidIanaTimezone(body.timezone.trim())) {
      return badRequest("timezone must be a valid IANA timezone");
    }
    updates.timezone = new Intl.DateTimeFormat("en-US", {timeZone:body.timezone.trim()}).resolvedOptions().timeZone;
  }

  if (body.scheduleHour !== undefined) {
    if (!isIntegerInRange(body.scheduleHour, 0, 23)) {
      return badRequest("scheduleHour must be an integer between 0 and 23");
    }
    updates.scheduleHour = body.scheduleHour;
  }

  // The plan ceiling caps each action budget; the worker still applies the
  // lower of this, the warm-up cap, and the configured value.
  for (const field of DAILY_LIMIT_FIELDS) {
    const value = body[field];
    if (value === undefined) continue;
    if (!isIntegerInRange(value, 0, limits.dailyActions)) {
      return badRequest(
        `${field} must be an integer between 0 and ${limits.dailyActions}`
      );
    }
    updates[field] = value;
  }

  if (body.dailyViewLimit !== undefined) {
    if (!isIntegerInRange(body.dailyViewLimit, 0, limits.dailyViews)) {
      return badRequest(`dailyViewLimit must be an integer between 0 and ${limits.dailyViews}`);
    }
    updates.dailyViewLimit = body.dailyViewLimit;
  }

  if (body.followAfterEngagement !== undefined) {
    if (typeof body.followAfterEngagement !== "boolean") return badRequest("followAfterEngagement must be a boolean");
    updates.followAfterEngagement = body.followAfterEngagement;
  }
  if (body.autoUnfollowEnabled !== undefined) {
    if (typeof body.autoUnfollowEnabled !== "boolean") return badRequest("autoUnfollowEnabled must be a boolean");
    updates.autoUnfollowEnabled = body.autoUnfollowEnabled;
  }
  if (body.followReelMessageEnabled !== undefined) {
    if (typeof body.followReelMessageEnabled !== "boolean") return badRequest("followReelMessageEnabled must be a boolean");
    updates.followReelMessageEnabled = body.followReelMessageEnabled;
  }
  if (body.followReelMessageTemplate !== undefined) {
    if (!validFollowReelTemplate(body.followReelMessageTemplate)) return badRequest("Reel message must contain 1–500 characters without links. EngageMate adds your reel link.");
    updates.followReelMessageTemplate = body.followReelMessageTemplate.trim();
  }
  if (body.dailyFollowLimit !== undefined) {
    if (!isIntegerInRange(body.dailyFollowLimit, 0, 20)) return badRequest("dailyFollowLimit must be an integer between 0 and 20");
    updates.dailyFollowLimit = body.dailyFollowLimit;
  }

  if (body.growthGoal !== undefined) {
    if (body.growthGoal === null) {
      updates.growthGoal = null;
    } else if (!isIntegerInRange(body.growthGoal, 0, 1_000_000)) {
      return badRequest("growthGoal must be a non-negative integer");
    } else {
      updates.growthGoal = body.growthGoal;
    }
  }

  if (body.persona !== undefined) {
    if (body.persona === null) {
      updates.persona = null;
    } else if (typeof body.persona !== "string" || body.persona.length > 2000) {
      return badRequest("persona must be a string of at most 2000 characters");
    } else {
      updates.persona = body.persona;
    }
  }

  if (body.bannedPhrases !== undefined) {
    const phrases = body.bannedPhrases;
    if (
      !Array.isArray(phrases) ||
      phrases.some((p) => typeof p !== "string" || p.trim() === "")
    ) {
      return badRequest("bannedPhrases must be an array of non-empty strings");
    }
    updates.bannedPhrases = (phrases as string[]).map((p) => p.trim());
  }

  if (body.expectedLanguage !== undefined) {
    if (
      typeof body.expectedLanguage !== "string" ||
      !/^[a-z]{2}(-[A-Za-z0-9]{2,8})?$/.test(body.expectedLanguage)
    ) {
      return badRequest("expectedLanguage must be a language tag such as 'en'");
    }
    updates.expectedLanguage = body.expectedLanguage;
  }

  const config = await saveAccountConfig(user.id, account.id, userId, updates);
  return NextResponse.json({ success: true, version: config.version });
}
