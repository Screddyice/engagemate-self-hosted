"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";

export function AccountResumeButton({ igAccountId }: { igAccountId: string }) {
  const router = useRouter();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const resume = async () => {
    setSaving(true);
    setError("");
    try {
      const response = await fetch(
        `/api/accounts/${encodeURIComponent(igAccountId)}/resume`,
        { method: "POST" },
      );
      const body = (await response.json().catch(() => ({}))) as {
        error?: string;
        detail?: string;
      };
      if (!response.ok) {
        throw new Error(body.error || body.detail || "Account could not be resumed");
      }
      router.push("/dashboard");
      router.refresh();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Account could not be resumed",
      );
      setSaving(false);
    }
  };

  return (
    <div className="mt-4">
      <Button
        variant="outline"
        className="min-h-11"
        onClick={resume}
        disabled={saving}
      >
        {saving ? "Resuming account…" : "Resume account automation"}
      </Button>
      {error && (
        <p className="mt-2 text-sm text-red-700" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
