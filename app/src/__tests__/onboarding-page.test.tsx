import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  redirect: vi.fn(),
}));

vi.mock("@/lib/ensure-user", () => ({
  ensureUser: vi.fn(),
}));

vi.mock("@/lib/queries", () => ({
  getUserAccounts: vi.fn(),
  getAgentConfig: vi.fn(),
  hasEnabledTarget: vi.fn(),
}));

vi.mock("@/lib/onboarding-control-api", () => ({
  getOnboardingBrandProfile: vi.fn(),
}));

vi.mock("@/lib/selected-account", () => ({
  resolveSelectedAccount: vi.fn(),
}));

const analysis = {
  voice_summary: "Warm, direct, and practical",
  audience_summary: "Founders and operators",
  brand_identity: "Builder",
  engagement_style: "Thoughtful",
  content_pillars: ["AI"],
  conversation_topics: ["Deployment"],
  search_terms: ["AI founders"],
  us_audience_signals: ["United States"],
  next_question: "What are you building?",
};

describe("OnboardingPage", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const queries = await import("@/lib/queries");
    const selection = await import("@/lib/selected-account");
    vi.mocked(selection.resolveSelectedAccount).mockImplementation(async (userId) => {
      const accounts = await queries.getUserAccounts(userId);
      const selected =
        accounts.find((account: { healthStatus?: string | null }) =>
          (account.healthStatus ?? "active").toLowerCase() === "active"
        ) ?? accounts[0] ?? null;
      return { accounts, selected } as never;
    });
  });

  it("derives saved voice from the authoritative onboarding profile", async () => {
    const { ensureUser } = await import("@/lib/ensure-user");
    vi.mocked(ensureUser).mockResolvedValue({
      id: "user-1",
      name: "Sam",
    } as never);

    const queries = await import("@/lib/queries") as unknown as {
      getUserAccounts: ReturnType<typeof vi.fn>;
      getAgentConfig: ReturnType<typeof vi.fn>;
      hasEnabledTarget: ReturnType<typeof vi.fn>;
    };
    const control = await import("@/lib/onboarding-control-api");
    queries.getUserAccounts.mockResolvedValue([
      { id: "account-1", username: "demo.creator", healthStatus: "active" },
    ]);
    queries.getAgentConfig.mockResolvedValue(null);
    queries.hasEnabledTarget.mockResolvedValue(false);
    vi.mocked(control.getOnboardingBrandProfile).mockResolvedValue({
      profile_id: "profile-1",
      ig_account_id: "account-1",
      status: "complete",
      reference_urls: ["one", "two", "three"],
      answers: {},
      analysis,
      candidates: [],
    });

    const { default: OnboardingPage } = await import(
      "@/app/(onboarding)/onboarding/page"
    );
    const page = await OnboardingPage();

    expect(control.getOnboardingBrandProfile).toHaveBeenCalledWith(
      "user-1",
      "account-1",
    );
    expect(page.props.persona).toBe("Warm, direct, and practical");
  });

  it("ignores brand state returned for a different Instagram account", async () => {
    const { ensureUser } = await import("@/lib/ensure-user");
    vi.mocked(ensureUser).mockResolvedValue({
      id: "user-1",
      name: "Sam",
    } as never);
    const queries = await import("@/lib/queries") as unknown as {
      getUserAccounts: ReturnType<typeof vi.fn>;
      getAgentConfig: ReturnType<typeof vi.fn>;
      hasEnabledTarget: ReturnType<typeof vi.fn>;
    };
    const control = await import("@/lib/onboarding-control-api");
    queries.getUserAccounts.mockResolvedValue([
      { id: "account-1", username: "demo.creator", healthStatus: "active" },
    ]);
    queries.getAgentConfig.mockResolvedValue(null);
    queries.hasEnabledTarget.mockResolvedValue(false);
    vi.mocked(control.getOnboardingBrandProfile).mockResolvedValue({
      profile_id: "profile-other",
      ig_account_id: "account-other",
      status: "complete",
      reference_urls: ["one", "two", "three"],
      answers: {},
      analysis,
      candidates: [],
    });

    const { default: OnboardingPage } = await import(
      "@/app/(onboarding)/onboarding/page"
    );
    const page = await OnboardingPage();

    expect(page.props.persona).toBe("");
    expect(page.props.brandProfile).toBeNull();
  });

  it("resumes at Brand when voice is saved but no target is enabled", async () => {
    const { ensureUser } = await import("@/lib/ensure-user");
    vi.mocked(ensureUser).mockResolvedValue({
      id: "user-1",
      name: "Sam",
    } as never);
    const queries = await import("@/lib/queries") as unknown as {
      getUserAccounts: ReturnType<typeof vi.fn>;
      getAgentConfig: ReturnType<typeof vi.fn>;
      hasEnabledTarget: ReturnType<typeof vi.fn>;
    };
    const control = await import("@/lib/onboarding-control-api");
    queries.getUserAccounts.mockResolvedValue([
      { id: "account-1", username: "demo.creator", healthStatus: "active" },
    ]);
    queries.getAgentConfig.mockResolvedValue({
      enabled: false,
      persona: "Warm, direct, and practical",
    });
    queries.hasEnabledTarget.mockResolvedValue(false);
    vi.mocked(control.getOnboardingBrandProfile).mockResolvedValue({
      profile_id: "profile-1",
      ig_account_id: "account-1",
      status: "complete",
      reference_urls: ["one", "two", "three"],
      answers: {},
      analysis,
      candidates: [],
    });

    const { default: OnboardingPage } = await import(
      "@/app/(onboarding)/onboarding/page"
    );
    const page = await OnboardingPage();

    expect(page.props.initialStep).toBe(3);
    expect(page.props.brandProfile.profile_id).toBe("profile-1");
  });

  it("resumes at Launch when voice and an enabled target are saved", async () => {
    const { ensureUser } = await import("@/lib/ensure-user");
    vi.mocked(ensureUser).mockResolvedValue({ id: "user-1", name: "Sam" } as never);
    const queries = await import("@/lib/queries") as unknown as {
      getUserAccounts: ReturnType<typeof vi.fn>;
      getAgentConfig: ReturnType<typeof vi.fn>;
      hasEnabledTarget: ReturnType<typeof vi.fn>;
    };
    const control = await import("@/lib/onboarding-control-api");
    queries.getUserAccounts.mockResolvedValue([
      { id: "account-1", username: "demo.creator", healthStatus: "active" },
    ]);
    queries.getAgentConfig.mockResolvedValue({ enabled: false, persona: "Direct" });
    queries.hasEnabledTarget.mockResolvedValue(true);
    vi.mocked(control.getOnboardingBrandProfile).mockResolvedValue(null);

    const { default: OnboardingPage } = await import(
      "@/app/(onboarding)/onboarding/page"
    );
    const page = await OnboardingPage();

    expect(page.props.initialStep).toBe(5);
    expect(page.props.hasTarget).toBe(true);
  });
  it("resumes at Launch when voice and a source audience are approved", async () => {
    const { ensureUser } = await import("@/lib/ensure-user");
    vi.mocked(ensureUser).mockResolvedValue({ id: "user-1", name: "Sam" } as never);
    const queries = await import("@/lib/queries") as unknown as {
      getUserAccounts: ReturnType<typeof vi.fn>;
      getAgentConfig: ReturnType<typeof vi.fn>;
      hasEnabledTarget: ReturnType<typeof vi.fn>;
    };
    const control = await import("@/lib/onboarding-control-api");
    queries.getUserAccounts.mockResolvedValue([
      { id: "account-1", username: "demo.creator", healthStatus: "active" },
    ]);
    queries.getAgentConfig.mockResolvedValue({ enabled: false, persona: "Direct" });
    queries.hasEnabledTarget.mockResolvedValue(false);
    vi.mocked(control.getOnboardingBrandProfile).mockResolvedValue({
      profile_id: "profile-1", ig_account_id: "account-1", status: "complete",
      reference_urls: ["one", "two", "three"], answers: {}, analysis,
      candidates: [], follower_audience_approved: true,
    });

    const { default: OnboardingPage } = await import(
      "@/app/(onboarding)/onboarding/page"
    );
    const page = await OnboardingPage();

    expect(page.props.initialStep).toBe(5);
    expect(page.props.hasTarget).toBe(true);
  });

  it("loads resume state for the healthy account", async () => {
    const { ensureUser } = await import("@/lib/ensure-user");
    vi.mocked(ensureUser).mockResolvedValue({ id: "user-1", name: "Sam" } as never);
    const queries = await import("@/lib/queries") as unknown as {
      getUserAccounts: ReturnType<typeof vi.fn>;
      getAgentConfig: ReturnType<typeof vi.fn>;
      hasEnabledTarget: ReturnType<typeof vi.fn>;
    };
    const control = await import("@/lib/onboarding-control-api");
    queries.getUserAccounts.mockResolvedValue([
      { id: "expired-1", username: "alpha", healthStatus: "expired" },
      { id: "active-1", username: "zulu", healthStatus: "active" },
    ]);
    queries.getAgentConfig.mockResolvedValue({ enabled: false, persona: "Direct" });
    queries.hasEnabledTarget.mockResolvedValue(true);
    vi.mocked(control.getOnboardingBrandProfile).mockResolvedValue(null);

    const { default: OnboardingPage } = await import(
      "@/app/(onboarding)/onboarding/page"
    );
    const page = await OnboardingPage();

    expect(queries.getAgentConfig).toHaveBeenCalledWith("user-1", "active-1");
    expect(queries.hasEnabledTarget).toHaveBeenCalledWith("user-1", "active-1");
    expect(page.props.initialStep).toBe(5);
  });

  it("loads resume state for the account selected by the user", async () => {
    const { ensureUser } = await import("@/lib/ensure-user");
    vi.mocked(ensureUser).mockResolvedValue({ id: "user-1", name: "Sam" } as never);
    const queries = await import("@/lib/queries") as unknown as {
      getAgentConfig: ReturnType<typeof vi.fn>;
      hasEnabledTarget: ReturnType<typeof vi.fn>;
    };
    const selection = await import("@/lib/selected-account");
    const control = await import("@/lib/onboarding-control-api");
    const accounts = [
      { id: "account-a", username: "alpha", healthStatus: "active" },
      { id: "account-b", username: "zulu", healthStatus: "active" },
    ];
    vi.mocked(selection.resolveSelectedAccount).mockResolvedValue({
      accounts,
      selected: accounts[1],
    } as never);
    queries.getAgentConfig.mockResolvedValue({ enabled: false, persona: "Direct" });
    queries.hasEnabledTarget.mockResolvedValue(true);
    vi.mocked(control.getOnboardingBrandProfile).mockResolvedValue(null);

    const { default: OnboardingPage } = await import(
      "@/app/(onboarding)/onboarding/page"
    );
    const page = await OnboardingPage();

    expect(queries.getAgentConfig).toHaveBeenCalledWith("user-1", "account-b");
    expect(queries.hasEnabledTarget).toHaveBeenCalledWith("user-1", "account-b");
    expect(page.props.initialAccountId).toBe("account-b");
  });

  it("redirects an enabled account even if parallel brand loading fails", async () => {
    const navigation = await import("next/navigation");
    const { ensureUser } = await import("@/lib/ensure-user");
    vi.mocked(ensureUser).mockResolvedValue({ id: "user-1", name: "Sam" } as never);
    const queries = await import("@/lib/queries") as unknown as {
      getUserAccounts: ReturnType<typeof vi.fn>;
      getAgentConfig: ReturnType<typeof vi.fn>;
      hasEnabledTarget: ReturnType<typeof vi.fn>;
    };
    const control = await import("@/lib/onboarding-control-api");
    queries.getUserAccounts.mockResolvedValue([
      { id: "account-1", username: "demo.creator", healthStatus: "active" },
    ]);
    queries.getAgentConfig.mockResolvedValue({ enabled: true, persona: "Direct" });
    queries.hasEnabledTarget.mockResolvedValue(false);
    vi.mocked(control.getOnboardingBrandProfile).mockRejectedValue(
      new Error("Control API unavailable"),
    );

    const { default: OnboardingPage } = await import(
      "@/app/(onboarding)/onboarding/page"
    );
    await OnboardingPage();

    expect(navigation.redirect).toHaveBeenCalledWith("/dashboard");
    expect(control.getOnboardingBrandProfile).toHaveBeenCalledWith(
      "user-1",
      "account-1",
    );
  });

  it("resumes a connected account without voice at the brand step", async () => {
    const { ensureUser } = await import("@/lib/ensure-user");
    vi.mocked(ensureUser).mockResolvedValue({ id: "user-1", name: "Sam" } as never);
    const queries = await import("@/lib/queries") as unknown as {
      getUserAccounts: ReturnType<typeof vi.fn>;
      getAgentConfig: ReturnType<typeof vi.fn>;
      hasEnabledTarget: ReturnType<typeof vi.fn>;
    };
    const control = await import("@/lib/onboarding-control-api");
    queries.getUserAccounts.mockResolvedValue([
      { id: "account-1", username: "demo.creator", healthStatus: "active" },
    ]);
    queries.getAgentConfig.mockResolvedValue({ enabled: false, persona: null });
    queries.hasEnabledTarget.mockResolvedValue(false);
    vi.mocked(control.getOnboardingBrandProfile).mockResolvedValue(null);

    const { default: OnboardingPage } = await import(
      "@/app/(onboarding)/onboarding/page"
    );
    const page = await OnboardingPage();

    expect(page.props.initialStep).toBe(3);
    expect(page.props.persona).toBe("");
  });

  it("marks saved brand state unavailable instead of treating it as missing", async () => {
    const { ensureUser } = await import("@/lib/ensure-user");
    vi.mocked(ensureUser).mockResolvedValue({ id: "user-1", name: "Sam" } as never);
    const queries = await import("@/lib/queries") as unknown as {
      getUserAccounts: ReturnType<typeof vi.fn>;
      getAgentConfig: ReturnType<typeof vi.fn>;
      hasEnabledTarget: ReturnType<typeof vi.fn>;
    };
    const control = await import("@/lib/onboarding-control-api");
    queries.getUserAccounts.mockResolvedValue([
      { id: "account-1", username: "demo.creator", healthStatus: "active" },
    ]);
    queries.getAgentConfig.mockResolvedValue({ enabled: false, persona: null });
    queries.hasEnabledTarget.mockResolvedValue(false);
    vi.mocked(control.getOnboardingBrandProfile).mockRejectedValue(
      new Error("Control API unavailable"),
    );

    const { default: OnboardingPage } = await import(
      "@/app/(onboarding)/onboarding/page"
    );
    const page = await OnboardingPage();

    expect(page.props.initialStep).toBe(3);
    expect(page.props.brandProfile).toBeNull();
    expect(page.props.brandProfileUnavailable).toBe(true);
  });

  it("starts optional brand loading while config readiness is still pending", async () => {
    const { ensureUser } = await import("@/lib/ensure-user");
    vi.mocked(ensureUser).mockResolvedValue({ id: "user-1", name: "Sam" } as never);
    const queries = await import("@/lib/queries") as unknown as {
      getUserAccounts: ReturnType<typeof vi.fn>;
      getAgentConfig: ReturnType<typeof vi.fn>;
      hasEnabledTarget: ReturnType<typeof vi.fn>;
    };
    const control = await import("@/lib/onboarding-control-api");
    queries.getUserAccounts.mockResolvedValue([
      { id: "account-1", username: "demo.creator", healthStatus: "active" },
    ]);
    let resolveConfig: ((value: { enabled: boolean; persona: null }) => void) | undefined;
    queries.getAgentConfig.mockReturnValue(
      new Promise((resolve) => {
        resolveConfig = resolve;
      }),
    );
    queries.hasEnabledTarget.mockResolvedValue(false);
    vi.mocked(control.getOnboardingBrandProfile).mockResolvedValue(null);

    const { default: OnboardingPage } = await import(
      "@/app/(onboarding)/onboarding/page"
    );
    const rendering = OnboardingPage();

    await vi.waitFor(() => {
      expect(control.getOnboardingBrandProfile).toHaveBeenCalledWith(
        "user-1",
        "account-1",
      );
    });
    resolveConfig?.({ enabled: false, persona: null });
    await rendering;
  });

  it.each([
    ["login_required", true], ["login_required", false], ["manual_pause", false],
  ])(
    "routes a launched paused account to health for review (%s, enabled=%s)",
    async (pauseReason, enabled) => {
      const { ensureUser } = await import("@/lib/ensure-user");
      const queries = await import("@/lib/queries");
      const selection = await import("@/lib/selected-account");
      const control = await import("@/lib/onboarding-control-api");
      const { redirect } = await import("next/navigation");
      vi.mocked(ensureUser).mockResolvedValue({ id: "user-1" } as never);
      const selected = {
        id: "paused-selected", username: "creator", healthStatus: "paused", pauseReason,
      };
      vi.mocked(selection.resolveSelectedAccount).mockResolvedValue({
        accounts: [selected, { id: "other-active", healthStatus: "active" }], selected,
      } as never);
      vi.mocked(queries.getAgentConfig).mockResolvedValue({
        enabled, launchedAt: new Date("2026-09-01T00:00:00Z"),
      } as never);
      const { default: OnboardingPage } = await import(
        "@/app/(onboarding)/onboarding/page"
      );

      await OnboardingPage();

      expect(queries.getAgentConfig).toHaveBeenCalledWith("user-1", "paused-selected");
      expect(redirect).toHaveBeenCalledWith("/account-health");
      expect(redirect).not.toHaveBeenCalledWith("/dashboard");
      expect(queries.hasEnabledTarget).not.toHaveBeenCalled();
      expect(control.getOnboardingBrandProfile).not.toHaveBeenCalled();
    },
  );

  it("keeps an unlaunched paused account in onboarding", async () => {
    const { ensureUser } = await import("@/lib/ensure-user");
    const queries = await import("@/lib/queries");
    const { redirect } = await import("next/navigation");
    vi.mocked(ensureUser).mockResolvedValue({ id: "user-1" } as never);
    vi.mocked(queries.getUserAccounts).mockResolvedValue([
      { id: "account-1", username: "creator", healthStatus: "paused" },
    ] as never);
    vi.mocked(queries.getAgentConfig).mockResolvedValue({
      enabled: false, launchedAt: null,
    } as never);
    const { default: OnboardingPage } = await import(
      "@/app/(onboarding)/onboarding/page"
    );

    const page = await OnboardingPage();

    expect(redirect).not.toHaveBeenCalled();
    expect(page.props.initialStep).toBe(1);
    expect(page.props.initialAccountId).toBe("account-1");
  });
});
