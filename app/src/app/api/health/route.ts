import { NextResponse } from "next/server";

// Liveness probe for the container — intentionally no auth and no DB access.
export const dynamic = "force-dynamic";

export function GET() {
  return NextResponse.json({ status: "ok", service: "engagemate-web" });
}
