import { timingSafeEqual } from "node:crypto";
import { NextResponse } from "next/server";
import { OWNER_COOKIE, ownerSessionValue } from "@/lib/owner-session";
export async function POST(req: Request) {
  const expected = process.env.OWNER_ACCESS_TOKEN || "";
  const body = await req.json().catch(() => null);
  const supplied = typeof body?.token === "string" ? body.token : "";
  const a = Buffer.from(expected), b = Buffer.from(supplied);
  if (a.length < 32 || a.length !== b.length || !timingSafeEqual(a,b)) return NextResponse.json({error: "Invalid setup key"}, {status: 401});
  const value = await ownerSessionValue();
  if (!value) return NextResponse.json({error: "Setup unavailable"}, {status: 503});
  const response = NextResponse.json({ok:true}, {headers:{"Cache-Control":"no-store"}});
  response.cookies.set(OWNER_COOKIE, value, {httpOnly:true, sameSite:"strict", secure: new URL(process.env.NEXT_PUBLIC_APP_URL || "http://localhost:3030").protocol === "https:", path:"/", maxAge:30*86400});
  return response;
}
