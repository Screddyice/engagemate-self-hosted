import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import { getAccountById, getUserByOwnerKey } from "@/lib/queries";
import { requestOnboardingControl } from "@/lib/onboarding-control-api";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { userId } = await auth();
  if (!userId) return new NextResponse("Unauthorized", { status: 401 });

  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });

  const { id } = await params;
  const account = await getAccountById(user.id, id);
  if (!account) return new NextResponse("Not found", { status: 404 });
  if (account.healthStatus !== "paused" || !account.pausedAt) {
    return NextResponse.json(
      { error: "Account is not paused" },
      { status: 409 },
    );
  }

  const readiness = await requestOnboardingControl<{
    id: string;
    username: string;
    health: string;
    captured_at: string | null;
  }>(`/v1/accounts/${encodeURIComponent(account.id)}`, user.id, {
    method: "GET",
    signal: request.signal,
  });
  if (readiness.status !== 200 || "error" in readiness.body) {
    return NextResponse.json(readiness.body, { status: readiness.status });
  }

  const capturedAt = readiness.body.captured_at
    ? Date.parse(readiness.body.captured_at)
    : Number.NaN;
  if (
    readiness.body.username !== account.username ||
    !Number.isFinite(capturedAt) ||
    capturedAt <= account.pausedAt.getTime()
  ) {
    return NextResponse.json(
      { error: "Reconnect the matching Instagram account before resuming" },
      { status: 409 },
    );
  }

  const result = await requestOnboardingControl(
    `/v1/accounts/${encodeURIComponent(account.id)}/resume`,
    user.id,
    { method: "POST", signal: request.signal },
  );
  return NextResponse.json(result.body, {
    status: result.status,
    headers: result.headers,
  });
}
