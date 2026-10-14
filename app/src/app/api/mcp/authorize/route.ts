import { auth } from "@/lib/owner-auth";
import { NextResponse } from "next/server";

import { getUserByOwnerKey } from "@/lib/queries";

const STATE_PATTERN = /^[A-Za-z0-9_-]{43,128}$/;

function controlBaseUrl(): string {
  return (process.env.CONTROL_API_URL || "").replace(/\/$/, "");
}

async function identity() {
  const { userId } = await auth();
  if (!userId) return { error: new NextResponse("Unauthorized", { status: 401 }) };
  const user = await getUserByOwnerKey(userId);
  if (!user) return { error: NextResponse.json({ error: "User not found" }, { status: 404 }) };
  return { user };
}

function trustedHeaders(userId: string): Record<string, string> | null {
  const apiKey = process.env.INTERNAL_API_KEY;
  return apiKey
    ? {
        "Content-Type": "application/json",
        "X-Internal-Api-Key": apiKey,
        "X-User-Id": userId,
      }
    : null;
}

function configurationError() {
  const baseUrl = controlBaseUrl();
  if (!baseUrl || !process.env.INTERNAL_API_KEY) {
    return NextResponse.json({ error: "Control API not configured" }, { status: 503 });
  }
  if (
    process.env.NODE_ENV === "production" &&
    !baseUrl.startsWith("https://") &&
    baseUrl !== "http://api:8000"
  ) {
    return NextResponse.json(
      { error: "CONTROL_API_URL must use HTTPS in production" },
      { status: 500 },
    );
  }
  return null;
}

export async function GET(request: Request) {
  const state = new URL(request.url).searchParams.get("state") || "";
  if (!STATE_PATTERN.test(state)) {
    return NextResponse.json({ error: "Invalid authorization state" }, { status: 400 });
  }
  const actor = await identity();
  if (actor.error) return actor.error;
  const configError = configurationError();
  if (configError) return configError;
  const headers = trustedHeaders(actor.user!.id)!;

  try {
    const upstream = await fetch(
      `${controlBaseUrl()}/v1/mcp/oauth/pending?state=${encodeURIComponent(state)}`,
      { headers, cache: "no-store" },
    );
    const payload = await upstream.json().catch(() => ({}));
    if (!upstream.ok) {
      return NextResponse.json({ error: "Authorization request unavailable" }, { status: upstream.status });
    }
    return NextResponse.json({
      client_name: typeof payload.client_name === "string" ? payload.client_name : "Connected agent",
      requested_scopes: Array.isArray(payload.requested_scopes)
        ? payload.requested_scopes.filter((scope: unknown) => typeof scope === "string")
        : [],
    });
  } catch {
    return NextResponse.json({ error: "Control API unreachable" }, { status: 502 });
  }
}

export async function POST(request: Request) {
  const actor = await identity();
  if (actor.error) return actor.error;
  let body: {
    state?: unknown;
    approved?: unknown;
    approved_scopes?: unknown;
  };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON" }, { status: 400 });
  }
  if (typeof body.state !== "string" || !STATE_PATTERN.test(body.state)) {
    return NextResponse.json({ error: "Invalid authorization state" }, { status: 400 });
  }
  const configError = configurationError();
  if (configError) return configError;
  const headers = trustedHeaders(actor.user!.id)!;

  try {
    const upstream = await fetch(`${controlBaseUrl()}/v1/mcp/oauth/approve`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        state: body.state,
        approved: body.approved === true,
        approved_scopes: Array.isArray(body.approved_scopes)
          ? body.approved_scopes
          : [],
      }),
      cache: "no-store",
    });
    const payload = await upstream.json().catch(() => ({}));
    if (!upstream.ok || typeof payload.continue_url !== "string") {
      return NextResponse.json({ error: "Authorization could not be completed" }, { status: upstream.status });
    }
    return NextResponse.json({ continue_url: payload.continue_url });
  } catch {
    return NextResponse.json({ error: "Control API unreachable" }, { status: 502 });
  }
}
