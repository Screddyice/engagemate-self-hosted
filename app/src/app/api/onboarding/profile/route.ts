import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import { db } from "@/db";
import { users } from "@/db/schema";
import { eq } from "drizzle-orm";
import { getUserByOwnerKey } from "@/lib/queries";

export async function PUT(req: Request) {
  const { userId } = await auth();
  if (!userId) return new NextResponse("Unauthorized", { status: 401 });

  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });

  const { name } = await req.json();

  if (typeof name !== "string" || name.trim() === "") {
    return new NextResponse("Name required", { status: 400 });
  }

  const trimmed = name.trim();

  if (trimmed.length > 120) {
    return new NextResponse("Name must be at most 120 characters", {
      status: 400,
    });
  }

  const [updated] = await db
    .update(users)
    .set({ name: trimmed, updatedAt: new Date() })
    .where(eq(users.id, user.id))
    .returning({ id: users.id, name: users.name });

  return NextResponse.json(updated);
}
