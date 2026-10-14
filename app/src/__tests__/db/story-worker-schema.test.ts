import { describe, expect, it } from "vitest";
import { getTableConfig } from "drizzle-orm/pg-core";
import {
  igAccounts,
  igSessions,
  proxies,
  proxyBindings,
  fingerprints,
  agentConfigs,
  targets,
  jobs,
  storyEngagements,
  accountHealthEvents,
  engagementUsageEvents,
  runSummaries,
  accountGrowthSnapshots,
  weeklyReportPreferences,
  weeklyReportDeliveries,
} from "@/db/schema";

describe("story worker schema", () => {
  it("exports every tenant-isolated table", () => {
    for (const table of [
      igAccounts,
      igSessions,
      proxies,
      proxyBindings,
      fingerprints,
      agentConfigs,
      targets,
      jobs,
      storyEngagements,
      accountHealthEvents,
      engagementUsageEvents,
      runSummaries,
      accountGrowthSnapshots,
      weeklyReportPreferences,
      weeklyReportDeliveries,
    ]) {
      expect(table).toBeDefined();
    }
  });

  it("defines constrained growth snapshots and weekly report delivery state", () => {
    const snapshots = getTableConfig(accountGrowthSnapshots);
    const preferences = getTableConfig(weeklyReportPreferences);
    const deliveries = getTableConfig(weeklyReportDeliveries);

    expect(snapshots.foreignKeys.map((key) => key.getName())).toContain(
      "account_growth_snapshots_account_user_fk",
    );
    expect(snapshots.indexes.map((entry) => entry.config.name)).toContain(
      "account_growth_snapshots_account_date_idx",
    );
    expect(snapshots.checks.map((entry) => entry.name)).toEqual(
      expect.arrayContaining([
        "account_growth_snapshots_status_check",
        "account_growth_snapshots_source_check",
        "account_growth_snapshots_counts_check",
        "account_growth_snapshots_follower_status_check",
        "account_growth_snapshots_failure_code_check",
      ]),
    );
    expect(preferences.checks.map((entry) => entry.name)).toContain(
      "weekly_report_preferences_send_hour_check",
    );
    expect(deliveries.indexes.map((entry) => entry.config.name)).toContain(
      "weekly_report_deliveries_user_week_idx",
    );
    expect(deliveries.checks.map((entry) => entry.name)).toEqual(
      expect.arrayContaining([
        "weekly_report_deliveries_status_check",
        "weekly_report_deliveries_attempts_check",
        "weekly_report_deliveries_lease_check",
        "weekly_report_deliveries_payload_hash_check",
        "weekly_report_deliveries_last_error_code_check",
      ]),
    );
  });



  it("exports a tenant-scoped engagement usage ledger", () => {
    const config = getTableConfig(engagementUsageEvents);
    const constraintNames = config.foreignKeys.map((key) => key.getName());
    const checkNames = config.checks.map((entry) => entry.name);

    expect(config.name).toBe("engagement_usage_events");
    expect(constraintNames).toContain("engagement_usage_events_account_user_fk");
    expect(constraintNames).toContain("engagement_usage_events_engagement_scope_fk");
    expect(checkNames).toEqual(
      expect.arrayContaining([
        "engagement_usage_events_surface_check",
        "engagement_usage_events_action_check",
        "engagement_usage_events_status_check",
        "engagement_usage_events_quantity_check",
      ]),
    );
  });

  it("stores bounded aggregate COGS counters on each run summary", () => {
    expect(runSummaries.inputTokens).toBeDefined();
    expect(runSummaries.outputTokens).toBeDefined();
    expect(runSummaries.imagesAnalyzed).toBeDefined();
    expect(runSummaries.videoFramesAnalyzed).toBeDefined();
    expect(runSummaries.retries).toBeDefined();
    expect(runSummaries.runtimeMs).toBeDefined();
    const config = getTableConfig(runSummaries);
    expect(config.checks.map((entry) => entry.name)).toContain(
      "run_summaries_cost_counters_check",
    );
  });

  it("uses stable Instagram identity within each tenant", () => {
    const index = getTableConfig(igAccounts).indexes.find(
      (candidate) => candidate.config.name === "ig_accounts_user_ig_user_id_idx",
    );

    expect(index?.config.unique).toBe(true);
    expect(index?.config.where).toBeDefined();
  });
});
