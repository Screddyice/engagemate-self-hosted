import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  EXTENSION_TICKET_TTL_SECONDS,
  mintExtensionTicket,
  verifyExtensionTicket,
} from "@/lib/extension-ticket";

const USER_ID = "11111111-2222-4333-8444-555555555555";
const NOW = Date.UTC(2026, 8, 4, 0, 0, 0);
const originalEnv = { ...process.env };

describe("extension tickets", () => {
  beforeEach(() => {
    process.env = { ...originalEnv, INTERNAL_API_KEY: "test-signing-key" };
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it("round-trips a user-bound ticket for the configured lifetime", () => {
    const ticket = mintExtensionTicket(USER_ID, NOW);

    expect(ticket).toMatch(/^em_xt_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
    expect(verifyExtensionTicket(ticket, NOW)).toBe(USER_ID);
    expect(
      verifyExtensionTicket(ticket, NOW + EXTENSION_TICKET_TTL_SECONDS * 1000 - 1),
    ).toBe(USER_ID);
    expect(
      verifyExtensionTicket(ticket, NOW + EXTENSION_TICKET_TTL_SECONDS * 1000),
    ).toBeNull();
  });

  it("rejects tampering, malformed data, and a different signing key", () => {
    const ticket = mintExtensionTicket(USER_ID, NOW);
    expect(verifyExtensionTicket(`${ticket}x`, NOW)).toBeNull();
    expect(verifyExtensionTicket("em_xt_not-a-ticket", NOW)).toBeNull();

    process.env.INTERNAL_API_KEY = "different-key";
    expect(verifyExtensionTicket(ticket, NOW)).toBeNull();
  });

  it("fails closed when the signing key is unavailable", () => {
    const ticket = mintExtensionTicket(USER_ID, NOW);
    delete process.env.INTERNAL_API_KEY;
    delete process.env.PIPELINE_API_KEY;

    expect(verifyExtensionTicket(ticket, NOW)).toBeNull();
  });
});
