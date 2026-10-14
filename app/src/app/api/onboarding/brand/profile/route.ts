import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import { getUserByOwnerKey } from "@/lib/queries";
import { requestOnboardingControl } from "@/lib/onboarding-control-api";
import type { OnboardingBrandProfile } from "@/lib/onboarding-profile";

export async function GET(request: Request) {
  const { userId } = await auth();
  if (!userId) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const user = await getUserByOwnerKey(userId);
  if (!user) return NextResponse.json({ error: "User not found" }, { status: 404 });

  const searchParams = new URL(request.url).searchParams;
  const igAccountId = searchParams.get("ig_account_id")?.trim();
  if (!igAccountId) {
    return NextResponse.json(
      { error: "Instagram account is required" },
      { status: 400 },
    );
  }
  const upstreamQuery = new URLSearchParams({ ig_account_id: igAccountId });
  if (searchParams.get("details") === "false") {
    upstreamQuery.set("details", "false");
  }
  const path = `/v1/onboarding/brand/profile?${upstreamQuery}`;
  const result = await requestOnboardingControl<OnboardingBrandProfile>(
    path,
    user.id,
    { signal: request.signal },
  );
  return NextResponse.json(result.body, {
    status: result.status,
    headers: result.headers,
  });
}
