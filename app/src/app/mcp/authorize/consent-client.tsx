"use client";

import { useEffect, useState } from "react";

type Consent = { client_name: string; requested_scopes: string[] };

export function ConsentClient({ state }: { state: string }) {
  const [consent, setConsent] = useState<Consent | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetch(`/api/mcp/authorize?state=${encodeURIComponent(state)}`, { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("This authorization request is unavailable.");
        return response.json() as Promise<Consent>;
      })
      .then(setConsent)
      .catch((reason: Error) => setError(reason.message));
  }, [state]);

  async function finish(approved: boolean) {
    if (!consent) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/mcp/authorize", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          state,
          approved,
          approved_scopes: approved ? consent.requested_scopes : [],
        }),
      });
      const payload = (await response.json()) as { continue_url?: string; error?: string };
      if (!response.ok || !payload.continue_url) {
        throw new Error(payload.error || "Authorization could not be completed.");
      }
      window.location.assign(payload.continue_url);
    } catch (reason) {
      setBusy(false);
      setError(reason instanceof Error ? reason.message : "Authorization could not be completed.");
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-xl items-center px-6 py-16">
      <section className="w-full rounded-2xl border border-zinc-200 bg-white p-8 shadow-sm">
        <p className="text-sm font-medium text-indigo-600">EngageMate agent access</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight text-zinc-950">
          Approve account controls
        </h1>
        {error ? <p className="mt-6 text-sm text-red-700">{error}</p> : null}
        {!consent && !error ? <p className="mt-6 text-zinc-600">Loading request...</p> : null}
        {consent ? (
          <>
            <p className="mt-5 text-zinc-700">
              {consent.client_name} is requesting these permissions:
            </p>
            <ul className="mt-4 space-y-2 rounded-xl bg-zinc-50 p-4 text-sm text-zinc-800">
              {consent.requested_scopes.map((scope) => (
                <li key={scope}>{scope}</li>
              ))}
            </ul>
            <p className="mt-4 text-sm text-zinc-600">
              Instagram passwords and session cookies stay outside this connection.
            </p>
            <div className="mt-8 flex gap-3">
              <button
                type="button"
                disabled={busy}
                onClick={() => finish(true)}
                className="rounded-lg bg-zinc-950 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
              >
                Allow access
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => finish(false)}
                className="rounded-lg border border-zinc-300 px-4 py-2 text-sm font-medium text-zinc-800 disabled:opacity-50"
              >
                Deny
              </button>
            </div>
          </>
        ) : null}
      </section>
    </main>
  );
}
