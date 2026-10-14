import { db } from "@/db";
import { users } from "@/db/schema";
export async function provisionOwner(ownerKey: string) {
  const [user] = await db.insert(users).values({ ownerKey,
    email: process.env.OWNER_EMAIL || "owner@localhost.invalid", name: "Owner", plan: "self_hosted",
  }).onConflictDoUpdate({target: users.ownerKey, set: {updatedAt: new Date()}}).returning();
  return user;
}
