"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { BehaviorSettings, type BehaviorAllocations } from "@/components/settings/behavior-settings";
import { Button } from "@/components/ui/button";
import { launchOnboardingAccount } from "@/lib/onboarding-launch";

const ALLOCATION_FIELDS = [
  ["dailyViewLimit", "Story views"],
  ["dailyLikeLimit", "Likes"],
  ["dailyReplyLimit", "Replies"],
] as const;

type SavedAllocations = BehaviorAllocations & { planDailyActions?: number; planDailyViews?: number; igAccountId: string };

interface LaunchStepProps {
  name: string;
  accountCount: number;
  hasPersona: boolean;
  hasTarget: boolean;
  igAccountId?: string;
  onBack: () => void;
}

export function LaunchStep({
  name,
  accountCount,
  hasPersona,
  hasTarget,
  igAccountId,
  onBack,
}: LaunchStepProps) {
  const router = useRouter();
  const [pendingBehaviors, setPendingBehaviors] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState("");

  const [savedAllocations, setSavedAllocations] = useState<SavedAllocations | null>(null);
  const [allocationError, setAllocationError] = useState(false);
  const [allocationAttempt, setAllocationAttempt] = useState(0);
  const allocations = savedAllocations?.igAccountId === igAccountId ? savedAllocations : null;
  const allZero = allocations && ALLOCATION_FIELDS.every(([key]) => allocations[key] === 0);

  useEffect(() => {
    const controller = new AbortController();
    setSavedAllocations(null);
    setPendingBehaviors(false);
    setAllocationError(false);
    if (!igAccountId) return () => controller.abort();
    void (async () => {
      try {
        const response = await fetch(`/api/accounts/${encodeURIComponent(igAccountId)}/config`, {
          cache: "no-store", signal: controller.signal,
        });
        const body = await response.json();
        if (!response.ok || !body || body.igAccountId !== igAccountId ||
            typeof body.followAfterEngagement !== "boolean" || typeof body.autoUnfollowEnabled !== "boolean" || !Number.isInteger(body.dailyFollowLimit) || body.dailyFollowLimit < 0 || body.dailyFollowLimit > 20 ||
            (body.followReelMessageEnabled !== undefined && typeof body.followReelMessageEnabled !== "boolean") ||
            (body.followReelMessageTemplate !== undefined && typeof body.followReelMessageTemplate !== "string") ||
            !ALLOCATION_FIELDS.every(([key]) => Number.isInteger(body[key]) && body[key] >= 0)) {
          throw new Error("Saved allocations are unavailable");
        }
        if (!controller.signal.aborted) setSavedAllocations(body);
      } catch {
        if (!controller.signal.aborted) setAllocationError(true);
      }
    })();
    return () => controller.abort();
  }, [igAccountId, allocationAttempt]);

  const launch = async () => {
    if (!igAccountId) {
      setError("Connect Instagram before launch.");
      return;
    }
    if (!allocations || pendingBehaviors) return;
    setLaunching(true);
    setError("");
    try {
      await launchOnboardingAccount(igAccountId);
      router.push("/dashboard");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Launch failed");
      setLaunching(false);
    }
  };

  return (
    <div>
      <h2 className="text-2xl font-bold">Ready to launch</h2>
      <p className="mt-1 text-muted-foreground">
        Review your setup before launch. You can change these settings from the dashboard.
      </p>

      <div className="mt-8 space-y-4">
        <div className="rounded-lg border p-4">
          <p className="text-sm text-muted-foreground">Name</p>
          <p className="mt-1 font-medium">{name}</p>
        </div>
        <div className="rounded-lg border p-4">
          <p className="text-sm text-muted-foreground">Connected accounts</p>
          <p className="mt-1 font-medium">
            {accountCount > 0
              ? `${accountCount} account${accountCount > 1 ? "s" : ""}`
              : "None yet"}
          </p>
        </div>
        <div className="rounded-lg border p-4">
          <p className="text-sm text-muted-foreground">Voice configured</p>
          <p className="mt-1 font-medium">{hasPersona ? "Yes" : "Not yet"}</p>
        </div>
        <div className="rounded-lg border p-4">
          <p className="text-sm text-muted-foreground">Audience approved</p>
          <p className="mt-1 font-medium">{hasTarget ? "Yes" : "Not yet"}</p>
        </div>
      </div>

      <div className="mt-4 rounded-lg border p-4">
        <p className="font-medium">Saved daily allocations</p>
        <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {ALLOCATION_FIELDS.map(([key, label]) => (
            <div key={key}>
              <dt className="text-sm text-muted-foreground">{label}</dt>
              <dd className="mt-1 font-medium">{allocations ? allocations[key] : "—"}</dd>
            </div>
          ))}
        </dl>
        {allocationError ? (
          <div className="mt-3">
            <p role="alert" className="text-sm text-destructive">Could not load saved allocations. Retry before launch.</p>
            <Button variant="outline" className="mt-2" onClick={() => setAllocationAttempt((value) => value + 1)}>Retry allocations</Button>
          </div>
        ) : !allocations ? (
          <p role="status" className="mt-3 text-sm text-muted-foreground">
            {igAccountId ? "Loading saved allocations…" : "Connect an account to review its allocations."}
          </p>
        ) : (
          <p role="status" className="mt-3 text-sm text-muted-foreground">
            {allZero
              ? "All four allocations are zero. Launch will enable the account, but it cannot view, like, reply, or DM until you save higher limits."
              : "A zero allocation disables that action. Launch preserves these limits; warm-up and plan ceilings may reduce today’s allowance."}
          </p>
        )}
      </div>

      {allocations && igAccountId && <BehaviorSettings key={igAccountId} accountId={igAccountId} initial={allocations}
        planDailyActions={allocations.planDailyActions} planDailyViews={allocations.planDailyViews}
        disabled={launching} onPending={setPendingBehaviors} onSaved={() => setAllocationAttempt((value) => value + 1)} />}
      <p className="mt-4 text-sm text-muted-foreground">
        Warm-up starts when you launch.
      </p>
      {error && (
        <p className="mt-4 text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="mt-8 flex gap-3">
        <Button variant="outline" onClick={onBack}>
          Back
        </Button>
        <Button
          onClick={launch}
          disabled={pendingBehaviors || launching || !igAccountId || !hasPersona || !hasTarget || !allocations}
          variant="gradient"
        >
          {launching ? "Launching…" : "Launch EngageMate"}
        </Button>
      </div>
    </div>
  );
}
