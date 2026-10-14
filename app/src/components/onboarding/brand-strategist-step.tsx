"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";

import type {
  BrandAnalysis,
  BrandCandidate,
  OnboardingBrandProfile,
} from "@/lib/onboarding-profile";

interface BrandStrategistStepProps {
  igAccountId?: string;
  defaultReferenceUrls?: string[];
  initialProfile?: OnboardingBrandProfile | null;
  onTargetConfigured?: () => void;
  onSaved: (result: { persona: string; analysis: BrandAnalysis }) => void;
  onBack: () => void;
}

export const MIN_REFERENCE_ACCOUNTS = 3;
export const MAX_REFERENCE_ACCOUNTS = 5;
const POLL_BASE_DELAY_MS = 2_000;
const POLL_MAX_DELAY_MS = 30_000;
const POLL_PENDING_DELAY_MS = 3_000;
const MAX_TRANSIENT_POLL_FAILURES = 5;

function pollDelay(attempt: number) {
  return Math.min(POLL_BASE_DELAY_MS * 2 ** attempt, POLL_MAX_DELAY_MS);
}

function retryAfterMs(response: Response) {
  const value = response.headers?.get("retry-after");
  if (!value) return null;
  const seconds = Number(value);
  const delay = Number.isFinite(seconds)
    ? seconds * 1_000
    : Date.parse(value) - Date.now();
  if (!Number.isFinite(delay) || delay <= 0) return null;
  return delay;
}

function initialReferenceUrls(defaults: string[]) {
  const values = defaults.slice(0, MAX_REFERENCE_ACCOUNTS);
  while (values.length < MIN_REFERENCE_ACCOUNTS) values.push("");
  return values;
}

function referenceKey(value: string) {
  try {
    const parsed = new URL(value.trim());
    if (
      !["instagram.com", "www.instagram.com"].includes(
        parsed.hostname.toLowerCase(),
      )
    ) {
      return value.trim().toLowerCase().replace(/\/+$/, "");
    }
    return parsed.pathname.split("/").filter(Boolean)[0]?.toLowerCase() || "";
  } catch {
    return value.trim().toLowerCase().replace(/\/+$/, "");
  }
}

function uniqueReferenceUrls(values: string[]) {
  const seen = new Set<string>();
  return values.filter((value) => {
    const key = referenceKey(value);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function referenceUrl(value: string) {
  const trimmed = value.trim();
  try {
    const parsed = new URL(trimmed);
    if (
      ["instagram.com", "www.instagram.com"].includes(
        parsed.hostname.toLowerCase(),
      )
    ) {
      const handle = parsed.pathname.split("/").filter(Boolean)[0] || "";
      return `https://www.instagram.com/${handle.toLowerCase()}/`;
    }
  } catch {
    // A bare handle is the primary input shown by the onboarding UI.
  }
  return `https://www.instagram.com/${trimmed.replace(/^@/, "").toLowerCase()}/`;
}

const QUESTIONS = [
  ["brand", "What do you want people to know you for?"],
  [
    "audience",
    "Who do you want to attract? Include the kind of people you want replying to your Stories.",
  ],
  [
    "interests",
    "What subjects do you love talking about? These become the topics we open conversations with.",
  ],
  [
    "voice",
    "How should your replies sound? Share words, energy, or examples you want to emulate.",
  ],
  ["goals", "What would make this feel successful in your first week?"],
] as const;

export function BrandStrategistStep({
  igAccountId,
  defaultReferenceUrls = [],
  initialProfile = null,
  onTargetConfigured,
  onSaved,
  onBack,
}: BrandStrategistStepProps) {
  const [referenceUrls, setReferenceUrls] = useState<string[]>(
    initialReferenceUrls(initialProfile?.reference_urls ?? defaultReferenceUrls),
  );
  const [draftBase, setDraftBase] = useState(initialProfile?.reference_urls ?? defaultReferenceUrls);
  const [answers, setAnswers] = useState<Record<string, string>>(
    initialProfile?.answers ?? {},
  );
  const [analysis, setAnalysis] = useState<BrandAnalysis | null>(
    initialProfile?.analysis ?? null,
  );
  const [candidates, setCandidates] = useState<BrandCandidate[]>(
    initialProfile?.candidates ?? [],
  );
  const [profileId, setProfileId] = useState(initialProfile?.profile_id ?? "");
  const [profileStatus, setProfileStatus] = useState<
    OnboardingBrandProfile["status"]
  >(initialProfile?.status ?? "complete");
  const [saving, setSaving] = useState(false);
  const [serverDraftSaved, setServerDraftSaved] = useState(initialProfile?.status === "pending" && !initialProfile.analysis);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [pollingStopped, setPollingStopped] = useState(false);
  const [importAnnouncement, setImportAnnouncement] = useState("");
  const acceptedCandidates = candidates
    .filter((candidate) => candidate.accepted)
    .slice(0, 8);
  const selectableCandidateIds = new Set(
    acceptedCandidates
      .filter((candidate) => !candidate.imported)
      .map((candidate) => candidate.candidate_id),
  );
  const selectedCandidateIds = selected.filter((candidateId) =>
    selectableCandidateIds.has(candidateId),
  );
  const hasImportedTarget = candidates.some((candidate) => candidate.imported);
  const savedSourceUrls = referenceUrls.map((url) => url.trim()).filter(Boolean).map(referenceUrl);
  const validSources = savedSourceUrls.length >= MIN_REFERENCE_ACCOUNTS &&
    savedSourceUrls.length <= MAX_REFERENCE_ACCOUNTS &&
    new Set(savedSourceUrls).size === savedSourceUrls.length;

  useEffect(() => {
    if (!analysis || profileStatus !== "pending" || !igAccountId) return;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let inFlight = false;
    let transientFailures = 0;
    let stopped = false;

    const stopPolling = () => {
      stopped = true;
      setPollingStopped(true);
    };

    const schedule = (delay: number) => {
      if (cancelled || stopped || document.visibilityState === "hidden") return;
      timer = setTimeout(() => {
        timer = undefined;
        void poll();
      }, delay);
    };

    const poll = async () => {
      if (cancelled || stopped || inFlight) return;
      inFlight = true;
      controller = new AbortController();
      try {
        const response = await fetch(
          `/api/onboarding/brand/profile?details=false&ig_account_id=${encodeURIComponent(igAccountId)}`,
          {
            cache: "no-store",
            signal: controller.signal,
          },
        );
        const body = (await response.json().catch(() => ({}))) as Partial<OnboardingBrandProfile> & {
          error?: string;
          detail?: string;
        };
        if (!response.ok) {
          const message = body.error || body.detail || "Candidate discovery failed";
          if (![429].includes(response.status) && response.status < 500) {
            setError(message);
            stopPolling();
            return;
          }
          transientFailures += 1;
          setError(message);
          if (transientFailures >= MAX_TRANSIENT_POLL_FAILURES) {
            stopPolling();
            return;
          }
          schedule(
            retryAfterMs(response as Response) ?? pollDelay(transientFailures - 1),
          );
          return;
        }
        if (cancelled) return;
        if (
          !body.status ||
          !["pending", "complete", "failed"].includes(body.status) ||
          !Array.isArray(body.candidates)
        ) {
          throw new Error("Candidate discovery returned an invalid response");
        }
        transientFailures = 0;
        setError("");
        setProfileStatus(body.status);
        setCandidates(body.candidates);
        setSelected((current) =>
          current.filter((candidateId) =>
            body.candidates?.some(
              (candidate) =>
                candidate.candidate_id === candidateId && !candidate.imported,
            ),
          ),
        );
        if (body.status === "pending") {
          schedule(POLL_PENDING_DELAY_MS);
        }
      } catch (caught) {
        if (cancelled || controller.signal.aborted) return;
        transientFailures += 1;
        setError(
          caught instanceof Error
            ? caught.message
            : "Candidate discovery failed",
        );
        if (transientFailures < MAX_TRANSIENT_POLL_FAILURES) {
          schedule(pollDelay(transientFailures - 1));
        } else {
          stopPolling();
        }
      } finally {
        inFlight = false;
      }
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState === "hidden") {
        if (timer) clearTimeout(timer);
        timer = undefined;
        return;
      }
      if (!cancelled && !stopped && !timer && !inFlight) schedule(0);
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    void poll();
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      if (timer) clearTimeout(timer);
      controller?.abort();
    };
  }, [analysis, profileStatus, igAccountId]);

  const updateAnswer = (key: string, value: string) => {
    setServerDraftSaved(false);
    setAnswers((current) => ({ ...current, [key]: value }));
  };

  const draftKey = igAccountId ? `engagemate:source-draft:${igAccountId}` : null;
  useEffect(() => {
    if (!draftKey) return;
    try {
      const raw = window.localStorage.getItem(draftKey);
      if (!raw) return;
      const draft = JSON.parse(raw);
      const saved = initialProfile?.reference_urls ?? defaultReferenceUrls;
      if (JSON.stringify(draft.base) !== JSON.stringify(saved)) {
        window.localStorage.removeItem(draftKey);
        return;
      }
      if (!Array.isArray(draft.urls) || draft.urls.length > MAX_REFERENCE_ACCOUNTS ||
          !draft.urls.every((url: unknown) => typeof url === "string" && url.length <= 500)) return;
      setReferenceUrls(draft.urls);
      setAnalysis(null);
      setError("Restored unsaved source accounts. Analyze them to save your new direction.");
    } catch {
      // Storage may be unavailable in private browsing; editing still works.
    }
    // Restore once for the selected account; input changes must not rehydrate it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey]);

  const saveSourceDraft = (urls: string[]) => {
    setServerDraftSaved(false);
    setReferenceUrls(urls);
    if (!draftKey) return;
    try {
      window.localStorage.setItem(draftKey, JSON.stringify({
        urls, base: draftBase,
      }));
    } catch {
      // A browser storage failure does not prevent server-side saving.
    }
  };

  const updateReferenceUrl = (index: number, value: string) => {
    saveSourceDraft(referenceUrls.map((url, position) => position === index ? value : url));
  };

  const addReferenceUrl = () => {
    if (referenceUrls.length < MAX_REFERENCE_ACCOUNTS) saveSourceDraft([...referenceUrls, ""]);
  };

  const removeReferenceUrl = (index: number) => {
    if (referenceUrls.length > MIN_REFERENCE_ACCOUNTS) {
      saveSourceDraft(referenceUrls.filter((_, position) => position !== index));
    }
  };

  const analyze = async () => {
    const entered = uniqueReferenceUrls(
      referenceUrls.map((url) => url.trim()).filter(Boolean),
    );
    if (
      entered.length < MIN_REFERENCE_ACCOUNTS ||
      entered.length > MAX_REFERENCE_ACCOUNTS ||
      QUESTIONS.some(([key]) => !answers[key]?.trim())
    ) {
      setError(
        "Add 3 to 5 unique audience source accounts and answer each question before continuing.",
      );
      return;
    }
    if (!igAccountId) {
      setError("Connect Instagram before analyzing your direction.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const normalizedReferenceUrls = entered.map(referenceUrl);
      const saved = await fetch("/api/onboarding/brand/sources", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ig_account_id: igAccountId, reference_urls: normalizedReferenceUrls, answers }),
      });
      const draft = await saved.json();
      if (!saved.ok || !draft.profile_id || !draft.source_revision) {
        throw new Error(draft.error || draft.detail || "Could not save source accounts");
      }
      setProfileId(draft.profile_id);
      setServerDraftSaved(true);
      setProfileStatus("pending");
      setAnalysis(null);
      setCandidates([]);
      setSelected([]);
      setReferenceUrls(normalizedReferenceUrls);
      setDraftBase(normalizedReferenceUrls);
      if (draftKey) {
        try { window.localStorage.removeItem(draftKey); } catch { /* optional browser cache */ }
      }
      const captured = await fetch("/api/onboarding/profiles", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ig_account_id: igAccountId, usernames: normalizedReferenceUrls.map(referenceKey) }),
      });
      const capturedBody = await captured.json();
      if (!captured.ok || !Array.isArray(capturedBody.profiles)) {
        throw new Error(capturedBody.error || capturedBody.detail || "Source profiles are unavailable. Your source accounts are saved; retry analysis.");
      }
      const referenceProfiles = capturedBody.profiles;
      const response = await fetch("/api/onboarding/brand", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ig_account_id: igAccountId,
          reference_urls: normalizedReferenceUrls,
          reference_profiles: referenceProfiles,
          source_revision: draft.source_revision,
          answers,
        }),
      });
      const body = (await response.json()) as {
        error?: string;
        detail?: string;
        profile_id?: string;
        analysis?: BrandAnalysis;
        candidates?: BrandCandidate[];
        status?: OnboardingBrandProfile["status"];
      };
      if (!response.ok || !body.analysis) {
        throw new Error(body.error || body.detail || "Brand analysis failed");
      }
      if (draftKey) {
        try { window.localStorage.removeItem(draftKey); } catch { /* optional browser cache */ }
      }
      setReferenceUrls(normalizedReferenceUrls);
      setDraftBase(normalizedReferenceUrls);
      setAnalysis(body.analysis);
      setProfileId(body.profile_id || "");
      setCandidates(body.candidates || []);
      setProfileStatus(body.status || "complete");
      setSelected([]);
      setPollingStopped(false);
      setImportAnnouncement("");
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Brand analysis failed",
      );
    } finally {
      setSaving(false);
    }
  };

  if (analysis) {
    return (
      <div>
        <p className="text-sm font-medium uppercase tracking-[0.18em] text-violet-400">
          Brand strategist
        </p>
        <h2 className="mt-2 text-2xl font-bold">Here&apos;s what I learned</h2>
        <p className="mt-1 text-muted-foreground">
          Review the interpretation before we set your voice and discovery
          preferences.
        </p>
        <div className="mt-6 grid gap-4 sm:grid-cols-2">
          <div className="rounded-lg border p-4">
            <p className="text-xs uppercase text-muted-foreground">
              Your voice
            </p>
            <p className="mt-2 text-sm">{analysis.voice_summary}</p>
          </div>
          <div className="rounded-lg border p-4">
            <p className="text-xs uppercase text-muted-foreground">Audience</p>
            <p className="mt-2 text-sm">{analysis.audience_summary}</p>
          </div>
          <div className="rounded-lg border p-4">
            <p className="text-xs uppercase text-muted-foreground">
              Brand identity
            </p>
            <p className="mt-2 text-sm">{analysis.brand_identity}</p>
          </div>
          <div className="rounded-lg border p-4">
            <p className="text-xs uppercase text-muted-foreground">
              Content pillars
            </p>
            <p className="mt-2 text-sm">
              {analysis.content_pillars.join(" · ")}
            </p>
          </div>
          <div className="rounded-lg border p-4">
            <p className="text-xs uppercase text-muted-foreground">
              Conversation topics
            </p>
            <p className="mt-2 text-sm">
              {analysis.conversation_topics.join(" · ")}
            </p>
          </div>
          <div className="rounded-lg border p-4">
            <p className="text-xs uppercase text-muted-foreground">
              Search direction
            </p>
            <p className="mt-2 text-sm">{analysis.search_terms.join(" · ")}</p>
          </div>
        </div>
        <div className="mt-6 rounded-lg border p-4">
          <p className="text-sm font-medium">Your source audience</p>
          <p className="mt-2 text-sm">{savedSourceUrls.map((url) => `@${referenceKey(url)}`).join(", ")}</p>
          <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
            <li>Collect followers of these inspiration accounts.</li>
            <li>Exclude you, your existing followers, and accounts you follow.</li>
            <li>Check the remaining people for active Stories.</li>
            <li>Work through eligible Stories within your daily action limits.</li>
          </ol>
          <p className="mt-3 text-xs text-muted-foreground">
            Use this direction approves this source audience. Actions begin after launch;
            daily limits remain ceilings when there are too few eligible Stories.
          </p>
        </div>
        {(acceptedCandidates.length > 0 || profileStatus !== "complete") && (
        <div className="mt-6 rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-4">
          <p className="text-sm font-medium text-emerald-700 dark:text-emerald-300">
            Suggested public accounts
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            Candidates are suggestions only. They are filtered for public
            profiles, 500+ followers, engagement signals, and public US location
            signals. Nothing is liked or messaged automatically.
          </p>
          {acceptedCandidates.length > 0 ? (
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {acceptedCandidates.map((candidate) => (
                <label
                  key={candidate.username}
                  className="flex items-center justify-between rounded border px-3 py-2 text-sm"
                >
                  <span className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={
                        candidate.imported ||
                        selectedCandidateIds.includes(candidate.candidate_id)
                      }
                      disabled={candidate.imported}
                      onChange={(event) =>
                        setSelected((current) =>
                          event.target.checked
                            ? [...current, candidate.candidate_id]
                            : current.filter(
                                (id) => id !== candidate.candidate_id,
                              ),
                        )
                      }
                    />
                    @{candidate.username}
                  </span>
                  <span className="flex items-center gap-2 text-xs text-emerald-700 dark:text-emerald-300">
                    {candidate.imported && (
                      <span className="rounded bg-emerald-500/10 px-1.5 py-0.5">
                        Added
                      </span>
                    )}
                    <span>{candidate.score}/100</span>
                  </span>
                </label>
              ))}
            </div>
          ) : profileStatus === "complete" ? (
            <div className="mt-3 rounded border border-amber-500/30 bg-amber-500/5 p-3">
              <p className="text-sm text-amber-700 dark:text-amber-300">
                No suggested accounts passed the filters. Adjust your source
                accounts and try again.
              </p>
              <Button
                variant="outline"
                className="mt-3"
                onClick={() => setAnalysis(null)}
              >
                Adjust source accounts
              </Button>
            </div>
          ) : null}
          {((profileStatus === "pending" && !pollingStopped) ||
            (profileStatus === "complete" && acceptedCandidates.length > 0)) && (
            <p
              className="mt-3 text-xs text-muted-foreground"
              role="status"
              aria-live="polite"
            >
              {profileStatus === "pending"
                ? "Finding suggested accounts now. You can review your direction while this finishes."
                : "Suggested accounts are ready."}
            </p>
          )}
          {profileStatus === "failed" && (
            <p className="mt-3 text-xs text-amber-700 dark:text-amber-300" role="alert">
              Suggested accounts could not be refreshed. Revisit your answers to try again.
            </p>
          )}
          {acceptedCandidates.length > 0 &&
            (igAccountId ? (
              <Button
                variant="outline"
                className="mt-4"
                disabled={saving || selectedCandidateIds.length === 0}
              onClick={async () => {
                const candidateIds = selectedCandidateIds;
                setSaving(true);
                setError("");
                try {
                  const response = await fetch(
                    "/api/onboarding/brand/approve",
                    {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({
                        profile_id: profileId,
                        candidate_ids: candidateIds,
                        ig_account_id: igAccountId,
                      }),
                    },
                  );
                  if (!response.ok) {
                    const body = (await response.json().catch(() => ({}))) as {
                      error?: string;
                      detail?: string;
                    };
                    throw new Error(
                      body.error || body.detail || "Could not add those accounts",
                    );
                  }
                  setCandidates((current) =>
                    current.map((candidate) =>
                      candidateIds.includes(candidate.candidate_id)
                        ? { ...candidate, imported: true }
                        : candidate,
                    ),
                  );
                  setSelected((current) =>
                    current.filter((candidateId) => !candidateIds.includes(candidateId)),
                  );
                  setImportAnnouncement(
                    `Added ${candidateIds.length} ${
                      candidateIds.length === 1 ? "account" : "accounts"
                    } to your target list.`,
                  );
                  onTargetConfigured?.();
                } catch (caught) {
                  setError(
                    caught instanceof Error
                      ? caught.message
                      : "Could not add those accounts",
                  );
                } finally {
                  setSaving(false);
                }
              }}
            >
              Add selected to target list
              </Button>
            ) : (
              <p className="mt-3 text-xs text-muted-foreground">
                Connect an Instagram account in the next step before adding
                suggestions to its target list.
              </p>
            ))}
          {importAnnouncement && (
            <p
              className="mt-3 text-sm text-emerald-700 dark:text-emerald-300"
              role="status"
              aria-live="polite"
            >
              {importAnnouncement}
            </p>
          )}
        </div>
        )}
        {error && (
          <p className="mt-4 text-sm text-red-400" role="alert">
            {error}
          </p>
        )}
        <div className="mt-8 flex gap-3">
          <Button variant="outline" onClick={() => setAnalysis(null)}>
            Revisit answers
          </Button>
          <Button
            onClick={async () => {
              setSaving(true);
              setError("");
              try {
                if (validSources) {
                  const response = await fetch("/api/onboarding/brand/approve-sources", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      profile_id: profileId, ig_account_id: igAccountId,
                      reference_urls: savedSourceUrls,
                    }),
                  });
                  if (!response.ok) {
                    const body = await response.json().catch(() => ({}));
                    throw new Error(body.error || body.detail || "Could not approve source audience");
                  }
                  onTargetConfigured?.();
                }
                await onSaved({ persona: analysis.voice_summary, analysis });
              } catch (caught) {
                setError(caught instanceof Error ? caught.message : "Could not save direction");
              } finally {
                setSaving(false);
              }
            }}
            disabled={saving || !igAccountId || (!validSources && !hasImportedTarget) || profileStatus !== "complete"}
            variant="gradient"
          >
            Use this direction
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <p className="text-sm font-medium uppercase tracking-[0.18em] text-violet-400">
        Hermes strategist
      </p>
      <h2 className="mt-2 text-2xl font-bold">
        Let&apos;s understand your brand
      </h2>
      <p className="mt-1 text-muted-foreground">
        Add 3 to 5 public Instagram audience source accounts. EngageMate studies
        their public audiences to find people worth engaging. These are external
        sources, not your connected Instagram accounts managed in EngageMate.
      </p>
      <div className="mt-6 space-y-5">
        <div>
          <label className="text-sm font-medium">
            Audience source accounts
          </label>
          <p className="text-xs text-muted-foreground">
            Add {MIN_REFERENCE_ACCOUNTS} to {MAX_REFERENCE_ACCOUNTS} accounts.
            EngageMate balances suggestions across every source.
          </p>
          <div className="mt-2 space-y-2">
            {referenceUrls.map((url, index) => (
              <div key={index} className="flex gap-2">
                <input
                  value={url}
                  disabled={saving}
                  onChange={(event) =>
                    updateReferenceUrl(index, event.target.value)
                  }
                  placeholder="https://www.instagram.com/your-reference/"
                  aria-label={`Audience source account ${index + 1}`}
                  className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"
                />
                {referenceUrls.length > MIN_REFERENCE_ACCOUNTS && (
                  <Button
                    variant="outline"
                    type="button"
                    disabled={saving}
                    onClick={() => removeReferenceUrl(index)}
                    aria-label={`Remove audience source account ${index + 1}`}
                  >
                    Remove
                  </Button>
                )}
              </div>
            ))}
          </div>
          {referenceUrls.length < MAX_REFERENCE_ACCOUNTS && (
            <Button
              variant="outline"
              type="button"
              className="mt-2"
              disabled={saving}
              onClick={addReferenceUrl}
            >
              Add another account
            </Button>
          )}
        </div>
        {QUESTIONS.map(([key, question]) => (
          <div key={key}>
            <label className="text-sm font-medium">{question}</label>
            <textarea
              value={answers[key] || ""}
              disabled={saving}
              onChange={(event) => updateAnswer(key, event.target.value)}
              rows={3}
              maxLength={1000}
              className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"
            />
          </div>
        ))}
      </div>
      {serverDraftSaved && (
        <p className="mt-4 text-sm text-muted-foreground" role="status">
          Your source accounts and answers are saved. Continue analysis when profile capture is available.
        </p>
      )}
      {error && (
        <p className="mt-4 text-sm text-red-400" role="alert">
          {error}
        </p>
      )}
      <div className="mt-8 flex gap-3">
        <Button variant="outline" onClick={onBack}>
          Back
        </Button>
        <Button onClick={analyze} disabled={saving} variant="gradient">
          {saving ? "Hermes is studying it..." : "Analyze my direction"}
        </Button>
      </div>
    </div>
  );
}
