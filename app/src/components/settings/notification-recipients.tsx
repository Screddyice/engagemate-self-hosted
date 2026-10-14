"use client";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export function NotificationRecipients({ primaryEmail, initialEmails }: { primaryEmail: string; initialEmails: string[] }) {
  const [emails, setEmails] = useState(initialEmails);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  async function save(next: string[]) {
    setBusy(true); setError(""); setSaved(false);
    try {
      const response = await fetch("/api/settings/notification-recipients", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ emails: next }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not save recipients.");
      setEmails(data.emails); setEmail(""); setSaved(true);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not save recipients. Try again."); }
    finally { setBusy(false); }
  }
  return <Card>
    <CardHeader>
      <CardTitle>Notification recipients</CardTitle>
      <CardDescription>Add up to five additional email addresses for login alerts, activity summaries, setup emails, and weekly reports. Your primary email stays included. Email preferences apply to all recipients; login alerts are always enabled.</CardDescription>
    </CardHeader>
    <CardContent className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3 text-sm"><span className="break-all">{primaryEmail}</span><span className="text-muted-foreground">Primary email</span></div>
      <ul className="space-y-2">{emails.map(value => <li key={value} className="flex items-center justify-between gap-3 rounded-md border p-3 text-sm"><span className="break-all">{value}</span><Button variant="ghost" disabled={busy} aria-label={`Remove ${value}`} onClick={() => save(emails.filter(item => item !== value))}>Remove</Button></li>)}</ul>
      <form className="space-y-2" onSubmit={event => { event.preventDefault(); const value = email.trim().toLowerCase(); if (value === primaryEmail.toLowerCase() || emails.includes(value)) { setError("That address already receives notifications."); return; } void save([...emails, value]); }}>
        <label className="text-sm font-medium" htmlFor="notification-email">Additional email address</label>
        <div className="flex flex-wrap gap-2"><input className="h-10 min-w-0 flex-1 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50" id="notification-email" type="email" required maxLength={254} value={email} onChange={event => setEmail(event.target.value)} placeholder="name@example.com" disabled={busy || emails.length >= 5} /><Button type="submit" disabled={busy || emails.length >= 5}>{busy ? "Saving…" : "Add email"}</Button></div>
      </form>
      <p className="text-xs text-muted-foreground">Additional recipients receive copies only. They do not gain access to your EngageMate account. Removing an address stops queued copies that have not started sending.</p>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {saved && <p role="status" className="text-sm text-muted-foreground">Notification recipients saved.</p>}
    </CardContent>
  </Card>;
}
