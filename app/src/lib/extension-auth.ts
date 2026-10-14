import { auth } from "@/lib/owner-auth";
import type { NextRequest } from "next/server";
import { getUserByOwnerKey } from "@/lib/queries";
import { verifyExtensionTicket } from "@/lib/extension-ticket";

export async function extensionUserId(
  req: NextRequest,
): Promise<string | null | undefined> {
  const authorization = req.headers.get("authorization");
  if (authorization) {
    const match = /^Bearer (em_xt_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$/.exec(
      authorization,
    );
    return match ? verifyExtensionTicket(match[1]) : null;
  }

  const { userId } = await auth();
  if (!userId) return null;
  const user = await getUserByOwnerKey(userId);
  return user?.id;
}
