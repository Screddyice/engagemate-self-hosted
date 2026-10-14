import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import { getUserByOwnerKey } from "@/lib/queries";
import { getNotificationRecipients, parseRecipientList, saveNotificationRecipients } from "@/lib/notification-recipients";

export async function GET() {
  const { userId } = await auth();
  if (!userId) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const user = await getUserByOwnerKey(userId);
  if (!user) return NextResponse.json({ error: "User not found" }, { status: 404 });
  return NextResponse.json({ emails: await getNotificationRecipients(user.id) });
}

export async function PUT(request: Request) {
  const { userId } = await auth();
  if (!userId) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const user = await getUserByOwnerKey(userId);
  if (!user) return NextResponse.json({ error: "User not found" }, { status: 404 });
  let body;
  try { body = await request.json(); } catch {
    return NextResponse.json({ error: "Invalid JSON" }, { status: 400 });
  }
  const parsed = parseRecipientList(body?.emails);
  if (!parsed) return NextResponse.json({ error: "Enter up to five valid email addresses." }, { status: 400 });
  const emails = parsed.filter(email => email !== user.email?.trim().toLowerCase());
  await saveNotificationRecipients(user.id, emails);
  return NextResponse.json({ emails });
}
