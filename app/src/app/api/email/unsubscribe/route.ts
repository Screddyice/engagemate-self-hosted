import { NextResponse } from "next/server";
import { verifyUnsubscribe } from "@/lib/email-unsubscribe";
import { unsubscribeUserFromCategory } from "@/lib/queries";

/**
 * Turns off one email category from a signed footer link. The link opens
 * /unsubscribe, and the change happens only on this POST, so a mail scanner
 * that follows the link cannot unsubscribe anyone. No session is needed: the
 * HMAC proves the request came from an email EngageMate sent.
 */
export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON" }, { status: 400 });
  }
  const input =
    body && typeof body === "object" && !Array.isArray(body)
      ? (body as Record<string, unknown>)
      : {};
  const verified = verifyUnsubscribe(input.u, input.c, input.t);
  if (!verified) {
    return NextResponse.json(
      { error: "Invalid unsubscribe link" },
      { status: 400 },
    );
  }
  // A signed link for a deleted account has nothing left to turn off.
  await unsubscribeUserFromCategory(verified.userId, verified.category);
  return NextResponse.json({ unsubscribed: verified.category });
}
