"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { FollowReelFields } from "@/components/settings/follow-reel-fields";
import { DEFAULT_FOLLOW_REEL_MESSAGE, validFollowReelTemplate } from "@/lib/follow-reel-settings";
import { Button } from "@/components/ui/button";

const ACTIONS = [
  ["dailyViewLimit", "View Stories", "Story views"],
  ["dailyLikeLimit", "Like Stories", "Story likes"],
  ["dailyReplyLimit", "Reply to Stories", "Story replies"],
] as const;
type ActionKey = (typeof ACTIONS)[number][0];
export type BehaviorAllocations = Record<ActionKey, number> & {
  dailyDmLimit?: number;
  dailyReelMessageLimit?: number | null;
  warmupExempt?: boolean;
  followAfterEngagement: boolean;
  dailyFollowLimit: number;
  autoUnfollowEnabled: boolean;
  followReelMessageEnabled?: boolean;
  followReelMessageTemplate?: string;
};

export function BehaviorSettings({ accountId, initial, disabled = false, planDailyActions, planDailyViews, onPending, onSaved }: {
  accountId: string;
  initial: BehaviorAllocations;
  disabled?: boolean;
  planDailyActions?: number;
  planDailyViews?: number;
  onPending?: (pending: boolean) => void;
  onSaved?: () => void;
}) {
  const router = useRouter();
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const [enabled, setEnabled] = useState(() => Object.fromEntries(ACTIONS.map(([key]) => [key, initial[key] > 0])) as Record<ActionKey, boolean>);
  const [limits, setLimits] = useState(() => Object.fromEntries(ACTIONS.map(([key]) => [key, String(initial[key] || 1)])) as Record<ActionKey, string>);
  const [follow, setFollow] = useState(initial.followAfterEngagement);
  const [followLimit, setFollowLimit] = useState(String(initial.dailyFollowLimit || 1));
  const [unfollow, setUnfollow] = useState(initial.autoUnfollowEnabled);
  const [reelEnabled, setReelEnabled] = useState(initial.followReelMessageEnabled ?? false);
  const [reelTemplate, setReelTemplate] = useState(initial.followReelMessageTemplate ?? DEFAULT_FOLLOW_REEL_MESSAGE);
  const [reelLimit, setReelLimit] = useState(initial.dailyReelMessageLimit == null ? (initial.dailyReplyLimit === 0 ? "1" : "") : String(initial.dailyReelMessageLimit));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const positive = (value: string) => /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) > 0;
  const ceiling = (key: ActionKey) => key === "dailyViewLimit" ? planDailyViews : planDailyActions;
  const validReelLimit = reelLimit === "" || (/^\d+$/.test(reelLimit) && Number(reelLimit) <= Math.min(20, planDailyActions ?? 20));
  const valid = (!reelEnabled || (validFollowReelTemplate(reelTemplate) && validReelLimit)) && ACTIONS.every(([key]) => !enabled[key] || (positive(limits[key]) && (ceiling(key) === undefined || Number(limits[key]) <= ceiling(key)!))) && (!follow || (positive(followLimit) && Number(followLimit) <= 20 && (enabled.dailyLikeLimit || enabled.dailyReplyLimit)));
  const changed = () => { setDirty(true); setSaved(false); setError(""); onPending?.(true); };

  async function save() {
    if (disabled || saving || !dirty || !valid) return;
    setSaving(true); setError("");
    const patch = {
      ...Object.fromEntries(ACTIONS.map(([key]) => [key, enabled[key] ? Number(limits[key]) : 0])),
      dailyDmLimit: 0,
      ...(validReelLimit ? {dailyReelMessageLimit: reelLimit === "" ? null : Number(reelLimit)} : {}),
      followAfterEngagement: follow,
      dailyFollowLimit: follow ? Number(followLimit) : 0,
      autoUnfollowEnabled: unfollow,
      followReelMessageEnabled: reelEnabled,
      ...(validFollowReelTemplate(reelTemplate) ? {followReelMessageTemplate: reelTemplate} : {}),
    };
    try {
      const response = await fetch(`/api/accounts/${encodeURIComponent(accountId)}/config`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(typeof body?.error === "string" ? body.error : "Could not save behavior choices. Try again.");
      }
      if (!mounted.current) return;
      setDirty(false); setSaved(true); onPending?.(false); onSaved?.(); router.refresh();
    } catch (caught) {
      if (mounted.current) setError(caught instanceof Error ? caught.message : "Could not save behavior choices. Try again.");
    } finally { if (mounted.current) setSaving(false); }
  }

  return <section className="mt-4 rounded-lg border p-4" aria-label="EngageMate behaviors">
    <h3 className="font-semibold">Choose what EngageMate does</h3>
    <p className="mt-1 text-sm text-muted-foreground">New accounts start with likes only. Turn Reel DMs, following, or Story replies on or off here. Unchecked actions stay off. These daily targets apply after warm-up; warm-up, account health, and your plan can lower today&apos;s allowance.</p>
    {initial.warmupExempt && <p className="mt-2 text-sm">Warm-up is waived for this established account. Account health and plan limits still apply.</p>}
    {planDailyActions !== undefined && <p className="mt-2 text-sm text-muted-foreground">Your plan allows {planDailyActions} likes, Story replies, and reel messages combined per day{planDailyViews !== undefined ? `, plus ${planDailyViews} view-only actions` : ""}. Raising one target does not increase the combined allowance.</p>}
    <p className="mt-2 text-sm text-muted-foreground">View Stories controls view-only actions. Likes and replies still require opening the recipient&apos;s Story.</p>
    <fieldset disabled={disabled || saving} className="mt-4 space-y-4">
      <legend className="sr-only">Behavior choices and daily targets</legend>
      {ACTIONS.map(([key, label, target]) => <div key={key} className="space-y-2">
        <label className="flex items-center gap-2"><input type="checkbox" checked={enabled[key]} onChange={event => { setEnabled({ ...enabled, [key]: event.target.checked }); changed(); }} />{label}</label>
        {enabled[key] && <label className="flex items-center justify-between gap-3 text-sm">{target} daily target
          <input type="number" min={1} max={ceiling(key)} step={1} className="w-24 rounded border p-2" value={limits[key]} onChange={event => { setLimits({ ...limits, [key]: event.target.value }); changed(); }} />
        </label>}
      </div>)}
      <label className="flex items-center gap-2"><input type="checkbox" checked={follow} onChange={event => { setFollow(event.target.checked); changed(); }} />Follow after engagement</label>
      <p className="text-sm text-muted-foreground">Follow eligible new accounts before a Story reply or after a successful like. Story replies require a confirmed new follow; the target is a ceiling, not a guaranteed total.</p>
      {follow && !enabled.dailyLikeLimit && !enabled.dailyReplyLimit && <p role="alert" className="text-sm text-destructive">Enable Story likes or replies for follows, or turn off Follow after engagement.</p>}
      {follow && <label className="flex items-center justify-between gap-3 text-sm">Follows daily target
        <input type="number" min={1} max={20} step={1} className="w-24 rounded border p-2" value={followLimit} onChange={event => { setFollowLimit(event.target.value); changed(); }} />
      </label>}
      <label className="flex items-center gap-2"><input type="checkbox" checked={unfollow} onChange={event => { setUnfollow(event.target.checked); changed(); }} />Automatically unfollow after 5 days</label>
      <p className="text-sm text-muted-foreground">Applies only to accounts EngageMate followed. This choice also controls existing scheduled unfollows, independently of new follows.</p>
      <FollowReelFields enabled={reelEnabled} template={reelTemplate} following={follow}
        onEnabled={value => { setReelEnabled(value); changed(); }} onTemplate={value => { setReelTemplate(value); changed(); }} />
      {reelEnabled && <label className="flex items-center justify-between gap-3 text-sm">Reel DMs daily target
        <input type="number" min={0} max={Math.min(20, planDailyActions ?? 20)} step={1} placeholder="Shared" className="w-24 rounded border p-2" value={reelLimit} onChange={event => { setReelLimit(event.target.value); changed(); }} />
      </label>}
      {reelEnabled && <p className="text-sm text-muted-foreground">An explicit reel target reserves capacity separate from Story replies. Leave it blank to share the reply allowance. Eligible new follows and account health determine sends.</p>}
      {reelEnabled && !validFollowReelTemplate(reelTemplate) && <p role="alert" className="text-sm text-destructive">Write a message of 1–500 characters without links.</p>}
      <Button variant="outline" onClick={save} disabled={!dirty || !valid}>{saving ? "Saving behaviors…" : "Save behavior choices"}</Button>
    </fieldset>
    {dirty && <p className="mt-3 text-sm text-muted-foreground">Save your behavior choices before launching.</p>}
    {saved && <p role="status" className="mt-3 text-sm">Behavior choices saved.</p>}
    {error && <p role="alert" className="mt-3 text-sm text-destructive">{error}</p>}
  </section>;
}
