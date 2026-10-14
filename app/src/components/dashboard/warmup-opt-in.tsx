"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { WARMUP_BRACKETS, type ActionCaps } from "@/lib/warmup";

export function WarmupOptIn({ accountId, current, planDailyActions }: {
  accountId: string;
  current: ActionCaps;
  planDailyActions: number;
}) {
  const [reviewing, setReviewing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(false);
  const mature = WARMUP_BRACKETS[WARMUP_BRACKETS.length - 1].caps;
  const proposed = {
    like: Math.max(current.like, Math.min(mature.like, planDailyActions)),
    reply: Math.max(current.reply, Math.min(mature.reply, planDailyActions)),
  };
  const patch = {
    ...(proposed.like > current.like ? { dailyLikeLimit: proposed.like } : {}),
    ...(proposed.reply > current.reply ? { dailyReplyLimit: proposed.reply } : {}),
  };

  async function confirm() {
    if (saving) return;
    setSaving(true);
    setError(false);
    try {
      const response = await fetch(`/api/accounts/${accountId}/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!response.ok) throw new Error("limit update failed");
      setSaved(true);
    } catch {
      setError(true);
    } finally {
      setSaving(false);
    }
  }

  if (saved) return (
    <p role="status" className="mt-4 text-sm">
      Maximums saved. Refresh the dashboard to see today&apos;s allowance.
      This did not resume the agent or restart warm-up.
    </p>
  );
  if (Object.keys(patch).length === 0) return null;
  return (
    <div className="mt-4 space-y-3">
      {reviewing ? (
        <>
          <p className="text-sm">
            Daily maximums: {current.like} → {proposed.like} likes, {current.reply} → {proposed.reply} Story replies.
          </p>
          <p className="max-w-prose text-sm text-muted-foreground">
            This opts this account into higher limits, including Story replies.
            Warm-up and your plan&apos;s combined {planDailyActions}-action daily cap
            still apply. Higher daily limits can use
            your monthly allowance sooner. If warm-up is complete, today&apos;s allowance may increase
            now. This does not change Story view limits, restart warm-up, or resume
            a paused agent.
          </p>
          <div className="flex flex-wrap gap-3">
            <Button disabled={saving} onClick={confirm}>
              {saving ? "Saving…" : "Confirm higher limits"}
            </Button>
            <Button variant="outline" disabled={saving} onClick={() => setReviewing(false)}>
              Cancel
            </Button>
          </div>
        </>
      ) : (
        <Button variant="outline" onClick={() => setReviewing(true)}>
          Review higher warm-up limits
        </Button>
      )}
      {error && <p role="alert" className="text-sm text-destructive">Could not save limits. Check your connection and try again.</p>}
    </div>
  );
}
