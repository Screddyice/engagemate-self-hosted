import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import { requestOnboardingControl } from "@/lib/onboarding-control-api";
import { getUserByOwnerKey } from "@/lib/queries";

export async function proxyOnboardingJsonPost(
  request: Request,
  upstreamPath: string,
) {
  const { userId } = await auth();
  if (!userId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const user = await getUserByOwnerKey(userId);
  if (!user) {
    return NextResponse.json({ error: "User not found" }, { status: 404 });
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON" }, { status: 400 });
  }

  const result = await requestOnboardingControl<Record<string, unknown>>(
    upstreamPath,
    user.id,
    { method: "POST", body: JSON.stringify(body), signal: request.signal },
  );
  return NextResponse.json(result.body, {
    status: result.status,
    headers: result.headers,
  });
}
