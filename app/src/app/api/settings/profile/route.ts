import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";
import { getUserByOwnerKey } from "@/lib/queries";

export async function GET() {
  const { userId } = await auth();
  if (!userId) return new NextResponse("Unauthorized", { status: 401 });

  const user = await getUserByOwnerKey(userId);
  if (!user) return new NextResponse("User not found", { status: 404 });

  // Filter out sensitive fields
  /* eslint-disable @typescript-eslint/no-unused-vars */
  const {id, email, name, plan} = user;
  const safeUser = {id, email, name, plan};
  /* eslint-enable @typescript-eslint/no-unused-vars */
  return NextResponse.json(safeUser);
}
