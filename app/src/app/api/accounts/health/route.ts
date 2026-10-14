import { NextResponse } from "next/server";
import { db } from "@/db";
import { igAccounts, accountHealthEvents } from "@/db/schema";
import { eq, and, inArray, sql } from "drizzle-orm";
import { auth } from "@/lib/owner-auth";
import { getUserByOwnerKey, getAccountById } from "@/lib/queries";

// A healthy report only refreshes an account that is still runnable, and an
// unhealthy one only halts an account that is not already in a harder state.
// Clearing `challenged` or `banned` is a control-plane action, never a client
// report.
const RUNNABLE_STATUSES = ["active", "degraded"];
const PAUSABLE_STATUSES = ["active", "degraded", "paused"];

// Extension reports Instagram session health for one owned account
export async function POST(req: Request) {
  const { userId } = await auth();
  if (!userId) return new NextResponse("Unauthorized", { status: 401 });

  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });

  const { igAccountId, isHealthy } = await req.json();

  if (typeof igAccountId !== "string" || typeof isHealthy !== "boolean") {
    return NextResponse.json(
      { error: "Missing igAccountId or isHealthy" },
      { status: 400 }
    );
  }

  const account = await getAccountById(user.id, igAccountId);

  if (!account) {
    return NextResponse.json({ error: "No account found" }, { status: 404 });
  }

  // The extension polls while a sign-in is required. Once that exact pause is
  // recorded, the same report is a duplicate and must not create another
  // health event or another reconnect alert.
  if (!isHealthy && account.healthStatus === "paused" && account.pauseReason === "login_required") {
    return NextResponse.json({
      accountId: account.id,
      username: account.username,
      healthStatus: account.healthStatus,
    });
  }

  const now = new Date();

  if (isHealthy) {
    const [updated] = await db
      .update(igAccounts)
      .set({ healthStatus: "active", lastHealthyAt: now, updatedAt: now })
      .where(
        and(
          eq(igAccounts.id, account.id),
          eq(igAccounts.userId, user.id),
          inArray(igAccounts.healthStatus, RUNNABLE_STATUSES)
        )
      )
      .returning({ healthStatus: igAccounts.healthStatus });

    return NextResponse.json({
      accountId: account.id,
      username: account.username,
      healthStatus: updated?.healthStatus ?? account.healthStatus,
    });
  }

  const [updated] = await db
    .update(igAccounts)
    .set({
      healthStatus: "paused",
      pausedAt: sql`coalesce(${igAccounts.pausedAt}, ${now})`,
      pauseReason: "login_required",
      updatedAt: now,
    })
    .where(
      and(
        eq(igAccounts.id, account.id),
        eq(igAccounts.userId, user.id),
        inArray(igAccounts.healthStatus, PAUSABLE_STATUSES)
      )
    )
    .returning({ healthStatus: igAccounts.healthStatus });

  if (updated) {
    await db.insert(accountHealthEvents).values({
      userId: user.id,
      igAccountId: account.id,
      eventType: "login_required",
      severity: "warning",
      evidence: { source: "extension" },
    });
  }

  return NextResponse.json({
    accountId: account.id,
    username: account.username,
    healthStatus: updated?.healthStatus ?? account.healthStatus,
  });
}
