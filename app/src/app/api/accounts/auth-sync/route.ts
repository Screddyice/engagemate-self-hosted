import { NextResponse } from "next/server";
import { verifyExtensionTicket } from "@/lib/extension-ticket";
import { AccountLimitReached, connectAccount } from "@/lib/account-limits";

const USERNAME_PATTERN = /^[a-z0-9._]{1,64}$/;
const IG_USER_ID_PATTERN = /^[1-9][0-9]{0,127}$/;

/**
 * Reconciles the caller's Instagram account row.
 *
 * Session cookies are never accepted here: they are captured by the Control
 * API through POST /api/extension/session, which encrypts them into
 * `ig_sessions`. This route only reads and writes non-secret account identity.
 */
export async function POST(req: Request) {
  const authHeader = req.headers.get("authorization");
  if (!authHeader?.startsWith("Bearer ")) {
    return new NextResponse("Unauthorized", { status: 401 });
  }

  const token = authHeader.slice(7);
  const ownerId = verifyExtensionTicket(token);
  if (!ownerId) return new NextResponse("Unauthorized", {status:401});
  const body = await req.json();
  const {credentials, igUserId, username: clientUsername} = body;

  if (credentials) {
    return new NextResponse(
      "Session cookies must be captured via /api/extension/session",
      { status: 400 }
    );
  }

  const username =
    typeof clientUsername === "string"
      ? clientUsername.trim().toLowerCase()
      : "";

  if (!USERNAME_PATTERN.test(username)) {
    return new NextResponse("Missing or invalid username", { status: 400 });
  }

  const normalizedIgUserId =
    typeof igUserId === "string" && IG_USER_ID_PATTERN.test(igUserId.trim())
      ? igUserId.trim()
      : null;

  try {
    const { account, created } = await connectAccount({
      userId: ownerId,
      username,
      igUserId: normalizedIgUserId,
    });
    return NextResponse.json(
      { id: account.id, username: account.username, updated: !created },
      { status: created ? 201 : 200 },
    );
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
