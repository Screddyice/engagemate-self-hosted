"use client";

import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { WeeklyReportPreference } from "@/lib/queries";

const REPORT_HOURS = Array.from({ length: 24 }, (_, hour) => hour);
const COMMON_TIMEZONES = [
  "UTC",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "Europe/London",
  "Asia/Makassar",
];

type Feedback =
  | { kind: "saved"; message: string }
  | { kind: "error"; message: string }
  | null;

export function WeeklyReportSettings({
  initialPreference,
  recipientEmail,
}: {
  initialPreference: WeeklyReportPreference;
  recipientEmail: string;
}) {
  const [enabled, setEnabled] = useState(initialPreference.enabled);
  const [timezone, setTimezone] = useState(initialPreference.timezone);
  const [sendHour, setSendHour] = useState(initialPreference.sendHour);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<Feedback>(null);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setFeedback(null);

    try {
      const response = await fetch("/api/settings/weekly-report", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled, timezone, sendHour }),
      });
      if (!response.ok) {
        const invalidPreference =
          response.status === 400 || response.status === 422;
        setFeedback({
          kind: "error",
          message: invalidPreference
            ? "Settings were not saved. Check the timezone and send hour, then try again."
            : "Settings were not saved. Try again.",
        });
        return;
      }

      setFeedback({
        kind: "saved",
        message: "Weekly report settings saved.",
      });
    } catch {
      setFeedback({
        kind: "error",
        message: "Settings were not saved. Try again.",
      });
    } finally {
      setSaving(false);
    }
  }

  const hourLabel = `${String(sendHour).padStart(2, "0")}:00`;

  return (
    <Card className="overflow-hidden shadow-sm">
      <CardHeader className="gap-4 border-b bg-muted/20 sm:flex-row sm:items-start sm:justify-between sm:space-y-0">
        <div>
          <CardTitle>Weekly growth report</CardTitle>
          <CardDescription className="mt-2 max-w-xl">
            Receive one Monday summary for every Instagram account you manage.
          </CardDescription>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-sm font-medium" aria-hidden="true">
            {enabled ? "On" : "Off"}
          </span>
          <button
            type="button"
            role="switch"
            aria-label="Weekly growth report"
            aria-checked={enabled}
            onClick={() => {
              setEnabled((current) => !current);
              setFeedback(null);
            }}
            className={`relative h-6 w-11 rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 ${
              enabled ? "bg-primary" : "bg-muted-foreground/35"
            }`}
          >
            <span
              className={`absolute top-0.5 h-5 w-5 rounded-full bg-background shadow-sm transition-transform ${
                enabled ? "translate-x-5" : "translate-x-0.5"
              }`}
            />
          </button>
        </div>
      </CardHeader>

      <CardContent className="p-0">
        <div className="grid border-b bg-muted/10 sm:grid-cols-3">
          <div className="border-b px-6 py-4 sm:border-b-0 sm:border-r">
            <p className="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Delivery day
            </p>
            <p className="mt-1 font-mono text-sm font-semibold">Monday</p>
          </div>
          <div className="border-b px-6 py-4 sm:border-b-0 sm:border-r">
            <p className="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Local time
            </p>
            <p className="mt-1 font-mono text-sm font-semibold tabular-nums">
              {hourLabel}
            </p>
          </div>
          <div className="px-6 py-4">
            <p className="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Status
            </p>
            <p className="mt-1 text-sm font-semibold">
              {enabled ? "Scheduled" : "Paused"}
            </p>
          </div>
        </div>

        <form onSubmit={save} className="space-y-6 p-6">
          <div className="grid gap-5 md:grid-cols-2">
            <label className="block text-sm font-medium">
              Report timezone
              <input
                type="text"
                value={timezone}
                onChange={(event) => {
                  setTimezone(event.target.value);
                  setFeedback(null);
                }}
                list="weekly-report-timezones"
                maxLength={64}
                required
                autoComplete="off"
                className="mt-2 h-10 w-full rounded-md border border-input bg-background px-3 font-mono text-sm shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
              <datalist id="weekly-report-timezones">
                {COMMON_TIMEZONES.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
            </label>

            <label className="block text-sm font-medium">
              Send hour
              <select
                value={sendHour}
                onChange={(event) => {
                  setSendHour(Number(event.target.value));
                  setFeedback(null);
                }}
                className="mt-2 h-10 w-full rounded-md border border-input bg-background px-3 font-mono text-sm tabular-nums shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {REPORT_HOURS.map((hour) => (
                  <option key={hour} value={hour}>
                    {`${String(hour).padStart(2, "0")}:00`}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div>
            <label
              htmlFor="weekly-report-recipient"
              className="block text-sm font-medium"
            >
              Report recipient
            </label>
            <input
              id="weekly-report-recipient"
              type="email"
              value={recipientEmail}
              readOnly
              className="mt-2 h-10 w-full rounded-md border border-input bg-muted/40 px-3 text-sm text-muted-foreground shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            />
            <span className="mt-2 block text-xs font-normal text-muted-foreground">
              EngageMate uses the email address on your account.
            </span>
          </div>

          <div className="rounded-lg border bg-muted/20 p-4">
            <p className="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Time-saved method
            </p>
            <p className="mt-2 text-sm leading-6">
              Estimated manual time saved = 30 seconds per Story opportunity
              reviewed, plus 60 seconds per sent reply or DM.
            </p>
          </div>

          <div className="flex min-h-9 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div aria-live="polite">
              {feedback?.kind === "saved" && (
                <p role="status" className="text-sm text-emerald-600">
                  {feedback.message}
                </p>
              )}
              {feedback?.kind === "error" && (
                <p role="alert" className="text-sm text-destructive">
                  {feedback.message}
                </p>
              )}
            </div>
            <Button type="submit" disabled={saving} className="sm:ml-auto">
              {saving ? "Saving…" : "Save changes"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
