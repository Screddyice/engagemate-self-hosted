import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import { getUserByOwnerKey, getUserAccounts } from "@/lib/queries";
import { AccountLimitReached, connectAccount } from "@/lib/account-limits";

// Matches the canonical Instagram handle the control plane persists.
const USERNAME_PATTERN = /^[a-z0-9._]{1,64}$/;

export async function GET() {
  const { userId } = await auth();
  if (!userId) return new NextResponse("Unauthorized", { status: 401 });

  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });

  const accounts = await getUserAccounts(user.id);
  return NextResponse.json(accounts);
}

export async function POST(req: Request) {
  const { userId } = await auth();
  if (!userId) return new NextResponse("Unauthorized", { status: 401 });

  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });

  const body = (await req.json()) as { username?: unknown };
  const username =
    typeof body?.username === "string"
      ? body.username.trim().toLowerCase()
      : "";

  if (!USERNAME_PATTERN.test(username)) {
    return NextResponse.json(
      { error: "Invalid Instagram username" },
      { status: 400 }
    );
  }

  try {
    // The Instagram session itself is captured by the Control API and encrypted
    // into ig_sessions; this route only creates the tenant's account row.
    const { account, created } = await connectAccount({
      userId: user.id,
      username,
    });
    return NextResponse.json(account, { status: created ? 201 : 200 });
  } catch (error) {
    if (error instanceof AccountLimitReached) {
      return NextResponse.json(
        { error: error.code, limit: error.limit },
        { status: 409 },
      );
    }
    throw error;
  }
}
