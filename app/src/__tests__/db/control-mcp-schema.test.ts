import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

describe("control MCP schema", () => {
  it("defines tenant-bound MCP state and versioned config", () => {
    const sql = readFileSync(
      resolve(process.cwd(), "src/db/migrations/0013_control_mcp.sql"),
      "utf8",
    ).toLowerCase();

    for (const name of [
      "mcp_oauth_clients",
      "mcp_authorization_requests",
      "mcp_authorization_codes",
      "mcp_tokens",
      "mcp_pending_changes",
      "mcp_audit_events",
    ]) {
      expect(sql).toContain(`create table "${name}"`);
    }

    expect(sql).toContain('add column "version" integer not null default 1');
    expect(sql).toContain('unique ("user_id", "ig_account_id", "version")');
  });
});
