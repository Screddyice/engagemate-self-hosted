"use client";

/**
 * Dashboard route wrapper around the shared connection component.
 *
 * It exists only to load the tenant's current accounts, so the dashboard and
 * onboarding cannot drift into two different connection experiences.
 */

import { useCallback, useEffect, useState } from "react";
import { useConnectedAccounts } from "./use-connected-accounts";
import {
  InstagramConnect,
  type ConnectableAccount,
} from "@/components/platform-login/instagram-connect";

export function InstagramConnectWizard({ hostedLoginEnabled = false, selectedAccountId = null }: {
  hostedLoginEnabled?: boolean;
  selectedAccountId?: string | null;
}) {
  const { accounts, setAccounts, refreshFailed } = useConnectedAccounts(null);
  const [connectedAccountId, setConnectedAccountId] = useState<string | null>(null);
  useEffect(() => { setConnectedAccountId(null); }, [selectedAccountId]);

  const handleConnected = useCallback((account: ConnectableAccount) => {
    setAccounts((current) =>
      current?.some((item) => item.id === account.id)
        ? current.map((item) => item.id === account.id ? account : item)
        : [...(current ?? []), account],
    );
    setConnectedAccountId(account.id);
  }, [setAccounts]);

  if (accounts === null) {
    return (
      <p className="mt-8 text-sm text-muted-foreground">
        {refreshFailed
          ? "We couldn't check your account status. Refresh this page to try again."
          : "Loading your connected accounts…"}
      </p>
    );
  }

  return (
    <div className="mt-8">
      {refreshFailed && <p role="status" className="mb-3 text-sm text-amber-400">
        We couldn&apos;t refresh account health. The status below may be out of date.
      </p>}
      <InstagramConnect hostedLoginEnabled={hostedLoginEnabled}
        accounts={accounts.filter((account) => account.id ===
          (connectedAccountId ?? selectedAccountId ?? accounts[0]?.id))}
        onConnected={handleConnected} />
    </div>
  );
}
