"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";

type RecordRow = { id: string; username: string; status: string; unfollowDueAt: string | null; cancellable: boolean; messageStatus?: string | null; messageSentAt?: string | null; reelUrl?: string | null };
type Status = { igAccountId: string; pendingUnfollows: number; uncertain: number; records: RecordRow[] };
const MESSAGE_LABELS: Record<string, string> = {pending: "Queued", running: "Sending", sent: "Sent", uncertain: "Needs review", cancelled: "Cancelled"};
function safeTimezone(timezone: string) {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: timezone }).format();
    return timezone;
  } catch {
    return "UTC";
  }
}
function formatAccountTime(value: string | null | undefined, timezone: string) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString("en-US", { timeZone: safeTimezone(timezone), timeZoneName: "short" });
}
function ReelMessageStatus({ row, timezone }: {row: RecordRow; timezone: string}) {
  if (!row.messageStatus) return null;
  const sent = row.messageStatus === "sent";
  const sentTime = sent ? formatAccountTime(row.messageSentAt, timezone) : null;
  const validReel = typeof row.reelUrl === "string" && /^https:\/\/www\.instagram\.com\/reel\/[A-Za-z0-9_-]+\/$/.test(row.reelUrl);
  return <div className="mt-1 text-sm">
    <p>Reel message: {MESSAGE_LABELS[row.messageStatus] ?? "Needs review"}{sentTime ? ` · ${sentTime}` : ""}</p>
    {row.messageStatus === "uncertain" && <p className="text-muted-foreground">EngageMate will not replay this message until delivery has been checked.</p>}
    {sent && validReel && <a className="underline" href={row.reelUrl!} target="_blank" rel="noopener noreferrer">View sent reel</a>}
  </div>;
}
export function FollowStatus({ accountId, autoUnfollowEnabled, timezone = "UTC" }: { accountId: string; autoUnfollowEnabled: boolean; timezone?: string }) {
  const [data, setData] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [cancelling, setCancelling] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController(); setData(null); setError("");
    void fetch(`/api/accounts/${encodeURIComponent(accountId)}/follows`, { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        const body = await response.json();
        if (!response.ok || body.igAccountId !== accountId || !Array.isArray(body.records) || !Number.isInteger(body.pendingUnfollows) || !Number.isInteger(body.uncertain)) throw new Error();
        if (!controller.signal.aborted) setData(body);
      }).catch(() => { if (!controller.signal.aborted) setError("Could not load follow and message history."); });
    return () => controller.abort();
  }, [accountId, attempt]);
  async function cancel(id: string) {
    if (cancelling) return; setCancelling(id); setError("");
    try {
      const response = await fetch(`/api/accounts/${encodeURIComponent(accountId)}/follows`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }),
      });
      if (!response.ok) throw new Error();
      setAttempt((value) => value + 1);
    } catch { setError("Could not cancel this unfollow. It may already be in progress. Refresh its status."); }
    finally { setCancelling(null); }
  }
  const current = data?.igAccountId === accountId ? data : null;
  return <div className="mt-4 rounded-lg border p-4 space-y-3">
    <h3 className="font-medium">Follow history and reel messages</h3>
    <p className="text-sm text-muted-foreground">Unfollows run at or after 120 hours, when the worker next checks and account health allows. Cancel an unfollow to keep following that account. Your existing follows are never added here.</p>
    {!autoUnfollowEnabled && <p role="status" className="text-sm">Automatic unfollow is off. Scheduled unfollows are paused.</p>}
    {current ? <>
      <p className="text-sm">{current.pendingUnfollows} pending · {current.uncertain} uncertain</p>
      {current.uncertain > 0 && <p className="text-sm">Uncertain actions need review before another attempt.</p>}
      {current.records.length === 0 ? <p className="text-sm text-muted-foreground">No follow or reel message history yet.</p> : <ul className="space-y-3">
        {current.records.map((row) => <li key={row.id} className="flex flex-wrap justify-between items-center gap-2 border-t pt-3">
          <div><p>@{row.username}</p><p className="text-sm text-muted-foreground">{row.status === "following" ? `Unfollow due ${formatAccountTime(row.unfollowDueAt, timezone) ?? "unavailable"}` : row.status === "cancelled" ? "Unfollow cancelled" : row.status.replaceAll("_", " ")}</p><ReelMessageStatus row={row} timezone={timezone} /></div>
          {row.cancellable && <Button variant="outline" disabled={cancelling !== null} onClick={() => cancel(row.id)}>{cancelling === row.id ? "Cancelling…" : "Cancel unfollow"}</Button>}
        </li>)}
      </ul>}
      {current.records.length === 100 && <p className="text-sm">Showing the first 100 records.</p>}
    </> : !error && <p role="status" className="text-sm">Loading follow history…</p>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    <Button variant="outline" disabled={cancelling !== null} onClick={() => setAttempt((value) => value + 1)}>Refresh history</Button>
  </div>;
}
