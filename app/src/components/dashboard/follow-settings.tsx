"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { FollowReelFields } from "@/components/settings/follow-reel-fields";
import { DEFAULT_FOLLOW_REEL_MESSAGE, validFollowReelTemplate } from "@/lib/follow-reel-settings";
import { Button } from "@/components/ui/button";

export function FollowSettings({ accountId, enabled, dailyLimit, autoUnfollowEnabled, followReelMessageEnabled = false, followReelMessageTemplate = DEFAULT_FOLLOW_REEL_MESSAGE, onSaved, onBusy }: {
  accountId: string; enabled: boolean; dailyLimit: number; autoUnfollowEnabled: boolean;
  followReelMessageEnabled?: boolean; followReelMessageTemplate?: string;
  onSaved?: () => void; onBusy?: (busy: boolean) => void;
}) {
  const router = useRouter();
  const [autoUnfollow, setAutoUnfollow] = useState(autoUnfollowEnabled);
  const [reelEnabled, setReelEnabled] = useState(followReelMessageEnabled);
  const [reelTemplate, setReelTemplate] = useState(followReelMessageTemplate);
  const [checked, setChecked] = useState(enabled);
  const [limit, setLimit] = useState(String(dailyLimit));
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState(false);
  const valid = (!reelEnabled || validFollowReelTemplate(reelTemplate)) && /^\d+$/.test(limit) && Number(limit) >= 0 && Number(limit) <= 20;

  async function save() {
    if (saving || !valid) return;
    setSaving(true); onBusy?.(true); setMessage(""); setError(false);
    try {
      const response = await fetch(`/api/accounts/${encodeURIComponent(accountId)}/config`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ followAfterEngagement: checked, dailyFollowLimit: Number(limit), autoUnfollowEnabled: autoUnfollow, followReelMessageEnabled: reelEnabled, ...(validFollowReelTemplate(reelTemplate) ? {followReelMessageTemplate: reelTemplate} : {}) }),
      });
      if (!response.ok) throw new Error("save failed");
      setMessage("Follow settings saved."); onSaved?.(); router.refresh();
    } catch {
      setError(true); setMessage("Could not save follow settings. Try again.");
    } finally { setSaving(false); onBusy?.(false); }
  }

  return <div className="mt-4 rounded-lg border p-4 space-y-3">
    <label className="flex items-center gap-2 font-medium">
      <input type="checkbox" checked={checked} disabled={saving} onChange={(event) => { setChecked(event.target.checked); setMessage(""); }} />
      Follow after engagement
    </label>
    <p className="text-sm text-muted-foreground">
      Follow eligible new accounts before a Story reply or after a successful Like, optionally unfollowing 120 hours (5 days) after the confirmed follow.
      This applies to new engagements after you enable it. Accounts you already follow are excluded.
      Follow and unfollow actions are recorded separately from Story engagements.
    </p>
    <label className="flex items-center gap-2 font-medium">
      <input type="checkbox" checked={autoUnfollow} disabled={saving} onChange={(event) => { setAutoUnfollow(event.target.checked); setMessage(""); }} />
      Automatically unfollow after 5 days
    </label>
    <p className="text-sm text-muted-foreground">Turning this off keeps the accounts you follow and pauses scheduled unfollows. Turning it back on resumes them, including overdue unfollows.</p>
    <label className="block text-sm">Daily follow limit
      <input className="ml-3 w-20 rounded border p-2" type="number" min={0} max={20} step={1}
        value={limit} disabled={saving} onChange={(event) => { setLimit(event.target.value); setMessage(""); }} />
    </label>
    <p className="text-sm text-muted-foreground">Choose 0–20. Zero stops new follows. Turning off Follow after engagement stops new follows. Automatic unfollow has its own setting; you can also cancel an individual unfollow.</p>
    <fieldset disabled={saving}>
      <FollowReelFields enabled={reelEnabled} template={reelTemplate} following={checked} onEnabled={setReelEnabled} onTemplate={setReelTemplate} />
    </fieldset>
    {reelEnabled && !validFollowReelTemplate(reelTemplate) && <p role="alert" className="text-sm text-destructive">Write a message of 1–500 characters without links.</p>}
    <Button variant="outline" onClick={save} disabled={saving || !valid}>{saving ? "Saving…" : "Save follow settings"}</Button>
    {message && <p role={error ? "alert" : "status"} className="text-sm">{message}</p>}
  </div>;
}
