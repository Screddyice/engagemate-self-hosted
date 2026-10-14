import { eq, sql } from "drizzle-orm";
import { db } from "@/db";
import { notificationRecipients } from "@/db/schema";

export function parseRecipientList(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length > 5) return null;
  const result: string[] = [];
  for (const raw of value) {
    if (typeof raw !== "string") return null;
    const email = raw.trim().toLowerCase();
    const parts = email.split("@");
    const local = parts[0];
    const domain = parts[1];
    if (parts.length !== 2 || email.length > 254 || !local || local.length > 64 ||
        !/^[a-z0-9!#$%&'*+\/=?^_`{|}~.-]+$/.test(local) ||
        local.startsWith(".") || local.endsWith(".") || local.includes("..") ||
        !domain || !domain.includes(".") || !domain.split(".").every(label =>
          /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) return null;
    if (!result.includes(email)) result.push(email);
  }
  return result;
}

export async function getNotificationRecipients(userId: string) {
  return db.select({ email: notificationRecipients.email }).from(notificationRecipients)
    .where(eq(notificationRecipients.userId, userId)).orderBy(notificationRecipients.email)
    .then(rows => rows.map(row => row.email));
}

export async function saveNotificationRecipients(userId: string, emails: string[]) {
  await db.transaction(async tx => {
    await tx.execute(sql`SELECT id FROM users WHERE id=${userId} FOR UPDATE`);
    await tx.delete(notificationRecipients).where(eq(notificationRecipients.userId, userId));
    if (emails.length) await tx.insert(notificationRecipients).values(emails.map(email => ({ userId, email })));
  });
  return emails;
}
