import { describe, it, expect } from "vitest";
import { db } from "@/db";

describe("db client", () => {
  it("is a drizzle instance with a query builder", () => {
    expect(db).toBeDefined();
    // drizzle exposes a `.select` query builder regardless of driver
    expect(db).toHaveProperty("select");
    expect(typeof (db as { select: unknown }).select).toBe("function");
  });
});
