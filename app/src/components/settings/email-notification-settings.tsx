"use client";

import { useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { EmailPreferences } from "@/lib/queries";

const OPTIONS: {
  key: keyof EmailPreferences;
  title: string;
  description: string;
}[] = [
  {
    key: "activityEmails",
    title: "Daily activity summary",
    description:
      "One email a day listing the views, likes, replies, follows, and unfollows EngageMate confirmed.",
  },
  {
    key: "onboardingEmails",
    title: "Setup emails",
    description:
      "Reminders and tips while you connect your Instagram account and launch EngageMate.",
  },
];

export function EmailNotificationSettings({
  initialPreferences,
}: {
  initialPreferences: EmailPreferences;
}) {
  const [preferences, setPreferences] = useState(initialPreferences);
  const [error, setError] = useState<string | null>(null);
  const [savingKey, setSavingKey] = useState<keyof EmailPreferences | null>(
    null,
  );

  async function toggle(key: keyof EmailPreferences) {
    const previous = preferences;
    const next = { ...preferences, [key]: !preferences[key] };
    setPreferences(next);
    setSavingKey(key);
    setError(null);
    try {
      const response = await fetch("/api/settings/email-preferences", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(next),
      });
      if (!response.ok) throw new Error("not saved");
    } catch {
      setPreferences(previous);
      setError("That change was not saved. Try again.");
    } finally {
      setSavingKey(null);
    }
  }

  return (
    <Card className="overflow-hidden shadow-sm">
      <CardHeader className="border-b bg-muted/20">
        <CardTitle>Email notifications</CardTitle>
        <CardDescription className="mt-2 max-w-xl">
          Choose which optional emails your notification recipients get. Login alerts
          stay enabled. Your primary copies include unsubscribe links. The weekly
          growth report has its own switch below.
        </CardDescription>
      </CardHeader>
      <CardContent className="divide-y p-0">
        {OPTIONS.map((option) => {
          const enabled = preferences[option.key];
          return (
            <div
              key={option.key}
              className="flex items-start justify-between gap-6 px-6 py-4"
            >
              <div>
                <p className="text-sm font-semibold">{option.title}</p>
                <p className="mt-1 text-sm text-muted-foreground">
                  {option.description}
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-label={option.title}
                aria-checked={enabled}
                disabled={savingKey !== null}
                onClick={() => toggle(option.key)}
                className={`relative mt-1 h-6 w-11 shrink-0 rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-60 ${
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
          );
        })}
        {error && (
          <p role="alert" className="px-6 py-3 text-sm text-destructive">
            {error}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
