import { cookies } from "next/headers";
import { OWNER_COOKIE, validOwnerSession } from "@/lib/owner-session";
export async function auth(): Promise<{userId: string | null}> {
  return { userId: await validOwnerSession((await cookies()).get(OWNER_COOKIE)?.value) ? "local-owner" : null };
}
