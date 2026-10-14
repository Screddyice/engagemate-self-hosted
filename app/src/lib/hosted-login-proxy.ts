import { auth } from "@/lib/owner-auth";
import { cookies } from "next/headers";
import { getUserByOwnerKey } from "@/lib/queries";
import { SELECTED_ACCOUNT_COOKIE } from "@/lib/selected-account";
import { hostedLoginEnabled } from "./hosted-login-config";

export const PRIVATE_HEADERS = {
  "Cache-Control": "no-store, max-age=0",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};
export const SAFE_CODES = new Set([
  "sign_in_required",
  "session_unverified",
  "identity_check_unavailable",
  "identity_mismatch",
  "session_expired",
  "session_not_found",
  "session_busy",
  "capacity",
  "capacity_unavailable",
  "proxy_unavailable",
  "account_limit",
  "account_conflict",
  "auth_required",
  "connection_unavailable",
  "invalid_input",
  "feature_disabled",
  "forbidden",
  "connection_capacity",
  "connection_in_progress",
  "connection_not_found",
  "connection_cancelled",
  "connection_closed",
  "connection_lost",
  "slow_down",
  "instagram_rate_limited",
  "identity_unavailable",
  "account_browser_busy",
  "geography_unavailable",
  "session_unavailable",
  "account_unavailable",
  "invalid_username",
]);
export function safeCode(value: unknown): string {
  return typeof value === "string" && SAFE_CODES.has(value)
    ? value
    : "connection_unavailable";
}
export function loginError(code: string, status: number): Response {
  return Response.json(
    { code: safeCode(code) },
    { status, headers: PRIVATE_HEADERS },
  );
}
export function validSessionId(id: string): boolean {
  return /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(
    id,
  );
}
export async function loginUser(
  req: Request,
  write = false,
): Promise<string | Response> {
  const { userId } = await auth();
  if (!userId) return loginError("auth_required", 401);
  if (!hostedLoginEnabled()) return loginError("feature_disabled", 404);
  if (write) {
    let origin: string;
    try {
      origin = new URL(process.env.NEXT_PUBLIC_APP_URL || req.url).origin;
    } catch {
      return loginError("connection_unavailable", 503);
    }
    if (
      req.headers.get("origin") !== origin ||
      req.headers.get("sec-fetch-site") === "cross-site"
    ) {
      return loginError("forbidden", 403);
    }
  }
  const user = await getUserByOwnerKey(userId);
  return user?.id || loginError("auth_required", 401);
}
/** Limit bytes while reading, including chunked requests that omit Content-Length. */
export async function limitedBytes(
  stream: ReadableStream<Uint8Array> | null,
  max: number,
): Promise<Uint8Array> {
  if (!stream) return new Uint8Array();
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > max) {
        await reader.cancel();
        throw new Error("size");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return bytes;
}
export async function loginBody(
  req: Request,
): Promise<Record<string, unknown> | null> {
  if (
    !req.headers
      .get("content-type")
      ?.toLowerCase()
      .startsWith("application/json")
  )
    return null;
  try {
    const body: unknown = JSON.parse(
      new TextDecoder().decode(await limitedBytes(req.body, 8192)),
    );
    return body && typeof body === "object" && !Array.isArray(body)
      ? (body as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}
const ACTIONS = new Set([
  "send_code", "continue_verification", "resend_code", "try_another_way", "use_sms", "use_whatsapp",
  "use_authenticator", "use_email", "use_backup_code", "confirm_login",
]);
export function cleanInput(
  body: Record<string, unknown> | null,
): Record<string, unknown> | null {
  if (!body) return null;
  if (
    body.type === "sign_in" &&
    typeof body.username === "string" &&
    /^[a-z0-9._]{1,30}$/.test(body.username) &&
    typeof body.password === "string" &&
    body.password.length > 0 &&
    body.password.length <= 1024 &&
    !/[\x00-\x1f\x7f-\x9f]/.test(body.password)
  )
    return {
      type: "sign_in",
      username: body.username,
      password: body.password,
    };
  if (body.type === "submit_code" && typeof body.code === "string" &&
    /^[A-Za-z0-9 -]{4,20}$/.test(body.code))
    return { type: "submit_code", code: body.code };
  if (
    body.type === "choose_action" &&
    typeof body.action === "string" &&
    ACTIONS.has(body.action)
  )
    return { type: "choose_action", action: body.action };
  return null;
}
export function cleanStep(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object") return null;
  const step = value as Record<string, unknown>;
  if (!["login", "code", "method", "approval", "ready", "unsupported"].includes(String(step.kind)) ||
    !Array.isArray(step.options) || step.options.length > ACTIONS.size ||
    step.options.some((option) => typeof option !== "string" || !ACTIONS.has(option)))
    return null;
  const result: Record<string, unknown> = { kind: step.kind, options: Array.from(new Set(step.options)) };
  if (step.kind === "code") {
    if (!["unknown", "authenticator", "sms", "whatsapp", "email", "backup"].includes(String(step.channel)) ||
      typeof step.invalid !== "boolean") return null;
    result.channel = step.channel;
    result.invalid = step.invalid;
  }
  return result;
}
export function cleanStatus(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (
    typeof v.id !== "string" ||
    !validSessionId(v.id) ||
    !["active", "connected", "error"].includes(String(v.state)) ||
    typeof v.expires_at !== "string" ||
    !Number.isFinite(Date.parse(v.expires_at))
  )
    return null;
  const result: Record<string, unknown> = {
    id: v.id,
    state: v.state,
    expires_at: new Date(v.expires_at).toISOString(),
    width: 1024,
    height: 768,
  };
  if (typeof v.username === "string" && /^[a-z0-9._]{1,30}$/.test(v.username))
    result.username = v.username;
  if (v.state === "error") result.code = safeCode(v.code);
  if (v.state === "connected") {
    const a = v.account as Record<string, unknown> | undefined;
    if (
      !a ||
      typeof a.id !== "string" ||
      !validSessionId(a.id) ||
      typeof a.username !== "string" ||
      !/^[A-Za-z0-9_.]{1,30}$/.test(a.username) ||
      !["active", "paused", "challenged", "degraded", "banned"].includes(
        String(a.health),
      ) ||
      a.proxy_bound !== true
    )
      return null;
    result.account = { id: a.id, username: a.username, healthStatus: a.health };
  }
  return result;
}
/** No caller-controlled origin, forwarded cookies, response text, or diagnostics. */
export async function requestLogin(
  userId: string,
  path: string,
  method = "GET",
  body?: Record<string, unknown>,
): Promise<Response> {
  const key = process.env.INTERNAL_API_KEY;
  if (!key) return loginError("connection_unavailable", 503);
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    path === "/v1/connect"
      ? 90_000
      : path.endsWith("/complete")
        ? 45_000
        : 15_000,
  );
  try {
    const response = await fetch(
      `${(process.env.LOGIN_SERVICE_URL || "http://login:8000").replace(/\/$/, "")}${path}`,
      {
        method,
        redirect: "error",
        cache: "no-store",
        signal: controller.signal,
        headers: {
          "X-Internal-Api-Key": key,
          "X-User-Id": userId,
          ...(body ? { "Content-Type": "application/json" } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
      },
    );
    if (response.status === 204 && response.ok)
      return new Response(null, { status: 204, headers: PRIVATE_HEADERS });
    const payload: unknown = JSON.parse(
      new TextDecoder().decode(await limitedBytes(response.body, 16_384)),
    );
    if (!response.ok) {
      const value = payload as {
        code?: unknown;
        detail?: { code?: unknown };
      } | null;
      return loginError(
        safeCode(value?.code ?? value?.detail?.code),
        [400, 401, 402, 403, 404, 409, 410, 422, 429, 503].includes(
          response.status,
        )
          ? response.status
          : 502,
      );
    }
    if (path.endsWith("/input"))
      return Response.json({ ok: true }, { headers: PRIVATE_HEADERS });
    if (path.endsWith("/step")) {
      const step = cleanStep(payload);
      return step ? Response.json(step, { headers: PRIVATE_HEADERS }) : loginError("connection_unavailable", 502);
    }
    const status = cleanStatus(payload);
    if (!status) return loginError("connection_unavailable", 502);
    if (status.state === "connected") {
      (await cookies()).set(
        SELECTED_ACCOUNT_COOKIE,
        (status.account as { id: string }).id,
        {
          httpOnly: true,
          secure: process.env.NODE_ENV === "production",
          sameSite: "lax",
          path: "/",
          maxAge: 60 * 60 * 24 * 365,
        },
      );
    }
    return Response.json(status, {
      status: response.status,
      headers: PRIVATE_HEADERS,
    });
  } catch {
    return loginError("connection_unavailable", 503);
  } finally {
    clearTimeout(timeout);
  }
}
export async function sessionRequest(
  req: Request,
  id: string,
  suffix = "",
  method = "GET",
): Promise<Response> {
  const user = await loginUser(req, method !== "GET");
  if (user instanceof Response) return user;
  if (!validSessionId(id)) return loginError("invalid_input", 400);
  let body: Record<string, unknown> | undefined;
  if (suffix === "/input") {
    const input = cleanInput(await loginBody(req));
    if (!input) return loginError("invalid_input", 400);
    body = input;
  }
  return requestLogin(user, `/v1/connect/${id}${suffix}`, method, body);
}
