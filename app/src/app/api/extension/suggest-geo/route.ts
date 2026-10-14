import { NextRequest, NextResponse } from "next/server";
import { isIP } from "node:net";
import { extensionUserId } from "@/lib/extension-auth";

/**
 * Login-geography suggestion for onboarding.
 *
 * Clerk-authenticated proxy → Control API POST /v1/accounts/suggest-login-geo.
 * The web tier geolocates the request's public IP, then sends only coordinates
 * and the browser's IANA time zone to the Control API. The IP is never stored
 * or forwarded. The time zone remains a fallback when IP geolocation fails.
 *
 * Carries no secrets in either direction, so unlike the session bridge it needs
 * no cookie-redaction pass.
 */

function controlBaseUrl(): string {
  const raw =
    process.env.CONTROL_API_URL || process.env.PIPELINE_API_URL || "";
  return raw.replace(/\/$/, "");
}

function controlApiKey(): string {
  return process.env.INTERNAL_API_KEY || process.env.PIPELINE_API_KEY || "";
}

const IP_GEO_URL = "https://ipwho.is";
const IP_GEO_TIMEOUT_MS = 3_000;

function requestIp(req: NextRequest): string | null {
  const forwarded = req.headers
    .get("x-forwarded-for")
    ?.split(",")
    .at(-1)
    ?.trim();
  const candidate = forwarded || req.headers.get("x-real-ip")?.trim() || "";
  return isIP(candidate) ? candidate : null;
}

async function requestCoordinates(
  req: NextRequest,
): Promise<{ latitude: number; longitude: number } | null> {
  const ip = requestIp(req);
  if (!ip) return null;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), IP_GEO_TIMEOUT_MS);
  try {
    const response = await fetch(`${IP_GEO_URL}/${encodeURIComponent(ip)}`, {
      method: "GET",
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) return null;
    const data = (await response.json()) as Record<string, unknown>;
    const latitude = data.latitude;
    const longitude = data.longitude;
    if (
      data.success !== true ||
      data.ip !== ip ||
      typeof latitude !== "number" ||
      !Number.isFinite(latitude) ||
      latitude < -90 ||
      latitude > 90 ||
      typeof longitude !== "number" ||
      !Number.isFinite(longitude) ||
      longitude < -180 ||
      longitude > 180
    ) {
      return null;
    }
    return { latitude, longitude };
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
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
  if (
    process.env.NODE_ENV === "production" &&
    !baseUrl.startsWith("https://") &&
    baseUrl !== "http://api:8000"
  ) {
    return NextResponse.json(
      { error: "CONTROL_API_URL must use HTTPS in production" },
      { status: 500 }
    );
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON" }, { status: 400 });
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return NextResponse.json({ error: "Invalid request" }, { status: 400 });
  }

  // Accept the browser time zone as a fallback. Ignore page-supplied IPs and
  // coordinates; only the reverse-proxy request can supply location evidence.
  const input = body as Record<string, unknown>;
  const hint: Record<string, unknown> = {};
  if (Object.hasOwn(input, "timezone")) hint.timezone = input.timezone;
  const coordinates = await requestCoordinates(req);
  if (coordinates) Object.assign(hint, coordinates);

  try {
    const response = await fetch(`${baseUrl}/v1/accounts/suggest-login-geo`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Internal-Api-Key": apiKey,
        "X-User-Id": userId,
      },
      body: JSON.stringify(hint),
    });
    const text = await response.text();
    if (!response.ok) {
      return NextResponse.json(
        { error: `suggestion failed (${response.status})` },
        { status: response.status }
      );
    }
    return new NextResponse(text, {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return NextResponse.json(
      { error: "Control API unreachable" },
      { status: 502 }
    );
  }
}
