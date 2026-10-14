import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";
import { getTableConfig } from "drizzle-orm/pg-core";

import {
  hermesExecutionRuns,
  hermesToolCalls,
  instagramExecutionControls,
  liveActionApprovals,
} from "@/db/schema";

describe("Hermes execution schema", () => {
  it("records runs, idempotent tool calls, approvals, and scoped controls", () => {
    const sql = readFileSync(
      resolve(process.cwd(), "src/db/migrations/0014_hermes_execution.sql"),
      "utf8",
    ).toLowerCase();

    for (const name of [
      "hermes_execution_runs",
      "hermes_tool_calls",
      "live_action_approvals",
      "instagram_execution_controls",
    ]) {
      expect(sql).toContain(`create table "${name}"`);
    }
    expect(sql).toContain('unique ("run_id", "idempotency_key")');

    expect(getTableConfig(hermesExecutionRuns).name).toBe(
      "hermes_execution_runs",
    );
    expect(getTableConfig(hermesToolCalls).uniqueConstraints).toHaveLength(1);
    expect(getTableConfig(liveActionApprovals).indexes.map((item) => item.config.name)).toContain(
      "live_action_approvals_unconsumed_expiry_idx",
    );
    expect(
      getTableConfig(instagramExecutionControls).indexes
        .filter((item) => item.config.unique)
        .map((item) => item.config.name),
    ).toHaveLength(4);
  });
});
