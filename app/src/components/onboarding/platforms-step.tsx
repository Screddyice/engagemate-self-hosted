"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";

interface AccountConfig {
  id: string;
  platform: string;
  username: string;
  dailyPostsLimit: number;
  dailyCommentsLimit: number;
  autoMode: boolean;
}

interface PlatformsStepProps {
  accounts: AccountConfig[];
  onNext: () => void;
  onBack: () => void;
}

export function PlatformsStep({
  accounts,
  onNext,
  onBack,
}: PlatformsStepProps) {
  const [configs, setConfigs] = useState<AccountConfig[]>(accounts);

  const updateConfig = (id: string, field: string, value: number | boolean) => {
    setConfigs(
      configs.map((c) => (c.id === id ? { ...c, [field]: value } : c)),
    );
  };

  const handleSave = async () => {
    for (const config of configs) {
      await fetch(`/api/accounts/${config.id}/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          dailyPostsLimit: config.dailyPostsLimit,
          dailyCommentsLimit: config.dailyCommentsLimit,
          autoMode: config.autoMode,
        }),
      });
    }
    onNext();
  };

  if (accounts.length === 0) {
    return (
      <div>
        <h2 className="text-2xl font-bold">Configure platforms</h2>
        <p className="mt-4 text-muted-foreground">
          No accounts connected yet. You can configure daily limits and
          autopilot settings after connecting accounts from the dashboard.
        </p>
        <div className="mt-8 flex gap-3">
          <Button variant="outline" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext} variant="gradient">
            Skip
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <h2 className="text-2xl font-bold">Configure platforms</h2>
      <p className="mt-1 text-muted-foreground">
        Set daily limits and autopilot mode for each connected account.
      </p>

      <div className="mt-8 space-y-4">
        {configs.map((config) => (
          <div key={config.id} className="rounded-lg border p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="font-medium capitalize">{config.platform}</p>
                <p className="text-sm text-muted-foreground">
                  {config.username}
                </p>
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={config.autoMode}
                  onChange={(e) =>
                    updateConfig(config.id, "autoMode", e.target.checked)
                  }
                  className="rounded"
                />
                Autopilot
              </label>
            </div>
            <div className="mt-4 grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs text-muted-foreground">
                  Daily posts
                </label>
                <input
                  type="number"
                  min={0}
                  max={20}
                  value={config.dailyPostsLimit}
                  onChange={(e) =>
                    updateConfig(
                      config.id,
                      "dailyPostsLimit",
                      parseInt(e.target.value) || 0,
                    )
                  }
                  className="mt-1 w-full rounded-md border bg-background px-3 py-1.5 text-sm"
                />
              </div>
              <div>
                <label className="text-xs text-muted-foreground">
                  Daily comments
                </label>
                <input
                  type="number"
                  min={0}
                  max={50}
                  value={config.dailyCommentsLimit}
                  onChange={(e) =>
                    updateConfig(
                      config.id,
                      "dailyCommentsLimit",
                      parseInt(e.target.value) || 0,
                    )
                  }
                  className="mt-1 w-full rounded-md border bg-background px-3 py-1.5 text-sm"
                />
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-8 flex gap-3">
        <Button variant="outline" onClick={onBack}>
          Back
        </Button>
        <Button onClick={handleSave} variant="gradient">
          Next
        </Button>
      </div>
    </div>
  );
}
