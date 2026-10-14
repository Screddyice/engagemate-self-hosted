import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import {
  getEmailPreferences,
  getUserByOwnerKey,
  updateEmailPreferences,
  type EmailPreferences,
} from "@/lib/queries";

function parsePreferences(value: unknown): EmailPreferences | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const input = value as Record<string, unknown>;
  if (
    typeof input.activityEmails !== "boolean" ||
    typeof input.onboardingEmails !== "boolean"
  ) {
    return null;
  }
  return {
    activityEmails: input.activityEmails,
    onboardingEmails: input.onboardingEmails,
  };
}

export async function GET() {
  const { userId } = await auth();
  if (!userId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });
  return NextResponse.json(await getEmailPreferences(user.id));
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
  const preferences = parsePreferences(body);
  if (!preferences) {
    return NextResponse.json(
      { error: "Invalid email preferences" },
      { status: 400 },
    );
  }
  return NextResponse.json(await updateEmailPreferences(user.id, preferences));
}
