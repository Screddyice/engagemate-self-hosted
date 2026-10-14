import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { render, screen } from "@testing-library/react";

vi.mock("next/link", () => ({
  default: (props: { children: React.ReactNode; href: string }) =>
    React.createElement("a", { href: props.href }, props.children),
}));

vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(),
  getAgentConfig: vi.fn(),
  getActionCountsSince: vi.fn(),
  getRecordedStoryActivity: vi.fn(),
  getRelationshipSummary: vi.fn(),
  getEngagementFeed: vi.fn(),
  getGrowthDashboard: vi.fn(),
  getLatestRestrictionAt: vi.fn(),
}));

vi.mock("@/lib/selected-account", () => ({
  resolveSelectedAccount: vi.fn(),
}));

vi.mock("@/lib/usage", () => ({
  getUsageSummary: vi.fn(),
}));

const USER = {
  id: "user-1",
  ownerKey: "owner_test_user",
  email: "creator@example.com",
  name: "Creator",
  plan: "pro",
  stripeCustomerId: "cus_secret_1",
  stripeSubscriptionId: "sub_secret_1",
  subscriptionStatus: "active",
  createdAt: new Date("2026-06-01T00:00:00Z"),
  updatedAt: new Date("2026-06-01T00:00:00Z"),
};

const DAY = 24 * 60 * 60 * 1000;

function account(overrides: Record<string, unknown> = {}) {
  return {
    id: "acct-1",
    userId: USER.id,
    username: "creator",
    igUserId: "ig-1",
    loginCountry: "US",
    loginRegion: null,
    loginCity: null,
    followerCount: 1200,
    warmupStartedAt: new Date(Date.now() - 8 * DAY),
    healthStatus: "active",
    lastHealthyAt: new Date(),
    pausedAt: null,
    pauseReason: null,
    createdAt: new Date("2026-07-01T00:00:00Z"),
    updatedAt: new Date("2026-07-01T00:00:00Z"),
    ...overrides,
  };
}

function config(overrides: Record<string, unknown> = {}) {
  return {
    id: "cfg-1",
    userId: USER.id,
    igAccountId: "acct-1",
    enabled: true,
    timezone: "America/Los_Angeles",
    scheduleHour: 9,
    dailyLikeLimit: 20,
    dailyReplyLimit: 5,
    dailyDmLimit: 5,
    growthGoal: 2000,
    persona: "Warm and specific",
    bannedPhrases: [],
    expectedLanguage: "en",
    vipEscalationEnabled: false,
    createdAt: new Date("2026-07-01T00:00:00Z"),
    updatedAt: new Date("2026-07-01T00:00:00Z"),
    ...overrides,
  };
}

const FEED = [
  {
    id: "eng-1",
    action: "reply",
    status: "sent",
    generatedText: "That shot of the studio is great",
    failureCode: null,
    createdAt: new Date("2026-07-30T10:00:00Z"),
    executedAt: new Date("2026-07-30T10:00:05Z"),
    targetUsername: "fanpage",
    targetTier: "vip",
  },
];

const GROWTH = {
  points: [
    { date: "2026-07-15", value: 1160 },
    { date: "2026-08-07", value: 1180 },
    { date: "2026-08-14", value: 1200 },
  ],
  currentFollowers: 1200,
  currentEngagementRateBps: 425,
  followerTrend7d: { start: 1180, end: 1200, delta: 20 },
  followerTrend30d: { start: 1160, end: 1200, delta: 40 },
  engagementTrend7d: { start: 400, end: 425, delta: 25 },
  engagementTrend30d: { start: 375, end: 425, delta: 50 },
  growthGoal: 2000,
  activity7d: {
    reviewed: 4,
    likes: 2,
    replies: 1,
    dms: 2,
    views: 3,
    skipped: 2,
    gated: 1,
    failed: 1,
  },
  estimatedTimeSavedSeconds: 300,
};

async function renderDashboard() {
  const { default: DashboardPage } = await import(
    "@/app/(dashboard)/dashboard/page"
  );
  render(await DashboardPage());
}

type StubOptions = {
  accounts?: unknown[];
  agentConfig?: unknown;
  counts?: { like: number; reply: number; dm: number };
  feed?: unknown[];
  growth?: unknown;
  restrictedAt?: Date | null;
};

async function stub(options: StubOptions = {}) {
  const {
    accounts = [account()],
    agentConfig = config(),
    counts = { like: 5, reply: 2, dm: 1 },
    feed = FEED,
    growth = GROWTH,
    restrictedAt = null,
  } = options;

  const queries = await import("@/lib/queries");
  vi.mocked(queries.getUserByOwnerKey).mockResolvedValue(USER as never);
  const selection = await import("@/lib/selected-account");
  vi.mocked(selection.resolveSelectedAccount).mockResolvedValue({
    accounts,
    selected: accounts[0] ?? null,
  } as never);
  vi.mocked(queries.getAgentConfig).mockResolvedValue(agentConfig as never);
  vi.mocked(queries.getActionCountsSince).mockResolvedValue(counts as never);
  vi.mocked(queries.getRelationshipSummary).mockResolvedValue({follows:22,unfollows:0,alreadyAbsent:6,pending:16,overdue:0,followBacks:1,checked:6});
  vi.mocked(queries.getRecordedStoryActivity).mockResolvedValue([{ action: "view", today: 0, total: 42 }, { action: "like", today: 0, total: 22 }, { action: "reply", today: 0, total: 3 }, { action: "dm", today: 0, total: 0 }]);
  vi.mocked(queries.getEngagementFeed).mockResolvedValue(feed as never);
  vi.mocked(queries.getGrowthDashboard).mockResolvedValue(growth as never);
  vi.mocked(queries.getLatestRestrictionAt).mockResolvedValue(restrictedAt);
  const usage = await import("@/lib/usage");
  vi.mocked(usage.getUsageSummary).mockResolvedValue({
    included: 1_500,
    billable: 420,
    reserved: 3,
    remaining: 1_077,
    overage: 0,
    projectedOverageCents: 0,
    periodStart: new Date("2026-08-01T00:00:00Z"),
    periodEnd: new Date("2026-09-01T00:00:00Z"),
  });
}

describe("Story dashboard home", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubEnv("STRIPE_SECRET_KEY", "sk_test_123");
    vi.stubEnv("STRIPE_STARTER_PRICE_ID", "price_starter_test");
    vi.stubEnv("STRIPE_PRO_PRICE_ID", "price_pro_test");
    vi.stubEnv("STRIPE_SCALE_PRICE_ID", "price_scale_test");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("shows the account health status", async () => {
    await stub();
    await renderDashboard();

    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("@creator")).toBeInTheDocument();
  });

  it("shows usage shared across the manager's accounts", async () => {
    await stub();
    await renderDashboard();

    expect(screen.getByText("420 recorded")).toBeInTheDocument();
    expect(screen.getByText("3 reserved")).toBeInTheDocument();
    expect(screen.getByText(/shared across every connected account/i)).toBeInTheDocument();
  });

  it("separates Pacific activity totals from UTC quotas and billing usage", async () => {
    await stub();
    await renderDashboard();
    expect(screen.getByText("Recorded Story activity")).toBeInTheDocument();
    expect(screen.getByText("Today (Pacific)")).toBeInTheDocument();
    expect(screen.getByText("All time")).toBeInTheDocument();
    expect(screen.getByText("Story views").closest("tr")).toHaveTextContent("42");
    expect(screen.getByText("Story likes").closest("tr")).toHaveTextContent("22");
    expect(screen.getByText("Daily quota (Pacific)")).toBeInTheDocument();
    expect(screen.getByText(/excludes views, follows, and unfollows/)).toBeInTheDocument();
  });

  it("uses the saved account timezone for activity and quota labels", async () => {
    await stub({ agentConfig: config({ timezone: "UTC" }) });
    await renderDashboard();

    expect(screen.getByText("Today (UTC)")).toBeInTheDocument();
    expect(screen.getByText("Daily quota (UTC)")).toBeInTheDocument();
    const queries = await import("@/lib/queries");
    expect(queries.getActionCountsSince).toHaveBeenCalledWith(
      USER.id,
      "acct-1",
      expect.any(Date),
      expect.any(Date),
      "UTC",
    );
    expect(queries.getRecordedStoryActivity).toHaveBeenCalledWith(
      USER.id,
      "acct-1",
      expect.any(Date),
      "UTC",
    );
  });

  it("distinguishes actual unfollows from already-absent accounts and unavailable outcomes", async () => {
    await stub();
    await renderDashboard();
    expect(screen.getByText("Unfollows completed by EngageMate").parentElement).toHaveTextContent("0");
    expect(screen.getByText("Already not following when checked").parentElement).toHaveTextContent("6");
    expect(screen.getByText("Observed follow-backs").parentElement).toHaveTextContent("At least 1");
    expect(screen.getAllByText("Not tracked yet")).toHaveLength(2);
  });

  it("shows the verified count only with full recent coverage", async () => {
    await stub();
    const queries = await import("@/lib/queries");
    vi.mocked(queries.getRelationshipSummary).mockResolvedValue({follows:22,unfollows:0,alreadyAbsent:6,pending:16,overdue:0,followBacks:7,checked:22});
    await renderDashboard();
    expect(screen.getByText("Observed follow-backs").parentElement?.querySelector("dd")).toHaveTextContent(/^7$/);
    expect(screen.getByText(/22 of 22 followed accounts checked/)).toBeInTheDocument();
    expect(screen.queryByText(/Incomplete coverage/)).not.toBeInTheDocument();
  });

  it("shows the warm-up stage and the cap it implies", async () => {
    await stub();
    await renderDashboard();

    expect(screen.getByText(/Days 8-14 \(day 9\)/)).toBeInTheDocument();
    expect(screen.getByText("8 likes, 2 replies")).toBeInTheDocument();
  });

  it("shows the qualifying reconnect reset and holds private messages", async () => {
    await stub({
      restrictedAt: new Date(Date.now() - DAY),
      agentConfig: config({ followReelMessageEnabled: true }),
    });
    await renderDashboard();

    expect(screen.getByText(/Days 1-3 \(day 2\)/)).toBeInTheDocument();
    expect(screen.getByText("3 likes, 0 replies")).toBeInTheDocument();
    expect(screen.getByText(/Story replies are waiting for warm-up/)).toBeInTheDocument();
    expect(screen.getByText(/reconnected after at least 30 days paused/)).toBeInTheDocument();
    expect(screen.getByText(/post-follow reel messages/)).toBeInTheDocument();
  });

  it("caps by the lowest of warm-up, plan, and configured limits", async () => {
    await stub({
      accounts: [account({ warmupStartedAt: new Date(Date.now() - 40 * DAY) })],
      agentConfig: config({ dailyReplyLimit: 1, dailyDmLimit: 0 }),
    });
    await renderDashboard();

    expect(screen.getByText("Days 31-44 (day 41)")).toBeInTheDocument();
    expect(screen.getByText("20 likes, 1 reply")).toBeInTheDocument();
  });

  it("shows today's like, reply, and DM counts against the cap", async () => {
    await stub();
    await renderDashboard();

    expect(screen.getByText("Likes")).toBeInTheDocument();
    expect(screen.getByText("Replies")).toBeInTheDocument();
    expect(screen.queryByText("DMs")).not.toBeInTheDocument();
    expect(screen.getByText("5 / 8")).toBeInTheDocument();
    expect(screen.getByText("2 / 2")).toBeInTheDocument();
  });

  it("shows snapshot-backed follower and engagement trends", async () => {
    await stub();
    await renderDashboard();

    expect(screen.getByText("1,200")).toBeInTheDocument();
    expect(screen.getByText("+20")).toBeInTheDocument();
    expect(screen.getByText("+40")).toBeInTheDocument();
    expect(screen.getByText("4.25%")).toBeInTheDocument();
    expect(screen.getByText("+0.25 pp")).toBeInTheDocument();
  });

  it("shows growth-goal progress when a goal is configured", async () => {
    await stub();
    await renderDashboard();

    expect(screen.getByText("Growth goal")).toBeInTheDocument();
    expect(screen.getByText("1,200 of 2,000 followers")).toBeInTheDocument();
    expect(screen.getByText("60%")).toBeInTheDocument();
  });

  it("omits the growth-goal section when no goal is set", async () => {
    await stub({
      agentConfig: config({ growthGoal: null }),
      growth: { ...GROWTH, growthGoal: null },
    });
    await renderDashboard();

    expect(screen.queryByText("Growth goal")).not.toBeInTheDocument();
  });

  it("shows the recent engagement feed", async () => {
    await stub();
    await renderDashboard();

    expect(screen.getByText("Recent engagement")).toBeInTheDocument();
    expect(
      screen.getByText("Replied to a Story from @fanpage"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("That shot of the studio is great"),
    ).toBeInTheDocument();
    expect(screen.getByText("sent")).toBeInTheDocument();
  });

  it("labels follow-back observations without claiming causal attribution", async () => {
    await stub();
    await renderDashboard();

    const text = document.body.textContent ?? "";
    expect(text).toContain("Incomplete coverage: the total may be higher.");
    expect(text).not.toMatch(/followed you back/i);
    expect(text).not.toMatch(/followers from @/i);
    expect(text).toMatch(
      /does not attribute follower or engagement changes to any individual target or action/i,
    );
  });

  it("never leaks billing identifiers", async () => {
    await stub();
    await renderDashboard();

    const text = document.body.textContent ?? "";
    expect(text).not.toContain("cus_secret_1");
    expect(text).not.toContain("sub_secret_1");
  });

  it("renders an empty state when no account is connected", async () => {
    await stub({ accounts: [] });
    await renderDashboard();

    expect(
      screen.getByText("No Instagram account connected yet."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Likes")).not.toBeInTheDocument();
  });

  it("renders an empty engagement feed state", async () => {
    await stub({ feed: [] });
    await renderDashboard();

    expect(
      screen.getByText(/No Story engagement yet/),
    ).toBeInTheDocument();
  });

  it("renders a paused state with a health link", async () => {
    await stub({
      accounts: [
        account({
          healthStatus: "paused",
          pauseReason: "challenge_detected",
          pausedAt: new Date(),
        }),
      ],
    });
    await renderDashboard();

    expect(
      screen.getByText("Instagram stopped this account"),
    ).toBeInTheDocument();
    expect(screen.getByText(/challenge detected/)).toBeInTheDocument();
    expect(screen.getByText("Review account health")).toBeInTheDocument();
  });

  it("renders a degraded state", async () => {
    await stub({ accounts: [account({ healthStatus: "degraded" })] });
    await renderDashboard();

    expect(screen.getByText("Running degraded")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Resume agent" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Review account health" }),
    ).toHaveAttribute("href", "/account-health");
  });

  it("renders a paused-agent state when the account is disabled", async () => {
    await stub({ agentConfig: config({ enabled: false }) });
    await renderDashboard();

    expect(screen.getByText("Agent is paused")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Resume agent" }),
    ).toBeInTheDocument();
  });

  it("says warm-up has not started when the account has no warm-up date", async () => {
    await stub({
      accounts: [account({ warmupStartedAt: null })],
      counts: { like: 0, reply: 0, dm: 0 },
    });
    await renderDashboard();

    expect(screen.getByText(/Warm-up has not started/)).toBeInTheDocument();
    expect(screen.getAllByText("0 / 0")).toHaveLength(2);
  });

  it("uses the manager's selected owned account", async () => {
    await stub({
      accounts: [account(), account({ id: "acct-2", username: "second" })],
    });
    const selection = await import("@/lib/selected-account");
    vi.mocked(selection.resolveSelectedAccount).mockResolvedValue({
      accounts: [account(), account({ id: "acct-2", username: "second" })],
      selected: account({ id: "acct-2", username: "second" }),
    } as never);
    await renderDashboard();

    const queries = await import("@/lib/queries");
    expect(screen.getByText("@second")).toBeInTheDocument();
    expect(queries.getAgentConfig).toHaveBeenCalledWith(USER.id, "acct-2");
    expect(queries.getGrowthDashboard).toHaveBeenCalledWith(
      USER.id,
      "acct-2",
      expect.any(Date),
    );
  });
});
