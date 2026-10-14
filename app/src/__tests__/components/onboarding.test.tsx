import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { act, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WizardShell } from "@/components/onboarding/wizard-shell";
import { ProfileStep } from "@/components/onboarding/profile-step";
import { VoiceStep } from "@/components/onboarding/voice-step";
import { LaunchStep } from "@/components/onboarding/launch-step";
import { InstagramConnectWizard } from "@/components/platform-login/instagram-connect-wizard";
import { AccountsStep } from "@/components/onboarding/accounts-step";
import { OnboardingWizard } from "@/app/(onboarding)/onboarding/wizard-client";

// Mock fetch
const mockFetch = vi.fn();
const mockAllocationFetch = vi.fn();
global.fetch = ((...args: Parameters<typeof fetch>) => {
  const url = String(args[0]);
  return /^\/api\/accounts\/[^/]+\/config$/.test(url) && !args[1]?.method
    ? mockAllocationFetch(...args) : mockFetch(...args);
}) as typeof fetch;

// Mock next/navigation
const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: vi.fn(), back: vi.fn() }),
  usePathname: () => "/onboarding",
}));

beforeEach(() => {
  vi.clearAllMocks();
  mockFetch.mockReset();
  mockAllocationFetch.mockImplementation(async (url: string) => ({
    ok: true, json: async () => ({
      igAccountId: url.split("/")[3], dailyViewLimit: 30,
      dailyLikeLimit: 3, dailyReplyLimit: 0, dailyDmLimit: 0,
      followAfterEngagement: false, autoUnfollowEnabled: true, dailyFollowLimit: 3,
    }),
  }));
});

afterEach(() => {
  vi.unstubAllEnvs();
});

// ===========================================================================
// InstagramConnectWizard
// ===========================================================================
describe("InstagramConnectWizard", () => {
  it("keeps the selected paused account reconnectable beside another active account", async () => {
    mockFetch.mockResolvedValue({ ok: true, json: async () => [
      { id: "other", username: "other", healthStatus: "active" },
      { id: "selected", username: "demo_user", healthStatus: "paused" },
    ] });
    render(<InstagramConnectWizard hostedLoginEnabled selectedAccountId="selected" />);
    expect(await screen.findByLabelText("Instagram username")).toHaveValue("demo_user");
    expect(screen.queryByText("Instagram connected")).toBeNull();
  });
  it("rechecks account health when the user returns to a stale tab", async () => {
    const account = { id: "selected", username: "demo_user", healthStatus: "active" };
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [account] });
    render(<InstagramConnectWizard hostedLoginEnabled selectedAccountId="selected" />);
    await screen.findByText("Instagram connected");
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [{ ...account, healthStatus: "paused" }] });
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    expect(await screen.findByLabelText("Instagram username")).toHaveValue("demo_user");
    expect(screen.queryByText("Instagram connected")).toBeNull();
  });
  it("does not treat a failed initial health load as an empty account list", async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 503 });
    render(<InstagramConnectWizard hostedLoginEnabled selectedAccountId="selected" />);
    await screen.findByText(/couldn't check your account status/);
    expect(screen.queryByRole("button", { name: "Connect Instagram" })).toBeNull();
  });
  it("loads existing accounts from the server before offering a connection", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => [
        { id: "acct-1", username: "engagemate", healthStatus: "active" },
      ],
    });

    render(React.createElement(InstagramConnectWizard));

    expect(await screen.findByText(/^Instagram$/)).toBeInTheDocument();
    expect(screen.queryByText("@engagemate")).not.toBeInTheDocument();
    expect(mockFetch).toHaveBeenCalledWith("/api/accounts", expect.objectContaining({ cache: "no-store" }));
    expect(
      screen.queryByText(/not available yet/i),
    ).not.toBeInTheDocument();
  });

  it("offers the same connection flow when an account is expired", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => [
        { id: "acct-1", username: "engagemate", healthStatus: "expired" },
      ],
    });

    render(React.createElement(InstagramConnectWizard));

    expect(
      await screen.findByRole("button", { name: "Connect Instagram" }),
    ).toBeInTheDocument();
  });
});

// ===========================================================================
// AccountsStep
// ===========================================================================
describe("AccountsStep", () => {
  beforeEach(() => {
    mockFetch.mockResolvedValue({ ok: true, json: async () => [] });
  });

  it("does not advance until the server confirms an account", async () => {
    const onNext = vi.fn();
    render(
      <AccountsStep
        connectedAccounts={[]}
        onConnected={vi.fn()}
        onNext={onNext}
        onBack={vi.fn()}
      />,
    );

    expect(
      screen.queryByRole("button", { name: "Skip for now" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("offers no way to postpone the connection", () => {
    render(
      <AccountsStep
        connectedAccounts={[]}
        onConnected={vi.fn()}
        onNext={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    expect(screen.queryByText(/coming soon/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/skip/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/recommended to continue/i)).not.toBeInTheDocument();
  });

  it("advances once an account is connected", async () => {
    const onNext = vi.fn();
    render(
      <AccountsStep
        connectedAccounts={[{ id: "acct-1", username: "engagemate" }]}
        onConnected={vi.fn()}
        onNext={onNext}
        onBack={vi.fn()}
      />,
    );

    const next = screen.getByRole("button", { name: "Next" });
    expect(next).toBeEnabled();
    fireEvent.click(next);
    expect(onNext).toHaveBeenCalled();
  });

  it("keeps Next disabled when every saved account is unhealthy", () => {
    render(
      <AccountsStep
        connectedAccounts={[
          { id: "acct-1", username: "engagemate", healthStatus: "expired" },
        ]}
        onConnected={vi.fn()}
        onNext={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it.each(["paused", "degraded"])(
    "keeps Next disabled when the saved account is %s",
    (healthStatus) => {
      render(
        <AccountsStep
          connectedAccounts={[
            { id: "acct-1", username: "engagemate", healthStatus },
          ]}
          onConnected={vi.fn()}
          onNext={vi.fn()}
          onBack={vi.fn()}
        />,
      );

      expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    },
  );
});

describe("OnboardingWizard authenticated-first order", () => {
  it("connects Instagram before Brand analysis", async () => {
    mockFetch.mockResolvedValue({ ok: true });
    render(
      <OnboardingWizard
        name="Sam"
        accounts={[{ id: "acct-1", username: "engagemate", healthStatus: "active" }]}
        persona=""
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    expect(
      await screen.findByRole("heading", { name: "Connect your Instagram account" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Let's understand your brand" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    expect(
      await screen.findByRole("heading", { name: "Let's understand your brand" }),
    ).toBeInTheDocument();
  });

  it("carries a successful target import through to Launch", async () => {
    mockFetch.mockResolvedValue({ ok: true });
    render(
      <OnboardingWizard
        name="Sam"
        accounts={[{ id: "acct-1", username: "engagemate", healthStatus: "active" }]}
        persona="Direct and useful"
        initialStep={3}
        brandProfile={{
          profile_id: "profile-1",
          ig_account_id: "acct-1",
          status: "complete",
          reference_urls: ["one", "two", "three"],
          answers: {},
          analysis: {
            voice_summary: "Direct and useful",
            audience_summary: "Founders and operators",
            brand_identity: "Builder",
            engagement_style: "Thoughtful",
            content_pillars: ["AI"],
            conversation_topics: ["Deployment"],
            search_terms: ["AI founders"],
            us_audience_signals: ["United States"],
            next_question: "What are you building?",
          },
          candidates: [{
            candidate_id: "candidate-1",
            username: "exampletechinc",
            score: 92,
            accepted: true,
            imported: false,
            us_signal: true,
            engagement_rate: 0.04,
            reasons: ["Audience match"],
          }],
        }}
      />,
    );

    fireEvent.click(screen.getByRole("checkbox", { name: /exampletechinc/i }));
    fireEvent.click(
      screen.getByRole("button", { name: "Add selected to target list" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Use this direction" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Use this direction" }));
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));

    const targetSummary = await screen.findByText("Audience approved");
    expect(within(targetSummary.parentElement!).getByText("Yes")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /launch engagemate/i }),
    ).toBeEnabled();
  });

  it("uses the explicitly selected account for saved brand state", () => {
    render(
      <OnboardingWizard
        name="Sam"
        accounts={[
          { id: "acct-a", username: "alpha", healthStatus: "active" },
          { id: "acct-b", username: "zulu", healthStatus: "active" },
        ]}
        initialAccountId="acct-b"
        persona="Direct and useful"
        initialStep={3}
        brandProfile={{
          profile_id: "profile-b",
          ig_account_id: "acct-b",
          status: "complete",
          reference_urls: ["one", "two", "three"],
          answers: {},
          analysis: {
            voice_summary: "Direct and useful",
            audience_summary: "Founders and operators",
            brand_identity: "Builder",
            engagement_style: "Thoughtful",
            content_pillars: ["AI"],
            conversation_topics: ["Deployment"],
            search_terms: ["AI founders"],
            us_audience_signals: ["United States"],
            next_question: "What are you building?",
          },
          candidates: [{
            candidate_id: "candidate-b",
            username: "selectedcreator",
            score: 92,
            accepted: true,
            imported: false,
            us_signal: true,
            engagement_rate: 0.04,
            reasons: ["Audience match"],
          }],
        }}
      />,
    );

    expect(screen.getByRole("checkbox", { name: /selectedcreator/i })).toBeInTheDocument();
  });

  it("blocks brand edits while saved strategy cannot be loaded", () => {
    render(
      <OnboardingWizard
        name="Sam"
        accounts={[{ id: "acct-1", username: "engagemate", healthStatus: "active" }]}
        initialAccountId="acct-1"
        persona=""
        initialStep={3}
        brandProfileUnavailable
      />,
    );

    expect(
      screen.getByText("We couldn't load your saved brand strategy."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /analyze my brand/i }),
    ).not.toBeInTheDocument();
  });
});

describe("WizardShell", () => {
  // Test 1
  it("should render the four story-foundation step labels", () => {
    render(<WizardShell currentStep={1}><div>Content</div></WizardShell>);
    expect(screen.getByText("Profile")).toBeInTheDocument();
    expect(screen.getByText("Accounts")).toBeInTheDocument();
    expect(screen.getByText("Voice")).toBeInTheDocument();
    expect(screen.getByText("Launch")).toBeInTheDocument();
    expect(screen.queryByText("Company")).not.toBeInTheDocument();
    expect(screen.queryByText("Configure")).not.toBeInTheDocument();
  });

  // Test 2
  it("should render children", () => {
    render(<WizardShell currentStep={1}><div>Step Content</div></WizardShell>);
    expect(screen.getByText("Step Content")).toBeInTheDocument();
  });

  // Test 3
  it("should show checkmark for completed steps", () => {
    render(<WizardShell currentStep={3}><div>Content</div></WizardShell>);
    // Steps 1 and 2 should show checkmarks
    const checkmarks = screen.getAllByText("✓");
    expect(checkmarks.length).toBe(2);
  });

  // Test 4
  it("should show step number for current and future steps", () => {
    render(<WizardShell currentStep={2}><div>Content</div></WizardShell>);
    // Step 1 is complete (checkmark), steps 2-4 show numbers
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  // Test 5
  it("should render with step 1", () => {
    render(<WizardShell currentStep={1}><div>First</div></WizardShell>);
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("First")).toBeInTheDocument();
  });

  // Test 6
  it("should render with step 4", () => {
    render(<WizardShell currentStep={4}><div>Last</div></WizardShell>);
    expect(screen.getByText("Last")).toBeInTheDocument();
    // All 3 previous steps should have checkmarks
    const checkmarks = screen.getAllByText("✓");
    expect(checkmarks.length).toBe(3);
  });

  // Test 7
  it("should render step connectors between steps", () => {
    const { container } = render(<WizardShell currentStep={1}><div>C</div></WizardShell>);
    // One fewer connector than steps; the wizard gained a Brand step.
    const connectors = container.querySelectorAll("[data-wizard-connector]");
    expect(connectors.length).toBe(4);
  });

  it("lets step connectors shrink on mobile without clipping the last step", () => {
    const { container } = render(<WizardShell currentStep={2}><div>C</div></WizardShell>);
    const progress = container.firstElementChild?.firstElementChild;
    const connectors = container.querySelectorAll("[data-wizard-connector]");

    expect(progress).toHaveClass("min-w-0");
    expect(connectors).toHaveLength(4);
    for (const connector of Array.from(connectors)) {
      expect(connector).toHaveClass("min-w-2", "flex-1");
    }
  });
});

// ===========================================================================
// ProfileStep
// ===========================================================================
describe("ProfileStep", () => {
  it("recovers from a network failure without losing the entered name", async () => {
    mockFetch.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const onSaved = vi.fn();
    render(<ProfileStep defaultName="Beta User" onSaved={onSaved} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText(/couldn't save your name/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
    expect(screen.getByDisplayValue("Beta User")).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
    mockFetch.mockResolvedValueOnce({ ok: true });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith("Beta User"));
  });

  const defaultProps = {
    defaultName: "",
    onSaved: vi.fn(),
  };

  // Test 8
  it("should render heading", () => {
    render(<ProfileStep {...defaultProps} />);
    expect(screen.getByText("What should we call you?")).toBeInTheDocument();
  });

  // Test 9
  it("should render the name input", () => {
    render(<ProfileStep {...defaultProps} />);
    expect(screen.getByPlaceholderText("Alex Rivera")).toBeInTheDocument();
  });

  // Test 10
  it("should not ask for a company slug", () => {
    render(<ProfileStep {...defaultProps} />);
    expect(screen.queryByPlaceholderText("acme-corp")).not.toBeInTheDocument();
    expect(screen.queryByText(/slug/i)).not.toBeInTheDocument();
  });

  // Test 11
  it("should show default value", () => {
    render(<ProfileStep defaultName="Test User" onSaved={vi.fn()} />);
    expect(screen.getByDisplayValue("Test User")).toBeInTheDocument();
  });

  // Test 12
  it("should accept typed input", async () => {
    render(<ProfileStep {...defaultProps} />);
    const nameInput = screen.getByPlaceholderText("Alex Rivera") as HTMLInputElement;
    await userEvent.type(nameInput, "Alex");
    expect(nameInput.value).toBe("Alex");
  });

  // Test 13
  it("should disable Next when the name is empty", () => {
    render(<ProfileStep {...defaultProps} />);
    const nextButton = screen.getByRole("button", { name: /next/i });
    expect(nextButton).toBeDisabled();
  });

  // Test 14
  it("should PUT to the profile route on submit", async () => {
    mockFetch.mockResolvedValue({ ok: true });
    render(<ProfileStep defaultName="Test" onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith("/api/onboarding/profile", expect.objectContaining({
        method: "PUT",
      }));
    });
  });

  // Test 15
  it("should call onSaved with the trimmed name after a successful save", async () => {
    mockFetch.mockResolvedValue({ ok: true });
    const onSaved = vi.fn();
    render(<ProfileStep defaultName="Test" onSaved={onSaved} />);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith("Test"));
  });

  // Test 16
  it("should show error on failed save", async () => {
    mockFetch.mockResolvedValue({ ok: false, text: () => Promise.resolve("Name required") });
    render(<ProfileStep defaultName="Test" onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    await waitFor(() => {
      expect(screen.getByText("Name required")).toBeInTheDocument();
    });
  });

  // Test 17
  it("should disable button while saving", async () => {
    let resolvePromise: (v: unknown) => void;
    mockFetch.mockReturnValue(new Promise((res) => { resolvePromise = res; }));
    render(<ProfileStep defaultName="Test" onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    await waitFor(() => expect(screen.getByText("Saving...")).toBeInTheDocument());
    resolvePromise!({ ok: true });
  });
});

// ===========================================================================
// VoiceStep
// ===========================================================================
describe("VoiceStep", () => {
  const defaultProps = {
    igAccountId: "acct-1",
    onSaved: vi.fn(),
    onBack: vi.fn(),
  };

  // Test 19
  it("should render heading", () => {
    render(<VoiceStep {...defaultProps} />);
    expect(screen.getByText("Define your voice")).toBeInTheDocument();
  });

  // Test 20
  it("should render a single persona field", () => {
    render(<VoiceStep {...defaultProps} />);
    expect(screen.getByPlaceholderText(/who are you/i)).toBeInTheDocument();
    expect(screen.getByText("Persona")).toBeInTheDocument();
  });

  // Test 21
  it("should render Back button", () => {
    render(<VoiceStep {...defaultProps} />);
    expect(screen.getByRole("button", { name: /back/i })).toBeInTheDocument();
  });

  // Test 22
  it("should call onBack when Back clicked", () => {
    const onBack = vi.fn();
    render(<VoiceStep {...defaultProps} onBack={onBack} />);
    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    expect(onBack).toHaveBeenCalled();
  });

  // Test 23
  it("should populate the persona from defaultPersona", () => {
    render(<VoiceStep {...defaultProps} defaultPersona="We are AI experts" />);
    expect(screen.getByDisplayValue("We are AI experts")).toBeInTheDocument();
  });

  // Test 24
  it("should PUT the persona and account id on submit", async () => {
    mockFetch.mockResolvedValue({ ok: true });
    render(<VoiceStep {...defaultProps} defaultPersona="Confident and direct" />);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith("/api/onboarding/voice", expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          igAccountId: "acct-1",
          persona: "Confident and direct",
        }),
      }));
    });
  });

  // Test 25
  it("should call onSaved after a successful save", async () => {
    mockFetch.mockResolvedValue({ ok: true });
    const onSaved = vi.fn();
    render(<VoiceStep {...defaultProps} defaultPersona="Confident" onSaved={onSaved} />);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith("Confident"));
  });

  // Test 26
  it("should show error on failed save", async () => {
    mockFetch.mockResolvedValue({ ok: false });
    render(<VoiceStep {...defaultProps} defaultPersona="Confident" />);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    await waitFor(() => {
      expect(screen.getByText("Failed to save voice config")).toBeInTheDocument();
    });
  });

  // Test 27
  it("should refuse to submit an empty persona", async () => {
    render(<VoiceStep {...defaultProps} />);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    await waitFor(() => {
      expect(
        screen.getByText("Describe your voice before continuing")
      ).toBeInTheDocument();
    });
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

// ===========================================================================
// LaunchStep
// ===========================================================================
describe("LaunchStep", () => {
  const defaultProps = {
    name: "Test User",
    accountCount: 2,
    hasPersona: true,
    hasTarget: true,
    igAccountId: "account-1",
    onBack: vi.fn(),
  };

  // Test 39
  it("should render heading", () => {
    render(<LaunchStep {...defaultProps} />);
    expect(screen.getByText("Ready to launch")).toBeInTheDocument();
  });

  // Test 40
  it("should display the user's name", () => {
    render(<LaunchStep {...defaultProps} />);
    expect(screen.getByText("Test User")).toBeInTheDocument();
  });

  // Test 41
  it("should display account count", () => {
    render(<LaunchStep {...defaultProps} />);
    expect(screen.getByText("2 accounts")).toBeInTheDocument();
  });

  // Test 42
  it("should display singular account", () => {
    render(<LaunchStep {...defaultProps} accountCount={1} />);
    expect(screen.getByText("1 account")).toBeInTheDocument();
  });

  // Test 43
  it("should display 'None yet' for 0 accounts", () => {
    render(<LaunchStep {...defaultProps} accountCount={0} />);
    expect(screen.getByText("None yet")).toBeInTheDocument();
  });

  // Test 44
  it("should display voice configured status", () => {
    render(<LaunchStep {...defaultProps} hasPersona={true} />);
    const voiceSummary = screen.getByText("Voice configured");
    expect(within(voiceSummary.parentElement!).getByText("Yes")).toBeInTheDocument();
  });

  // Test 45
  it("should display 'Not yet' when no persona", () => {
    render(<LaunchStep {...defaultProps} hasPersona={false} />);
    expect(screen.getByText("Not yet")).toBeInTheDocument();
  });

  it("keeps launch disabled until a target is enabled", () => {
    render(<LaunchStep {...defaultProps} hasTarget={false} />);

    expect(screen.getByText("Audience approved")).toBeInTheDocument();
    expect(screen.getAllByText("Not yet")).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: /launch engagemate/i }),
    ).toBeDisabled();
  });

  // Test 48
  it("should complete onboarding before navigating to the dashboard", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true });
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ enabled: true }),
    });
    render(<LaunchStep {...defaultProps} />);
    await waitFor(() => expect(screen.getByRole("button", { name: /launch engagemate/i })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /launch engagemate/i }));
    await waitFor(() =>
      expect(mockFetch).toHaveBeenCalledWith(
        "/api/onboarding/launch",
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"ig_account_id":"account-1"'),
        }),
      ),
    );
    expect(mockPush).toHaveBeenCalledWith("/dashboard");
  });

  // Test 49
  it("should stay on launch when completion fails", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true });
    mockFetch.mockResolvedValueOnce({
      ok: false,
      json: async () => ({ detail: "approve at least one target before launch" }),
    });
    render(<LaunchStep {...defaultProps} />);
    await waitFor(() => expect(screen.getByRole("button", { name: /launch engagemate/i })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /launch engagemate/i }));
    expect(
      await screen.findByText("approve at least one target before launch"),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "approve at least one target before launch",
    );
    expect(screen.getByRole("alert")).toHaveClass("text-destructive");
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("stays pending after launch succeeds until navigation unmounts it", async () => {
    let resolveLaunch!: (response: { ok: boolean }) => void;
    mockFetch.mockResolvedValueOnce({ ok: true });
    mockFetch.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveLaunch = resolve;
      }),
    );
    render(<LaunchStep {...defaultProps} />);

    await waitFor(() => expect(screen.getByRole("button", { name: /launch engagemate/i })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /launch engagemate/i }));
    expect(screen.getByRole("button", { name: "Launching…" })).toBeDisabled();

    resolveLaunch({ ok: true });

    await waitFor(() => expect(mockPush).toHaveBeenCalledWith("/dashboard"));
    expect(screen.getByRole("button", { name: "Launching…" })).toBeDisabled();
  });

  // Test 50
  it("should call onBack when Back clicked", () => {
    const onBack = vi.fn();
    render(<LaunchStep {...defaultProps} onBack={onBack} />);
    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    expect(onBack).toHaveBeenCalled();
  });
});

describe("hosted onboarding continuation", () => {
  it("uses refreshed owned health to disable Next without a reload", async () => {
    const account = { id: "acct-1", username: "demo_user", healthStatus: "active" };
    const { rerender } = render(<OnboardingWizard hostedLoginEnabled name="Sam"
      accounts={[account]} initialAccountId={account.id} persona="" initialStep={2} />);
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
    rerender(<OnboardingWizard hostedLoginEnabled name="Sam"
      accounts={[{ ...account, healthStatus: "paused" }]} initialAccountId={account.id} persona="" initialStep={2} />);
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.queryByText("Instagram connected")).toBeNull();
  });
  it("returns a paused account from Launch to its connection step on focus", async () => {
    const account = { id: "acct-1", username: "demo_user", healthStatus: "active" };
    render(<OnboardingWizard hostedLoginEnabled name="Sam" accounts={[account]}
      initialAccountId={account.id} persona="Warm" hasTarget initialStep={5} />);
    expect(screen.getByRole("button", { name: /launch engagemate/i })).toBeInTheDocument();
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [{ ...account, healthStatus: "paused" }] });
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    expect(await screen.findByLabelText("Instagram username")).toHaveValue("demo_user");
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /launch engagemate/i })).toBeNull();
  });
  it.each(["active", "paused"])("continues only a healthy confirmed account: %s", async (healthStatus) => {
    const opened = { opener: null, document: { title: "" }, closed: false,
      location: { replace: vi.fn() }, close: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(opened as unknown as Window);
    const id = "12345678-1234-1234-1234-123456789abc";
    mockFetch.mockImplementation(async (url: string, init?: RequestInit) => ({
      ok: true,
      json: async () => url === "/api/instagram/connect" && init?.method === "POST"
        ? { id, state: "active", expires_at: new Date(Date.now() + 600_000).toISOString() }
        : { id, state: "connected", account: { id: "acct-1", username: "demo_user", healthStatus } },
    }));
    try {
      render(<OnboardingWizard hostedLoginEnabled name="Sam" accounts={[]} persona="" initialStep={2} />);
      fireEvent.change(screen.getByLabelText("Instagram username"), { target: { value: "demo_user" } });
      fireEvent.click(screen.getByRole("button", { name: "Connect Instagram" }));
      if (healthStatus === "active") {
        expect(await screen.findByRole("heading", { name: "Let's understand your brand" })).toBeInTheDocument();
      } else {
        expect(await screen.findByText("Instagram sign-in saved")).toBeInTheDocument();
        expect(screen.getByText(/Automation remains paused/)).toBeInTheDocument();
        expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
        expect(screen.queryByRole("heading", { name: "Let's understand your brand" })).not.toBeInTheDocument();
      }
    } finally {
      open.mockRestore();
    }
  });
});
