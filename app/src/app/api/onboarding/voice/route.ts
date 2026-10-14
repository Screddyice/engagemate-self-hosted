import { planLimits } from "@/lib/plans";
import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import { db } from "@/db";
import { agentConfigs } from "@/db/schema";
import { eq, and } from "drizzle-orm";
import {
  getUserByOwnerKey,
  getAccountById,
  getUserAccounts,
  getAgentConfig,
} from "@/lib/queries";

export async function PUT(req: Request) {
  const { userId } = await auth();
  if (!userId) return new NextResponse("Unauthorized", { status: 401 });

  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });

  const { igAccountId, persona } = await req.json();

  if (typeof persona !== "string" || persona.trim() === "") {
    return NextResponse.json({ error: "persona is required" }, { status: 400 });
  }

  if (persona.length > 2000) {
    return NextResponse.json(
      { error: "persona must be at most 2000 characters" },
      { status: 400 }
    );
  }

  // Onboarding configures the account the caller just connected, so a single
  // owned account resolves implicitly; anything else must name its target.
  let account;
  if (igAccountId !== undefined) {
    if (typeof igAccountId !== "string") {
      return NextResponse.json(
        { error: "igAccountId must be a string" },
        { status: 400 }
      );
    }
    account = await getAccountById(user.id, igAccountId);
  } else {
    const accounts = await getUserAccounts(user.id);
    if (accounts.length > 1) {
      return NextResponse.json(
        { error: "igAccountId is required when several accounts are connected" },
        { status: 400 }
      );
    }
    account = accounts[0];
  }

  const trimmed = persona.trim();
  if (igAccountId !== undefined && !account) {
    return NextResponse.json({ error: "No account found" }, { status: 404 });
  }

  if (!account) {
    return NextResponse.json(
      { error: "Connect Instagram before saving a voice" },
      { status: 409 },
    );
  }

  const existing = await getAgentConfig(user.id, account.id);

  if (existing) {
    await db
      .update(agentConfigs)
      .set({ persona: trimmed, updatedAt: new Date() })
      .where(
        and(eq(agentConfigs.id, existing.id), eq(agentConfigs.userId, user.id))
      );
  } else {
    await db.insert(agentConfigs).values({
      userId: user.id,
      igAccountId: account.id,
      persona: trimmed,
      dailyLikeLimit: Math.min(20, planLimits(user.plan).dailyActions),
    });
  }

  return NextResponse.json({ igAccountId: account.id, persona: trimmed });
}
