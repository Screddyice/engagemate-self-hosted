"use client";

/**
 * Onboarding step two: connect Instagram.
 *
 * This step is mandatory. Every downstream step — voice, launch, and every
 * scheduled run after it — needs an account id, so a wizard that could be
 * skipped here produced tenants that finished onboarding and could never run.
 * There is no skip control, and Next stays disabled until the server has
 * returned a row.
 */

import { Button } from "@/components/ui/button";
import { InstagramConnect } from "@/components/platform-login/instagram-connect";
import {
  healthyAccount,
  isHealthyAccount,
  type ConnectableAccount,
} from "@/lib/instagram-account-health";

interface AccountsStepProps {
  hostedLoginEnabled?: boolean;
  connectedAccounts: ConnectableAccount[];
  activeAccountId?: string | null;
  onConnected: (account: ConnectableAccount) => void;
  onNext: () => void;
  onBack: () => void;
}

export function AccountsStep({
  hostedLoginEnabled = false,
  connectedAccounts,
  activeAccountId,
  onConnected,
  onNext,
  onBack,
}: AccountsStepProps) {
  const effectiveAccountId = activeAccountId ?? healthyAccount(connectedAccounts)?.id;
  const activeAccount = connectedAccounts.find(
    (account) => account.id === effectiveAccountId && isHealthyAccount(account),
  );
  const connectionAccounts = activeAccountId
    ? connectedAccounts.filter((account) => account.id === activeAccountId)
    : connectedAccounts;

  return (
    <div>
      <h2 className="text-2xl font-bold">Connect your Instagram account</h2>
      <p className="mt-1 text-muted-foreground">
        Connect the Instagram account you want EngageMate to use for Story engagement.
      </p>

      <div className="mt-6">
        <InstagramConnect hostedLoginEnabled={hostedLoginEnabled} accounts={connectionAccounts} onConnected={onConnected} />
      </div>

      <div className="mt-8 flex gap-3">
        <Button variant="outline" onClick={onBack}>
          Back
        </Button>
        <Button
          onClick={onNext}
          variant="gradient"
          disabled={!activeAccount}
        >
          Next
        </Button>
      </div>
    </div>
  );
}
