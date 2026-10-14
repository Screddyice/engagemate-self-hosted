"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { launchOnboardingAccount } from "@/lib/onboarding-launch";

export function AgentControlButton({
  igAccountId,
}: {
  igAccountId: string;
}) {
  const router = useRouter();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const update = async () => {
    setSaving(true);
    setError("");
    try {
      await launchOnboardingAccount(igAccountId, "Agent status was not saved");
      router.refresh();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Agent status was not saved",
      );
      setSaving(false);
    }
  };

  return (
    <div className="mt-4">
      <Button
        variant="outline"
        className="min-h-11"
        onClick={update}
        disabled={saving}
      >
        {saving ? "Resuming…" : "Resume agent"}
      </Button>
      {error && (
        <p className="mt-2 text-sm text-red-700" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
