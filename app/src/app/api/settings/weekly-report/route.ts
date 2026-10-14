import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import {
  getOrCreateWeeklyReportPreference,
  getUserByOwnerKey,
  updateWeeklyReportPreference,
  type WeeklyReportPreference,
} from "@/lib/queries";
import { isValidIanaTimezone } from "@/lib/timezone";

function safePreference(
  preference: WeeklyReportPreference,
): WeeklyReportPreference {
  return {
    enabled: preference.enabled,
    timezone: preference.timezone,
    sendHour: preference.sendHour,
  };
}

function parsePreference(value: unknown): WeeklyReportPreference | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }

  const input = value as Record<string, unknown>;
  if (
    typeof input.enabled !== "boolean" ||
    !isValidIanaTimezone(input.timezone) ||
    !Number.isInteger(input.sendHour) ||
    (input.sendHour as number) < 0 ||
    (input.sendHour as number) > 23
  ) {
    return null;
  }

  return {
    enabled: input.enabled,
    timezone: input.timezone,
    sendHour: input.sendHour as number,
  };
}

export async function GET() {
  const { userId } = await auth();
  if (!userId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });

  const preference = await getOrCreateWeeklyReportPreference(user.id);
  return NextResponse.json(safePreference(preference));
}

export async function PATCH(request: Request) {
  const { userId } = await auth();
  if (!userId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON" }, { status: 400 });
  }

  const preference = parsePreference(body);
  if (!preference) {
    return NextResponse.json(
      { error: "Invalid weekly report preference" },
      { status: 400 },
    );
  }

  await getOrCreateWeeklyReportPreference(user.id);
  const updated = await updateWeeklyReportPreference(user.id, preference);
  return NextResponse.json(safePreference(updated));
}
