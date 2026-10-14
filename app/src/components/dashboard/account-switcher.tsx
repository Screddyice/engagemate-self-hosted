"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type { IgAccountRow } from "@/lib/queries";

export function AccountSwitcher({
  accounts,
  selectedAccountId,
}: {
  accounts: IgAccountRow[];
  selectedAccountId: string | null;
}) {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  if (accounts.length === 0) return null;

  return (
    <div className="border-b px-4 py-4">
      <label
        htmlFor="working-account"
        className="mb-2 block text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground"
      >
        Working account
      </label>
      <select
        id="working-account"
        value={selectedAccountId ?? accounts[0].id}
        disabled={pending || accounts.length === 1}
        onChange={async (event) => {
          setPending(true);
          try {
            const response = await fetch("/api/accounts/active", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ igAccountId: event.target.value }),
            });
            if (!response.ok) throw new Error("Account switch failed");
            router.refresh();
          } finally {
            setPending(false);
          }
        }}
        className="h-10 w-full rounded-md border bg-background px-3 text-sm font-medium outline-none ring-offset-background focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-default disabled:opacity-80"
      >
        {accounts.map((account) => (
          <option key={account.id} value={account.id}>
            {`@${account.username} · ${account.healthStatus}`}
          </option>
        ))}
      </select>
    </div>
  );
}
