import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import { getUserByOwnerKey } from "@/lib/queries";
import {
  EXTENSION_TICKET_TTL_SECONDS,
  mintExtensionTicket,
} from "@/lib/extension-ticket";

export async function POST() {
  const { userId } = await auth();
  if (!userId) return new NextResponse("Unauthorized", { status: 401 });
  const user = await getUserByOwnerKey(userId);
  if (!user) return NextResponse.json({ error: "User not found" }, { status: 404 });

  try {
    return NextResponse.json(
      {
        ticket: mintExtensionTicket(user.id),
        expires_in: EXTENSION_TICKET_TTL_SECONDS,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return NextResponse.json(
      { error: "Extension connection is not configured" },
      { status: 503 },
    );
  }
}
