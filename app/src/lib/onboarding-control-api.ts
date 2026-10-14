import type { OnboardingBrandProfile } from "@/lib/onboarding-profile";

export type OnboardingControlError = {
  error: string;
  code?: string;
};

export type OnboardingControlResult<T> = {
  status: number;
  body: T | OnboardingControlError;
  headers?: HeadersInit;
};

const PROFILE_TIMEOUT_MS = 10_000;
const ANALYZE_TIMEOUT_MS = 120_000;
const MUTATION_TIMEOUT_MS = 30_000;

function controlTimeoutMs(path: string): number {
  if (path.startsWith("/v1/onboarding/brand/profile")) {
    return PROFILE_TIMEOUT_MS;
  }
  if (path === "/v1/onboarding/brand/analyze" || path === "/v1/onboarding/profiles") {
    return ANALYZE_TIMEOUT_MS;
  }
  return MUTATION_TIMEOUT_MS;
}

function boundedSignal(
  callerSignal: AbortSignal | null | undefined,
  timeoutMs: number,
): { signal: AbortSignal; cleanup: () => void } {
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  }
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return {
    signal: controller.signal,
    cleanup: () => {
      clearTimeout(timer);
      callerSignal?.removeEventListener("abort", abortFromCaller);
    },
  };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function errorBody(value: unknown, fallback: string): OnboardingControlError {
  const record = asRecord(value);
  const detail = asRecord(record?.detail);
  const validation = Array.isArray(record?.detail)
    ? record.detail
        .map((item) => {
          const issue = asRecord(item);
          if (!issue || typeof issue.msg !== "string") return null;
          const location = Array.isArray(issue.loc)
            ? issue.loc.filter((part) => part !== "body").join(".")
            : "";
          return location ? `${location}: ${issue.msg}` : issue.msg;
        })
        .filter((item): item is string => Boolean(item))
    : [];
  const message =
    (typeof record?.error === "string" && record.error) ||
    (typeof record?.detail === "string" && record.detail) ||
    (typeof detail?.message === "string" && detail.message) ||
    (validation.length > 0 && validation.join("; ")) ||
    fallback;
  const code =
    (typeof record?.code === "string" && record.code) ||
    (typeof detail?.code === "string" && detail.code) ||
    (validation.length > 0 ? "validation_error" : undefined) ||
    undefined;
  return code ? { error: message, code } : { error: message };
}

export async function requestOnboardingControl<T>(
  path: string,
  userId: string,
  init: RequestInit = {},
): Promise<OnboardingControlResult<T>> {
  const baseUrl = (process.env.CONTROL_API_URL || "").replace(/\/$/, "");
  const apiKey = process.env.INTERNAL_API_KEY || "";
  if (!baseUrl || !apiKey) {
    return {
      status: 503,
      body: { error: "Onboarding service is not configured" },
    };
  }

  const deadline = boundedSignal(init.signal, controlTimeoutMs(path));
  try {
    const headers = new Headers(init.headers);
    if (init.body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    headers.set("X-Internal-Api-Key", apiKey);
    headers.set("X-User-Id", userId);
    const response = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers,
      cache: "no-store",
      signal: deadline.signal,
    });

    const retryAfter = response.headers.get("retry-after")?.trim();
    const responseHeaders = retryAfter ? { "Retry-After": retryAfter } : undefined;

    let payload: unknown;
    try {
      payload = JSON.parse(await response.text()) as unknown;
    } catch {
      return {
        status: 502,
        body: { error: "Onboarding service returned an invalid response" },
        headers: responseHeaders,
      };
    }
    if (!asRecord(payload)) {
      return {
        status: 502,
        body: { error: "Onboarding service returned an invalid response" },
        headers: responseHeaders,
      };
    }

    if (!response.ok) {
      return {
        status: response.status,
        body: errorBody(payload, "Onboarding request failed"),
        headers: responseHeaders,
      };
    }
    return {
      status: response.status,
      body: payload as T,
      headers: responseHeaders,
    };
  } catch {
    return {
      status: 502,
      body: { error: "Onboarding service is unavailable" },
    };
  } finally {
    deadline.cleanup();
  }
}

export async function getOnboardingBrandProfile(
  userId: string,
  igAccountId: string,
): Promise<OnboardingBrandProfile | null> {
  const query = new URLSearchParams({ ig_account_id: igAccountId });
  const result = await requestOnboardingControl<OnboardingBrandProfile>(
    `/v1/onboarding/brand/profile?${query}`,
    userId,
  );
  if (result.status === 404) return null;
  if (result.status !== 200 || "error" in result.body) {
    throw new Error(
      "error" in result.body
        ? result.body.error
        : "Onboarding profile could not be loaded",
    );
  }
  return result.body;
}
