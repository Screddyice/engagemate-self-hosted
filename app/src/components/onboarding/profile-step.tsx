"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";

interface ProfileStepProps {
  defaultName: string;
  onSaved: (name: string) => void;
}

export function ProfileStep({ defaultName, onSaved }: ProfileStepProps) {
  const [name, setName] = useState(defaultName);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async () => {
    if (!name.trim()) {
      setError("Your name is required");
      return;
    }

    setSaving(true);
    setError("");

    try {
      const res = await fetch("/api/onboarding/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim() }),
      });

      if (!res.ok) {
        const text = await res.text();
        setError(text || "Failed to save");
        return;
      }

      onSaved(name.trim());
    } catch {
      setError("We couldn't save your name. Check your connection and try again.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <h2 className="text-2xl font-bold">What should we call you?</h2>
      <p className="mt-1 text-muted-foreground">
        This is the name shown across your dashboard and account settings.
      </p>

      <div className="mt-8 space-y-6">
        <div>
          <label className="text-sm font-medium">Your name</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Alex Rivera"
            className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"
          />
        </div>

        {error && <p className="text-sm text-red-500">{error}</p>}

        <Button
          onClick={handleSubmit}
          disabled={saving || !name.trim()}
          variant="gradient"
        >
          {saving ? "Saving..." : "Next"}
        </Button>
      </div>
    </div>
  );
}
