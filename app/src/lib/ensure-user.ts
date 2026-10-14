import { auth } from "@/lib/owner-auth";
import { getUserByOwnerKey, type UserRow } from "@/lib/queries";
export async function ensureUser(): Promise<UserRow | null> {
  const { userId } = await auth();
  return userId ? getUserByOwnerKey(userId) : null;
}
