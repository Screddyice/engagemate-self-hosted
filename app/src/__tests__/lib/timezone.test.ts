import { describe, expect, it, vi } from "vitest";
import { isValidIanaTimezone } from "@/lib/timezone";

describe("isValidIanaTimezone", () => {
  it("accepts bounded IANA timezone names", () => {
    expect(isValidIanaTimezone("UTC")).toBe(true);
    expect(isValidIanaTimezone("Asia/Makassar")).toBe(true);
  });

  it("rejects invalid names", () => {
    expect(isValidIanaTimezone("")).toBe(false);
    expect(isValidIanaTimezone("Mars/Olympus_Mons")).toBe(false);
  });

  it("rejects overlong input before constructing an Intl formatter", () => {
    const formatter = vi.spyOn(Intl, "DateTimeFormat");

    expect(isValidIanaTimezone("A".repeat(65))).toBe(false);
    expect(formatter).not.toHaveBeenCalled();
  });
});
