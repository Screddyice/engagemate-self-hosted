"use client";

import Link from "next/link";
import { useState } from "react";

type State = "idle" | "saving" | "done" | "error";

export function UnsubscribeConfirm({
  u,
  c,
  t,
  label,
}: {
  u: string;
  c: string;
  t: string;
  label: string;
}) {
  const [state, setState] = useState<State>("idle");

  async function unsubscribe() {
    setState("saving");
    try {
      const response = await fetch("/api/email/unsubscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ u, c, t }),
      });
      setState(response.ok ? "done" : "error");
    } catch {
      setState("error");
    }
  }

  if (state === "done") {
    return (
      <div role="status" className="text-[15px] leading-relaxed">
        <p className="font-semibold">You are unsubscribed.</p>
        <p className="mt-2 text-[var(--w-muted)]">
          EngageMate will stop sending you {label}. You can turn them back on
          any time in{" "}
          <Link href="/settings" className="text-[var(--w-blue)] underline">
            Settings
          </Link>
          .
        </p>
      </div>
    );
  }

  return (
    <div>
      <button
        type="button"
        onClick={unsubscribe}
        disabled={state === "saving"}
        className="flex h-11 items-center rounded-lg px-5 text-[14px] font-semibold text-white disabled:opacity-60"
        style={{
          background: "linear-gradient(180deg, #3b82f6, #2563eb)",
          boxShadow:
            "0 1px 2px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.14)",
        }}
      >
        {state === "saving" ? "Unsubscribing…" : `Unsubscribe from ${label}`}
      </button>
      {state === "error" && (
        <p role="alert" className="mt-3 text-[14px] text-[var(--w-red)]">
          That did not go through. Try again, or turn these emails off in
          Settings.
        </p>
      )}
    </div>
  );
}
