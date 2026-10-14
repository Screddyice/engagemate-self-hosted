"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";

interface VoiceStepProps {
  igAccountId?: string;
  defaultPersona?: string;
  onSaved: (persona: string) => void;
  onBack: () => void;
}

export function VoiceStep({
  igAccountId,
  defaultPersona,
  onSaved,
  onBack,
}: VoiceStepProps) {
  const [persona, setPersona] = useState(defaultPersona || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async () => {
    if (!persona.trim()) {
      setError("Describe your voice before continuing");
      return;
    }

    setSaving(true);
    setError("");

    const res = await fetch("/api/onboarding/voice", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ igAccountId, persona: persona.trim() }),
    });

    if (!res.ok) {
      setError("Failed to save voice config");
      setSaving(false);
      return;
    }

    setSaving(false);
    onSaved(persona.trim());
  };

  return (
    <div>
      <h2 className="text-2xl font-bold">Define your voice</h2>
      <p className="mt-1 text-muted-foreground">
        Your voice for replies that start from an active Instagram
        Story.
      </p>

      <div className="mt-8 space-y-6">
        <div>
          <label className="text-sm font-medium">Persona</label>
          <textarea
            value={persona}
            onChange={(e) => setPersona(e.target.value)}
            rows={8}
            maxLength={2000}
            placeholder={
              "Who are you, what do you talk about, and how do you sound? Include the tone and style you want, plus anything you always or never say."
            }
            className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            {persona.length}/2000 characters
          </p>
        </div>

        {error && <p className="text-sm text-red-500">{error}</p>}

        <div className="flex gap-3">
          <Button variant="outline" onClick={onBack}>
            Back
          </Button>
          <Button onClick={handleSubmit} disabled={saving} variant="gradient">
            {saving ? "Saving..." : "Next"}
          </Button>
        </div>
      </div>
    </div>
  );
}
