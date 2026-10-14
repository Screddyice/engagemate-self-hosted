import { afterEach, describe, expect, it, vi } from "vitest";
import { appHost, appUrl, supportEmail } from "@/lib/site";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("deployment identity", () => {
  it("names no host or support address when unconfigured", () => {
    vi.stubEnv("NEXT_PUBLIC_APP_URL", "");
    vi.stubEnv("NEXT_PUBLIC_SUPPORT_EMAIL", "");

    expect(appUrl()).toBeNull();
    expect(appHost()).toBeNull();
    expect(supportEmail()).toBeNull();
  });

  it("uses the configured origin and address", () => {
    vi.stubEnv("NEXT_PUBLIC_APP_URL", "https://app.example.com");
    vi.stubEnv("NEXT_PUBLIC_SUPPORT_EMAIL", "help@example.com");

    expect(appUrl()?.origin).toBe("https://app.example.com");
    expect(appHost()).toBe("app.example.com");
    expect(supportEmail()).toBe("help@example.com");
  });

  it("ignores malformed values instead of guessing", () => {
    vi.stubEnv("NEXT_PUBLIC_APP_URL", "not a url");
    vi.stubEnv("NEXT_PUBLIC_SUPPORT_EMAIL", "not an address");

    expect(appUrl()).toBeNull();
    expect(supportEmail()).toBeNull();
  });
});
