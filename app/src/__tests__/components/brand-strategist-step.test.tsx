import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BrandStrategistStep } from "@/components/onboarding/brand-strategist-step";

const mockFetch = vi.fn();
global.fetch = mockFetch;

const capturedProfiles = [
      {
        username: "creatorone",
        fullName: "Creator One",
        biography: "Austin product builder",
        followersCount: 1200,
        followingCount: 75,
        postsCount: 40,
        isPrivate: false,
        isVerified: true,
        latestPosts: [
          { likesCount: 48, commentsCount: 3, caption: "A useful post" },
        ],
      },
      {
        username: "creatortwo",
        fullName: "Creator Two",
        biography: "Operator in Chicago",
        followersCount: 1800,
        followingCount: 90,
        postsCount: 55,
        isPrivate: false,
        isVerified: false,
        latestPosts: [],
      },
      {
        username: "creatorthree",
        fullName: "Creator Three",
        biography: "Founder in New York",
        followersCount: 2400,
        followingCount: 110,
        postsCount: 70,
        isPrivate: false,
        isVerified: false,
        latestPosts: [],
      },
    ];
const savedDraft = { profile_id: "profile-1", source_revision: "2026-09-13T09:00:00+00:00" };

const props = {
  onSaved: vi.fn(),
  onBack: vi.fn(),
};

const analysis = {
  voice_summary: "Direct and useful",
  audience_summary: "Founders and operators",
  brand_identity: "Builder",
  engagement_style: "Thoughtful",
  content_pillars: ["AI"],
  conversation_topics: ["Deployment"],
  search_terms: ["AI founders"],
  us_audience_signals: ["United States"],
  next_question: "What are you building?",
};

function profile(
  status: "pending" | "complete" | "failed",
  candidates: Array<{
    candidate_id: string;
    username: string;
    score: number;
    accepted: boolean;
    imported: boolean;
    us_signal: boolean;
    engagement_rate: number;
    reasons: string[];
  }> = [],
) {
  return {
    profile_id: "profile-1",
    ig_account_id: "account-1",
    status,
    reference_urls: ["one", "two", "three"],
    answers: {},
    analysis,
    candidates,
  };
}

async function flushPoll() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function fillAnswers(container: HTMLElement) {
  for (const textarea of Array.from(container.querySelectorAll("textarea"))) {
    fireEvent.change(textarea, { target: { value: "A specific answer" } });
  }
}

describe("BrandStrategistStep audience sources", () => {
  beforeEach(() => {
    const storage = new Map<string, string>();
    Object.defineProperty(window, "localStorage", { configurable: true, value: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
      removeItem: (key: string) => storage.delete(key),
      clear: () => storage.clear(),
    } });
    vi.clearAllMocks();
    mockFetch.mockReset();

  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts with three audience source inputs", () => {
    render(<BrandStrategistStep {...props} />);

    expect(screen.getAllByLabelText(/Audience source account \d/)).toHaveLength(3);
  });

  it("explains the difference between audience sources and connected accounts", () => {
    render(<BrandStrategistStep {...props} />);

    expect(screen.getByText("Audience source accounts")).toBeInTheDocument();
    expect(screen.getByText(/your connected Instagram accounts/i)).toBeInTheDocument();
  });

  it("rejects fewer than three filled audience sources", async () => {
    const { container } = render(<BrandStrategistStep {...props} />);
    const inputs = screen.getAllByLabelText(/Audience source account \d/);
    fireEvent.change(inputs[0], {
      target: { value: "https://www.instagram.com/alpha/" },
    });
    fireEvent.change(inputs[1], {
      target: { value: "https://www.instagram.com/beta/" },
    });
    fillAnswers(container);

    fireEvent.click(screen.getByRole("button", { name: "Analyze my direction" }));

    await waitFor(() =>
      expect(
        screen.getByText(/Add 3 to 5 unique audience source accounts/i),
      ).toBeInTheDocument(),
    );
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("deduplicates canonically equivalent source URLs", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => savedDraft });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ profiles: capturedProfiles }) });
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ profile_id: "profile-1", analysis, candidates: [] }),
    });
    const { container } = render(
      <BrandStrategistStep {...props} igAccountId="account-1" />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Add another account" }));
    const inputs = screen.getAllByLabelText(/Audience source account \d/);
    fireEvent.change(inputs[0], {
      target: { value: "https://instagram.com/Creator/" },
    });
    fireEvent.change(inputs[1], {
      target: { value: "https://www.instagram.com/creator/?utm_source=test" },
    });
    fireEvent.change(inputs[2], {
      target: { value: "https://www.instagram.com/another/" },
    });
    fireEvent.change(inputs[3], {
      target: { value: "https://www.instagram.com/third/" },
    });
    fillAnswers(container);

    fireEvent.click(screen.getByRole("button", { name: "Analyze my direction" }));

    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    expect(JSON.parse(mockFetch.mock.calls[0][1].body).reference_urls).toEqual([
      "https://www.instagram.com/creator/",
      "https://www.instagram.com/another/",
      "https://www.instagram.com/third/",
    ]);
  });

  it("normalizes bare handles before requesting brand analysis", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => savedDraft });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ profiles: capturedProfiles }) });
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        profile_id: "profile-1",
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
        candidates: [],
      }),
    });
    const { container } = render(
      <BrandStrategistStep {...props} igAccountId="account-1" />,
    );
    const inputs = screen.getAllByLabelText(/Audience source account \d/);
    fireEvent.change(inputs[0], { target: { value: "CreatorOne" } });
    fireEvent.change(inputs[1], { target: { value: "@CreatorTwo" } });
    fireEvent.change(inputs[2], {
      target: { value: "https://instagram.com/CreatorThree/?utm_source=test" },
    });
    fillAnswers(container);

    fireEvent.click(screen.getByRole("button", { name: "Analyze my direction" }));

    await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(3));
    expect(screen.getByText("Brand strategist")).toBeInTheDocument();
    expect(screen.queryByText(/Hermes strategist/i)).not.toBeInTheDocument();
    expect(mockFetch.mock.calls.map(([url]) => url)).toEqual([
      "/api/onboarding/brand/sources", "/api/onboarding/profiles", "/api/onboarding/brand",
    ]);
    expect(JSON.parse(mockFetch.mock.calls[1][1].body)).toEqual({
      ig_account_id: "account-1", usernames: ["creatorone", "creatortwo", "creatorthree"],
    });
    const request = mockFetch.mock.calls[2][1] as RequestInit;
    const body = JSON.parse(String(request.body));
    expect(body.ig_account_id).toBe("account-1");
    expect(body.reference_urls).toEqual([
      "https://www.instagram.com/creatorone/",
      "https://www.instagram.com/creatortwo/",
      "https://www.instagram.com/creatorthree/",
    ]);
    expect(body.reference_profiles.map((profile: { username: string }) => profile.username)).toEqual([
      "creatorone",
      "creatortwo",
      "creatorthree",
    ]);
    expect(body.answers).toEqual({
      brand: "A specific answer",
      audience: "A specific answer",
      interests: "A specific answer",
      voice: "A specific answer",
      goals: "A specific answer",
    });
  });

  it("requires the connected Instagram account before profile analysis", async () => {
    const { container } = render(<BrandStrategistStep {...props} />);
    const inputs = screen.getAllByLabelText(/Audience source account \d/);
    ["one", "two", "three"].forEach((handle, index) => {
      fireEvent.change(inputs[index], { target: { value: handle } });
    });
    fillAnswers(container);

    fireEvent.click(screen.getByRole("button", { name: "Analyze my direction" }));

    expect(
      await screen.findByText(/Connect Instagram before analyzing your direction/i),
    ).toBeInTheDocument();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("keeps saved sources when the proxy cannot return source profiles", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => savedDraft });
    mockFetch.mockResolvedValueOnce({ ok: false, json: async () => ({ detail: "Instagram temporarily limited profile capture" }) });
    const onSaved = vi.fn();
    const { container } = render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        onSaved={onSaved}
      />,
    );
    const inputs = screen.getAllByLabelText(/Audience source account \d/);
    ["one", "two", "three"].forEach((handle, index) => {
      fireEvent.change(inputs[index], { target: { value: handle } });
    });
    fillAnswers(container);

    fireEvent.click(screen.getByRole("button", { name: "Analyze my direction" }));

    expect(
      await screen.findByText(/Instagram temporarily limited profile capture/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Hermes strategist/i)).toBeInTheDocument();
    expect(screen.getByText(/Your source accounts and answers are saved/i)).toBeInTheDocument();
    expect(screen.queryByText("Brand strategist")).not.toBeInTheDocument();
    expect(mockFetch.mock.calls.map(([url]) => url)).toEqual([
      "/api/onboarding/brand/sources", "/api/onboarding/profiles",
    ]);
    expect(JSON.parse(mockFetch.mock.calls[0][1].body).reference_urls).toEqual([
      "https://www.instagram.com/one/", "https://www.instagram.com/two/", "https://www.instagram.com/three/",
    ]);
    expect(window.localStorage.getItem("engagemate:source-draft:account-1")).toBeNull();
    expect(onSaved).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Analyze my direction" }),
    ).toBeEnabled();
  });

  it("shows API validation detail", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      json: async () => ({ detail: "reference URL must point to an Instagram profile" }),
    });
    const { container } = render(
      <BrandStrategistStep {...props} igAccountId="account-1" />,
    );
    const inputs = screen.getAllByLabelText(/Audience source account \d/);
    ["one", "two", "three"].forEach((handle, index) => {
      fireEvent.change(inputs[index], { target: { value: handle } });
    });
    fillAnswers(container);

    fireEvent.click(screen.getByRole("button", { name: "Analyze my direction" }));

    expect(
      await screen.findByText("reference URL must point to an Instagram profile"),
    ).toBeInTheDocument();
  });

  it("restores saved references and answers after a reload", () => {
    const { container } = render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={{
          profile_id: "profile-1",
          ig_account_id: "account-1",
          status: "complete",
          reference_urls: [
            "https://www.instagram.com/creatorone/",
            "https://www.instagram.com/creatortwo/",
            "https://www.instagram.com/creatorthree/",
          ],
          answers: {
            brand: "Practical AI deployment",
            audience: "Enterprise operators",
            interests: "Agents and infrastructure",
            voice: "Direct and warm",
            goals: "Useful conversations",
          },
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
          candidates: [],
        }}
      />,
    );

    expect(screen.getByText("Brand strategist")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Revisit answers" }));

    expect(screen.getAllByLabelText(/Audience source account \d/)).toHaveLength(3);
    expect(screen.getByDisplayValue("Practical AI deployment")).toBeInTheDocument();
    expect(container.querySelectorAll("textarea")).toHaveLength(5);
  });

  it("restores a durable pending source draft without polling or stale analysis", () => {
    render(<BrandStrategistStep {...props} igAccountId="account-1" initialProfile={{
      ...profile("pending"), analysis: null, answers: { audience: "A saved audience" },
    }} />);
    expect(screen.getByLabelText("Audience source account 1")).toHaveValue("one");
    expect(screen.getByText(/Your source accounts and answers are saved/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Use this direction" })).not.toBeInTheDocument();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("restores an unsaved source draft after reload and requires fresh analysis", () => {
    const saved = profile("complete");
    const first = render(<BrandStrategistStep {...props} igAccountId="account-1" initialProfile={saved} />);
    fireEvent.click(screen.getByRole("button", { name: "Revisit answers" }));
    fireEvent.change(screen.getByLabelText("Audience source account 1"), { target: { value: "replacement" } });
    first.unmount();
    render(<BrandStrategistStep {...props} igAccountId="account-1" initialProfile={saved} />);
    expect(screen.getByLabelText("Audience source account 1")).toHaveValue("replacement");
    expect(screen.getByRole("alert")).toHaveTextContent("Restored unsaved source accounts");
    expect(screen.queryByRole("button", { name: "Use this direction" })).not.toBeInTheDocument();
  });

  it("discards a stale draft when the saved account sources changed elsewhere", () => {
    window.localStorage.setItem("engagemate:source-draft:account-1", JSON.stringify({
      urls: ["stale", "two", "three"], base: ["old", "two", "three"],
    }));
    render(<BrandStrategistStep {...props} igAccountId="account-1" initialProfile={profile("complete")} />);
    expect(screen.getByRole("button", { name: "Use this direction" })).toBeEnabled();
    expect(window.localStorage.getItem("engagemate:source-draft:account-1")).toBeNull();
  });

  it("polls pending discovery and shows candidates when they arrive", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        profile_id: "profile-1",
        status: "complete",
        reference_urls: [
          "https://www.instagram.com/creatorone/",
          "https://www.instagram.com/creatortwo/",
          "https://www.instagram.com/creatorthree/",
        ],
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
        candidates: [
          {
            candidate_id: "candidate-1",
            username: "exampletechinc",
            score: 92,
            accepted: true,
            imported: false,
            us_signal: true,
            engagement_rate: 0.04,
            reasons: ["Audience match"],
          },
        ],
      }),
    });

    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={{
          profile_id: "profile-1",
          ig_account_id: "account-1",
          status: "pending",
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
          candidates: [],
        }}
      />,
    );

    expect(screen.getByText(/Finding suggested accounts/i)).toBeInTheDocument();
    expect(await screen.findByText("@exampletechinc")).toBeInTheDocument();
    expect(mockFetch).toHaveBeenCalledWith(
      "/api/onboarding/brand/profile?details=false&ig_account_id=account-1",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("scopes polling to the active Instagram account", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => profile("pending"),
    });
    const rendered = render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("pending")}
      />,
    );

    await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(1));
    expect(mockFetch.mock.calls[0][0]).toBe(
      "/api/onboarding/brand/profile?details=false&ig_account_id=account-1",
    );

    rendered.rerender(
      <BrandStrategistStep
        {...props}
        igAccountId="account-2"
        initialProfile={profile("pending")}
      />,
    );

    await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(2));
    expect(mockFetch.mock.calls[1][0]).toBe(
      "/api/onboarding/brand/profile?details=false&ig_account_id=account-2",
    );
  });

  it("polls healthy pending responses every three seconds", async () => {
    vi.useFakeTimers();
    mockFetch
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => profile("pending") })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => profile("pending") })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => profile("complete") });

    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("pending")}
      />,
    );
    await flushPoll();
    expect(mockFetch).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(2_999);
    });
    expect(mockFetch).toHaveBeenCalledTimes(1);
    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    await flushPoll();
    expect(mockFetch).toHaveBeenCalledTimes(2);

    await act(async () => {
      vi.advanceTimersByTime(2_999);
    });
    expect(mockFetch).toHaveBeenCalledTimes(2);
    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    await flushPoll();
    expect(mockFetch).toHaveBeenCalledTimes(3);
  });

  it("honors Retry-After values longer than the local backoff cap", async () => {
    vi.useFakeTimers();
    mockFetch
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: "Rate limited" }), {
          status: 429,
          headers: { "Retry-After": "120" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(profile("complete")), { status: 200 }),
      );

    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("pending")}
      />,
    );
    await flushPoll();
    expect(mockFetch).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(119_999);
    });
    expect(mockFetch).toHaveBeenCalledTimes(1);
    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    await flushPoll();
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it("honors a future Retry-After HTTP-date", async () => {
    vi.useFakeTimers();
    const now = new Date("2026-09-05T12:00:00Z");
    vi.setSystemTime(now);
    mockFetch
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: "Rate limited" }), {
          status: 429,
          headers: {
            "Retry-After": new Date(now.getTime() + 10_000).toUTCString(),
          },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(profile("complete")), { status: 200 }),
      );

    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("pending")}
      />,
    );
    await flushPoll();
    expect(mockFetch).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(9_999);
    });
    expect(mockFetch).toHaveBeenCalledTimes(1);
    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    await flushPoll();
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["invalid", "not-a-date"],
    ["past", new Date("2026-09-05T11:59:59Z").toUTCString()],
  ])(
    "falls back to local backoff for a %s Retry-After HTTP-date",
    async (_case, retryAfter) => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-09-05T12:00:00Z"));
      mockFetch
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ error: "Rate limited" }), {
            status: 429,
            headers: { "Retry-After": retryAfter },
          }),
        )
        .mockResolvedValueOnce(
          new Response(JSON.stringify(profile("complete")), { status: 200 }),
        );

      render(
        <BrandStrategistStep
          {...props}
          igAccountId="account-1"
          initialProfile={profile("pending")}
        />,
      );
      await flushPoll();
      expect(mockFetch).toHaveBeenCalledTimes(1);

      await act(async () => {
        vi.advanceTimersByTime(1_999);
      });
      expect(mockFetch).toHaveBeenCalledTimes(1);
      await act(async () => {
        vi.advanceTimersByTime(1);
      });
      await flushPoll();
      expect(mockFetch).toHaveBeenCalledTimes(2);
    },
  );

  it("stops polling on terminal authentication failures", async () => {
    vi.useFakeTimers();
    mockFetch.mockResolvedValue(
      new Response(JSON.stringify({ error: "Sign in again" }), { status: 401 }),
    );

    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("pending")}
      />,
    );
    await flushPoll();
    expect(screen.getByRole("alert")).toHaveTextContent("Sign in again");

    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("does not restart terminally stopped polling when the tab becomes visible", async () => {
    vi.useFakeTimers();
    let visibilityState: DocumentVisibilityState = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(
      () => visibilityState,
    );
    mockFetch.mockResolvedValue(
      new Response(JSON.stringify({ error: "Sign in again" }), { status: 401 }),
    );

    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("pending")}
      />,
    );
    await flushPoll();
    expect(screen.getByRole("alert")).toHaveTextContent("Sign in again");
    expect(screen.queryByText(/Finding suggested accounts now/i)).not.toBeInTheDocument();

    visibilityState = "hidden";
    fireEvent(document, new Event("visibilitychange"));
    visibilityState = "visible";
    fireEvent(document, new Event("visibilitychange"));
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    await flushPoll();

    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("stops retrying after five transient poll failures", async () => {
    vi.useFakeTimers();
    mockFetch.mockRejectedValue(new TypeError("network unavailable"));

    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("pending")}
      />,
    );
    await flushPoll();

    for (const delay of [2_000, 4_000, 8_000, 16_000]) {
      await act(async () => {
        vi.advanceTimersByTime(delay);
      });
      await flushPoll();
    }
    expect(mockFetch).toHaveBeenCalledTimes(5);

    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    expect(mockFetch).toHaveBeenCalledTimes(5);
    expect(screen.getByRole("alert")).toHaveTextContent("network unavailable");
  });

  it("aborts an in-flight poll and prevents late state updates when unmounted", async () => {
    vi.useFakeTimers();
    let resolveFetch: ((value: Response) => void) | undefined;
    mockFetch.mockImplementationOnce(() => new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    }));
    const rendered = render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("pending")}
      />,
    );
    await flushPoll();
    const options = mockFetch.mock.calls[0][1] as RequestInit;

    rendered.unmount();
    expect(options.signal?.aborted).toBe(true);

    resolveFetch?.(
      new Response(JSON.stringify(profile("complete", [{
        candidate_id: "late-candidate",
        username: "latecandidate",
        score: 90,
        accepted: true,
        imported: false,
        us_signal: true,
        engagement_rate: 0.04,
        reasons: ["late"],
      }])), { status: 200 }),
    );
    await flushPoll();
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("clears a scheduled poll while hidden and resumes once visible", async () => {
    vi.useFakeTimers();
    let visibilityState: DocumentVisibilityState = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(
      () => visibilityState,
    );
    mockFetch
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => profile("pending"),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => profile("complete"),
      });

    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("pending")}
      />,
    );
    await flushPoll();
    visibilityState = "hidden";
    fireEvent(document, new Event("visibilitychange"));

    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    expect(mockFetch).toHaveBeenCalledTimes(1);

    visibilityState = "visible";
    fireEvent(document, new Event("visibilitychange"));
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    await flushPoll();
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it("approves source followers before advancing without individual targets", async () => {
    const onSaved = vi.fn();
    const onTargetConfigured = vi.fn();
    mockFetch.mockResolvedValueOnce(new Response('{"approved":true}', { status: 200 }));
    render(<BrandStrategistStep {...props} igAccountId="account-1"
      initialProfile={profile("complete")} onSaved={onSaved}
      onTargetConfigured={onTargetConfigured} />);
    fireEvent.click(screen.getByRole("button", { name: "Use this direction" }));
    expect(onSaved).not.toHaveBeenCalled();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(onTargetConfigured).toHaveBeenCalledOnce();
    const [url, request] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/onboarding/brand/approve-sources");
    expect(JSON.parse(request.body)).toEqual({
      profile_id: "profile-1", ig_account_id: "account-1",
      reference_urls: ["one", "two", "three"].map((u) => `https://www.instagram.com/${u}/`),
    });
  });

  it("does not advance when source approval fails", async () => {
    const onSaved = vi.fn();
    mockFetch.mockResolvedValueOnce(new Response('{"detail":"source accounts changed"}', { status: 422 }));
    render(<BrandStrategistStep {...props} igAccountId="account-1"
      initialProfile={profile("complete")} onSaved={onSaved} />);
    fireEvent.click(screen.getByRole("button", { name: "Use this direction" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("source accounts changed");
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("allows source approval when no manual candidates pass filters", () => {
    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("complete", [{
          candidate_id: "candidate-1",
          username: "filteredout",
          score: 41,
          accepted: false,
          imported: false,
          us_signal: false,
          engagement_rate: 0.01,
          reasons: ["Below threshold"],
        }])}
      />,
    );

    expect(screen.getByText("Your source audience")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use this direction" })).toBeEnabled();
    expect(screen.queryByText("Suggested accounts are ready.")).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Add at least one suggested account/i),
    ).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Revisit answers" }),
    );

    expect(screen.getByText("Audience source accounts")).toBeInTheDocument();
    expect(screen.getAllByLabelText(/Audience source account \d/)).toHaveLength(3);
  });

  it("uses theme-safe colors for candidate and warning statuses", () => {
    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("complete", [{
          candidate_id: "candidate-1",
          username: "exampletechinc",
          score: 92,
          accepted: true,
          imported: false,
          us_signal: true,
          engagement_rate: 0.04,
          reasons: ["Audience match"],
        }])}
      />,
    );

    expect(screen.getByText("Suggested public accounts")).toHaveClass(
      "text-emerald-700",
      "dark:text-emerald-300",
    );
    expect(screen.getByText("92/100").parentElement).toHaveClass(
      "text-emerald-700",
      "dark:text-emerald-300",
    );

  });

  it("reports a configured target after candidate import succeeds", async () => {
    const onTargetConfigured = vi.fn();
    mockFetch.mockResolvedValueOnce(new Response("{}", { status: 200 }));
    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("complete", [{
          candidate_id: "candidate-1",
          username: "exampletechinc",
          score: 92,
          accepted: true,
          imported: false,
          us_signal: true,
          engagement_rate: 0.04,
          reasons: ["Audience match"],
        }])}
        onTargetConfigured={onTargetConfigured}
      />,
    );

    fireEvent.click(screen.getByRole("checkbox", { name: /exampletechinc/i }));
    fireEvent.click(
      screen.getByRole("button", { name: "Add selected to target list" }),
    );

    await waitFor(() => expect(onTargetConfigured).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: "Use this direction" })).toBeEnabled();
    const announcement = screen.getByText("Added 1 account to your target list.");
    expect(announcement).toHaveAttribute("role", "status");
    expect(announcement).toHaveAttribute("aria-live", "polite");
  });

  it("shows candidate approval detail returned by the server", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ detail: "Candidate no longer meets the import rules" }),
        { status: 422, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={profile("complete", [{
          candidate_id: "candidate-1",
          username: "exampletechinc",
          score: 92,
          accepted: true,
          imported: false,
          us_signal: true,
          engagement_rate: 0.04,
          reasons: ["Audience match"],
        }])}
      />,
    );

    fireEvent.click(screen.getByRole("checkbox", { name: /exampletechinc/i }));
    fireEvent.click(
      screen.getByRole("button", { name: "Add selected to target list" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Candidate no longer meets the import rules",
    );
  });

  it("renders imported candidates as non-interactive added rows", () => {
    render(
      <BrandStrategistStep
        {...props}
        igAccountId="account-1"
        initialProfile={{
          profile_id: "profile-1",
          ig_account_id: "account-1",
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
          candidates: [
            {
              candidate_id: "candidate-1",
              username: "exampletechinc",
              score: 92,
              accepted: true,
              imported: true,
              us_signal: true,
              engagement_rate: 0.04,
              reasons: ["Audience match"],
            },
          ],
        }}
      />,
    );

    expect(screen.getByRole("checkbox", { name: /exampletechinc/i })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /exampletechinc/i })).toBeDisabled();
    expect(screen.getByText("Added")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add selected to target list" })).toBeDisabled();
  });
});
