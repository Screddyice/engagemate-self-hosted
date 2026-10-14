import { WARMUP_BRACKETS } from "@/lib/warmup";
import { Badge } from "@/components/marketing/primitives";

/**
 * Diagrams for the parts of the product that have no screen to screenshot.
 * WarmupRamp reads the product's own brackets rather than restating them, so
 * the marketing page cannot drift from what the worker actually enforces.
 */

export function IsolationDiagram() {
  const accounts = [
    { handle: "@lumen.studio", geo: "Los Angeles, US", ip: "198.51.100.190" },
    { handle: "@harborcoffee", geo: "Seattle, US", ip: "198.51.100.77" },
    { handle: "@novaskincare", geo: "Brooklyn, US", ip: "198.51.100.44" },
  ];
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      {accounts.map((account) => (
        <div
          key={account.handle}
          className="rounded-2xl border border-[var(--w-border)] bg-[var(--w-card)] p-5"
          style={{ boxShadow: "var(--w-glass)" }}
        >
          <p className="font-mono text-[13px] font-semibold text-[var(--w-t1)]">
            {account.handle}
          </p>
          <dl className="mt-4 space-y-2.5 font-mono text-[11px]">
            {[
              ["proxy", account.ip],
              ["geo", account.geo],
              ["profile", "chrome-" + account.handle.slice(1, 5)],
              ["fingerprint", "pinned"],
            ].map(([key, value]) => (
              <div key={key} className="flex justify-between gap-3">
                <dt className="text-[var(--w-t4)]">{key}</dt>
                <dd className="truncate text-[var(--w-t2)]">{value}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-4 border-t border-[var(--w-border)] pt-3 text-[11px] leading-relaxed text-[var(--w-t4)]">
            Nothing is shared with the other accounts. Not the IP, not the
            browser profile, not the cookies.
          </p>
        </div>
      ))}
    </div>
  );
}

export function WarmupRamp() {
  const peak = Math.max(
    ...WARMUP_BRACKETS.map((b) => b.caps.like + b.caps.reply + b.caps.dm),
  );
  return (
    <div
      className="rounded-2xl border border-[var(--w-border)] bg-[var(--w-card)] p-6"
      style={{ boxShadow: "var(--w-glass)" }}
    >
      <div className="flex items-end gap-2 sm:gap-4">
        {WARMUP_BRACKETS.map((bracket) => {
          const total =
            bracket.caps.like + bracket.caps.reply + bracket.caps.dm;
          return (
            <div key={bracket.label} className="flex flex-1 flex-col items-center">
              <span className="mb-2 font-mono text-[13px] font-bold text-[var(--w-t1)]">
                {total}
              </span>
              <div
                className="w-full rounded-t-md"
                style={{
                  height: `${Math.max(8, (total / peak) * 150)}px`,
                  background:
                    "linear-gradient(180deg, rgba(59,130,246,0.85), rgba(59,130,246,0.22))",
                }}
              />
              <span className="mt-3 text-center font-mono text-[10px] leading-tight text-[var(--w-t4)]">
                {bracket.label}
              </span>
            </div>
          );
        })}
      </div>
      <p className="mt-6 border-t border-[var(--w-border)] pt-4 text-sm leading-relaxed text-[var(--w-t3)]">
        Total actions a day, per account, from the day it connects. A new
        account does not get a full budget, and no setting skips the ramp.
      </p>
    </div>
  );
}

export function DecisionLoop() {
  const steps = [
    { k: "watch", v: "Check each target for an active Story" },
    { k: "read", v: "A vision model reads the Story for actual context" },
    { k: "decide", v: "Like, reply, DM, or skip" },
    { k: "gate", v: "Deterministic check before anything sends" },
    { k: "send", v: "One action, at a human pace" },
  ];
  return (
    <ol className="space-y-2">
      {steps.map((step, index) => (
        <li
          key={step.k}
          className="flex items-center gap-4 rounded-xl border border-[var(--w-border)] bg-[var(--w-card)] px-5 py-4"
        >
          <span className="font-mono text-xs text-[var(--w-t4)]">
            {String(index + 1).padStart(2, "0")}
          </span>
          <span className="font-mono text-[13px] font-semibold text-[var(--w-blue)]">
            {step.k}
          </span>
          <span className="text-sm text-[var(--w-t2)]">{step.v}</span>
        </li>
      ))}
    </ol>
  );
}

export function BreakerStates() {
  const states: {
    state: string;
    tone: "good" | "warn" | "bad";
    meaning: string;
  }[] = [
    {
      state: "active",
      tone: "good",
      meaning: "Running inside today's cap on its own schedule.",
    },
    {
      state: "degraded",
      tone: "warn",
      meaning:
        "Recent runs hit resistance. The worker holds back on its own before anything escalates.",
    },
    {
      state: "paused",
      tone: "bad",
      meaning:
        "A challenge, checkpoint, or login signal appeared. Every action stops until you reconnect.",
    },
  ];
  return (
    <div className="space-y-3">
      {states.map((item) => (
        <div
          key={item.state}
          className="flex flex-col gap-3 rounded-xl border border-[var(--w-border)] bg-[var(--w-card)] px-5 py-4 sm:flex-row sm:items-center"
        >
          <div className="sm:w-32">
            <Badge tone={item.tone}>{item.state}</Badge>
          </div>
          <p className="flex-1 text-sm leading-relaxed text-[var(--w-t2)]">
            {item.meaning}
          </p>
        </div>
      ))}
    </div>
  );
}
