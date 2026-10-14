"use client";

export function FollowReelFields({ enabled, template, following, onEnabled, onTemplate }: {
  enabled: boolean; template: string; following: boolean;
  onEnabled: (value: boolean) => void; onTemplate: (value: string) => void;
}) {
  return <div className="space-y-2 border-t pt-4">
    <label className="flex items-center gap-2"><input type="checkbox" checked={enabled} onChange={event => onEnabled(event.target.checked)} />Send a reel after following</label>
    <p className="text-sm text-muted-foreground">Send one message after each new confirmed follow. EngageMate rotates your latest three reels and adds the reel link. Existing follows do not receive messages.</p>
    {enabled && <>
      {!following && <p className="text-sm text-muted-foreground">Turn on Follow after engagement to send these messages after new follows.</p>}
      <label className="block text-sm">Reel message
        <textarea className="mt-2 block w-full rounded border bg-transparent p-3" rows={4} maxLength={500} value={template} onChange={event => onTemplate(event.target.value)} />
      </label>
      <p className="text-sm text-muted-foreground">Write 1–500 characters without links. Add your name if you want a signature. Messages use your saved daily allowance. Account health and plan limits still apply.</p>
    </>}
    <p className="text-sm text-muted-foreground">Turning this off cancels messages that have not started sending. EngageMate holds uncertain deliveries for review to avoid duplicate messages.</p>
  </div>;
}
