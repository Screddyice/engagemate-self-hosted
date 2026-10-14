import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";
import { getTableConfig } from "drizzle-orm/pg-core";

import { hermesToolCalls } from "@/db/schema";

const migration = readFileSync(
  resolve(
    process.cwd(),
    "src/db/migrations/0017_hermes_tool_call_attempt.sql",
  ),
  "utf8",
).toLowerCase();

describe("hermes tool call attempt migration", () => {
  it("records what a call attempted, not only that it happened", () => {
    // A canary denial is the only record of an attempt the worker never got
    // to make. Approving it requires naming the exact target, Story, and
    // text, so those have to survive the denial.
    for (const column of [
      "target_id",
      "story_fingerprint",
      "generated_text_hash",
      "proposed_text",
    ]) {
      expect(migration).toContain(`add column "${column}"`);
    }
  });

  it("adds the columns without demanding a value from existing rows", () => {
    // hermes_tool_calls is an append-only audit table on a live deployment.
    // A NOT NULL or a DEFAULT would rewrite it; discover and complete_run
    // legitimately have nothing to record.
    expect(migration).not.toContain("not null");
    expect(migration).not.toContain("default");
  });

  it("constrains both hashes to hex digests", () => {
    expect(migration).toContain("hermes_tool_calls_fingerprint_check");
    expect(migration).toContain("hermes_tool_calls_text_hash_check");
    expect(migration).toContain("[0-9a-f]{64}");
  });

  it("keeps the new columns nullable in the schema", () => {
    const columns = Object.fromEntries(
      getTableConfig(hermesToolCalls).columns.map((column) => [
        column.name,
        column,
      ]),
    );
    for (const name of [
      "target_id",
      "story_fingerprint",
      "generated_text_hash",
      "proposed_text",
    ]) {
      expect(columns[name]).toBeDefined();
      expect(columns[name].notNull).toBe(false);
    }
  });
});
