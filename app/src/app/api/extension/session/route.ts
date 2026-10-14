import { NextRequest, NextResponse } from "next/server";
import { extensionUserId } from "@/lib/extension-auth";

/**
 * Extension session bridge.
 *
 * POST: Clerk-authenticated proxy → Control API POST /v1/accounts/session.
 * GET:  Confirms the browser has a Clerk session (popup status support).
 *
 * Env (prefer these names):
 *   CONTROL_API_URL   — HTTPS Control API base, or http://api:8000 in Compose
 *   INTERNAL_API_KEY  — trusted web-tier key for X-Internal-Api-Key
 *
 * Fallbacks (last resort only): PIPELINE_API_URL / PIPELINE_API_KEY
 *
 * Never log cookie values. Never echo cookies in error responses.
 */

type SessionCookie = {
  name?: unknown;
  value?: unknown;
};

function controlBaseUrl(): string {
  const raw =
    process.env.CONTROL_API_URL || process.env.PIPELINE_API_URL || "";
  return raw.replace(/\/$/, "");
}

function controlApiKey(): string {
  return process.env.INTERNAL_API_KEY || process.env.PIPELINE_API_KEY || "";
}

function assertProductionHttps(baseUrl: string): string | null {
  const privateComposeApi = baseUrl === "http://api:8000";
  if (
    process.env.NODE_ENV === "production" &&
    !baseUrl.startsWith("https://") &&
    !privateComposeApi
  ) {
    return "CONTROL_API_URL must use HTTPS in production";
  }
  return null;
}

function collectSecrets(body: unknown): string[] {
  const secrets: string[] = [];
  if (!body || typeof body !== "object") return secrets;
  const cookies = (body as { cookies?: SessionCookie[] }).cookies;
  if (!Array.isArray(cookies)) return secrets;
  for (const cookie of cookies) {
    if (typeof cookie?.value === "string" && cookie.value.length > 0) {
      secrets.push(cookie.value);
    }
  }
  return secrets;
}

function containsSecret(text: string, secrets: string[]): boolean {
  return secrets.some((s) => s.length >= 4 && text.includes(s));
}

function redactedAccount(data: Record<string, unknown>) {
  return {
    id: data.id,
    username: data.username,
    health: data.health,
    captured_at: data.captured_at,
    proxy_bound: data.proxy_bound,
  };
}

function safeErrorBody(
  status: number,
  upstreamText: string,
  secrets: string[]
): { error: string } {
  // Prefer generic messages; only surface a short detail when it is
  // non-secret and clearly a validation message.
  let detail: string | null = null;
  try {
    const parsed = JSON.parse(upstreamText) as { detail?: unknown };
    if (typeof parsed.detail === "string") {
      const d = parsed.detail.trim();
      if (
        d.length > 0 &&
        d.length <= 200 &&
        !containsSecret(d, secrets) &&
        !/cookie|sessionid|password|token|secret/i.test(d)
      ) {
        detail = d;
      }
    }
  } catch {
    // ignore non-JSON upstream bodies
  }

  if (detail) return { error: detail };

  if (status === 401 || status === 403) return { error: "Unauthorized" };
  if (status === 409) return { error: "Account conflict" };
  if (status === 422) return { error: "Invalid session payload" };
  return { error: "Session capture failed" };
}

// GET — popup uses this to verify extension or Clerk auth. Account status (username,
// health, last sync) is stored by the extension from redacted POST
// responses only — never from multi-platform org tables (removed).
export async function GET(req: NextRequest) {
  const userId = await extensionUserId(req);
  if (userId === null) return new NextResponse("Unauthorized", { status: 401 });

  return NextResponse.json({
    authenticated: true,
    connected: false,
    account: null,
  });
}

export async function POST(req: NextRequest) {
  const userId = await extensionUserId(req);
  if (!userId) {
    return userId === undefined
      ? NextResponse.json({ error: "User not found" }, { status: 404 })
      : new NextResponse("Unauthorized", { status: 401 });
  }

  const baseUrl = controlBaseUrl();
  const apiKey = controlApiKey();
  if (!baseUrl || !apiKey) {
    return NextResponse.json(
      { error: "Control API not configured" },
      { status: 503 }
    );
  }

  const httpsError = assertProductionHttps(baseUrl);
  if (httpsError) {
    return NextResponse.json({ error: httpsError }, { status: 500 });
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON" }, { status: 400 });
  }

  const secrets = collectSecrets(body);
  // Intentionally do not log request body (contains Instagram cookies).

  try {
    const response = await fetch(`${baseUrl}/v1/accounts/session`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Internal-Api-Key": apiKey,
        "X-User-Id": userId,
      },
      body: JSON.stringify(body),
    });

    const upstreamText = await response.text();

    if (!response.ok) {
      // Never echo upstream bodies that may include cookie material.
      return NextResponse.json(
        safeErrorBody(response.status, upstreamText, secrets),
        { status: response.status }
      );
    }

    let data: Record<string, unknown>;
    try {
      data = JSON.parse(upstreamText) as Record<string, unknown>;
    } catch {
      return NextResponse.json(
        { error: "Invalid Control API response" },
        { status: 502 }
      );
    }

    const redacted = redactedAccount(data);
    // Defense in depth: refuse to return a body that still holds a secret.
    if (containsSecret(JSON.stringify(redacted), secrets)) {
      return NextResponse.json(
        { error: "Session capture failed" },
        { status: 502 }
      );
    }

    return NextResponse.json(redacted, { status: response.status });
  } catch {
    // Do not surface Error.message — it can embed request material.
    return NextResponse.json(
      { error: "Failed to reach Control API" },
      { status: 502 }
    );
  }
}
