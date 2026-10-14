import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import { and, asc, eq, isNotNull, isNull, sql } from "drizzle-orm";
import { db } from "@/db";
import { relationshipFollows, targets, mcpAuditEvents } from "@/db/schema";
import { getUserByOwnerKey, getAccountById } from "@/lib/queries";

type Context = { params: Promise<{ id: string }> };
async function owned(context: Context) {
  const { userId } = await auth();
  if (!userId) return { error: new NextResponse("Unauthorized", { status: 401 }) };
  const user = await getUserByOwnerKey(userId);
  if (!user) return { error: new NextResponse("Not found", { status: 404 }) };
  const { id } = await context.params;
  const account = await getAccountById(user.id, id);
  if (!account) return { error: new NextResponse("Not found", { status: 404 }) };
  return { userId: user.id, accountId: account.id, actorId: userId };
}
export async function GET(_request: Request, context: Context) {
  const scope = await owned(context);
  if (scope.error) return scope.error;
  const scoped = and(eq(relationshipFollows.userId, scope.userId!), eq(relationshipFollows.igAccountId, scope.accountId!));
  const [counts] = await db.select({
    pendingUnfollows: sql<number>`count(*) filter (where ${relationshipFollows.status} = 'following')::int`,
    uncertain: sql<number>`count(*) filter (where ${relationshipFollows.status} = 'uncertain' or exists (
      select 1 from follow_reel_messages frm
      where frm.relationship_follow_id = ${relationshipFollows.id}
        and frm.user_id = ${relationshipFollows.userId}
        and frm.ig_account_id = ${relationshipFollows.igAccountId}
        and frm.status = 'uncertain'
    ))::int`,
  }).from(relationshipFollows).where(scoped);
  const messageField = (column: "status" | "sent_at" | "reel_url") => sql<string | null>`(
    select ${sql.identifier(column)} from follow_reel_messages frm
    where frm.relationship_follow_id = ${relationshipFollows.id}
      and frm.user_id = ${relationshipFollows.userId}
      and frm.ig_account_id = ${relationshipFollows.igAccountId}
  )`;
  const records = await db.select({ id: relationshipFollows.id, username: targets.username,
    status: relationshipFollows.status, confirmedFollowAt: relationshipFollows.confirmedFollowAt,
    unfollowDueAt: relationshipFollows.unfollowDueAt,
    messageStatus: messageField("status"), messageSentAt: messageField("sent_at"), reelUrl: messageField("reel_url"),
    cancellable: sql<boolean>`${relationshipFollows.status} = 'following' and ${relationshipFollows.confirmedFollowAt} is not null and ${relationshipFollows.claimedBy} is null and ${relationshipFollows.leaseToken} is null and ${relationshipFollows.leaseExpiresAt} is null`,
  }).from(relationshipFollows).innerJoin(targets, and(eq(targets.id, relationshipFollows.targetId), eq(targets.userId, relationshipFollows.userId), eq(targets.igAccountId, relationshipFollows.igAccountId)))
    .where(and(scoped, sql`(${relationshipFollows.status} in ('following', 'unfollow_running', 'uncertain', 'cancelled') or ${messageField("status")} is not null)`))
    .orderBy(sql`case when ${relationshipFollows.status} = 'cancelled' then 1 else 0 end`, asc(relationshipFollows.unfollowDueAt), asc(relationshipFollows.createdAt)).limit(100);
  return NextResponse.json({ igAccountId: scope.accountId, ...counts, records }, { headers: { "Cache-Control": "private, no-store" } });
}
export async function POST(request: Request, context: Context) {
  const scope = await owned(context);
  if (scope.error) return scope.error;
  let body;
  try { body = await request.json(); } catch { return NextResponse.json({ error: "Invalid cancellation" }, { status: 400 }); }
  if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).length !== 1 || typeof body.id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(body.id)) return NextResponse.json({ error: "Invalid cancellation" }, { status: 400 });
  const cancelled = await db.transaction(async (tx) => {
    const [row] = await tx.update(relationshipFollows).set({ status: "cancelled", nextAttemptAt: null, lastError: "cancelled_by_user", updatedAt: sql`now()` })
      .where(and(eq(relationshipFollows.id, body.id), eq(relationshipFollows.userId, scope.userId!), eq(relationshipFollows.igAccountId, scope.accountId!), eq(relationshipFollows.status, "following"), isNotNull(relationshipFollows.confirmedFollowAt), isNull(relationshipFollows.claimedBy), isNull(relationshipFollows.leaseToken), isNull(relationshipFollows.leaseExpiresAt))).returning({ id: relationshipFollows.id });
    if (!row) return false;
    await tx.insert(mcpAuditEvents).values({ userId: scope.userId!, igAccountId: scope.accountId!, toolName: "web.follow.cancel_unfollow", outcome: "updated", beforeState: { id: row.id, status: "following" }, afterState: { id: row.id, status: "cancelled" }, safeMetadata: { actorType: "web", actorId: scope.actorId } });
    return true;
  });
  return cancelled ? NextResponse.json({ success: true }) : NextResponse.json({ error: "This unfollow is unavailable or already in progress. Refresh its status." }, { status: 409 });
}
