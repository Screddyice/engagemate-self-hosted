import { auth } from "@/lib/owner-auth";
import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { getAccountById, getUserByOwnerKey } from "@/lib/queries";
import { SELECTED_ACCOUNT_COOKIE } from "@/lib/selected-account";

export async function POST(req: Request) {
  const { userId } = await auth();
  if (!userId) return new NextResponse("Unauthorized", { status: 401 });

  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });

  const body = (await req.json()) as { igAccountId?: unknown };
  if (
    typeof body.igAccountId !== "string" ||
    body.igAccountId.length === 0 ||
    body.igAccountId.length > 128
  ) {
    return NextResponse.json({ error: "invalid_account" }, { status: 400 });
  }

  const account = await getAccountById(user.id, body.igAccountId);
  if (!account) {
    return NextResponse.json({ error: "account_not_found" }, { status: 404 });
  }

  (await cookies()).set(SELECTED_ACCOUNT_COOKIE, account.id, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 24 * 365,
  });
  return NextResponse.json({ selected: account.id });
}
