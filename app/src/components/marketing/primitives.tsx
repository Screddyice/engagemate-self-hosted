import type { ReactNode } from "react";

/**
 * Shared marketing primitives. The semantic colors deliberately match the
 * product's own badges: green is a sent action, amber is a gated one, red is a
 * stop. A visitor who signs up sees the same language they were sold in.
 */

export function DotGrid() {
  return (
    <div
      aria-hidden
      className="pointer-events-none fixed inset-0 z-0"
      style={{
        backgroundImage:
          "radial-gradient(circle at 1px 1px, var(--w-dot) 1px, transparent 0)",
        backgroundSize: "32px 32px",
        maskImage:
          "radial-gradient(ellipse 100% 70% at 50% 0%, #000 40%, transparent 100%)",
        WebkitMaskImage:
          "radial-gradient(ellipse 100% 70% at 50% 0%, #000 40%, transparent 100%)",
      }}
    />
  );
}

export function Eyebrow({
  children,
  tone = "blue",
}: {
  children: ReactNode;
  tone?: "blue" | "green" | "amber";
}) {
  const color =
    tone === "green"
      ? "var(--w-green)"
      : tone === "amber"
        ? "var(--w-amber)"
        : "var(--w-blue)";
  return (
    <p
      className="font-mono text-[13px] font-semibold uppercase tracking-[0.18em]"
      style={{ color }}
    >
      {children}
    </p>
  );
}

export function Display({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    // pb: background-clip:text paints only inside the padding box, and
    // leading-[1.02] leaves the last line's descenders (the y in "replying")
    // outside it, where they render as nothing. The padding extends the paint
    // without loosening the line spacing.
    <h1
      className={`bg-clip-text pb-[0.14em] font-black leading-[1.02] tracking-[-0.045em] text-transparent ${className}`}
      style={{
        fontSize: "clamp(44px, 6.4vw, 78px)",
        backgroundImage:
          "linear-gradient(to bottom, var(--w-display-from), var(--w-display-to))",
      }}
    >
      {children}
    </h1>
  );
}

export function SectionHeading({ children }: { children: ReactNode }) {
  return (
    <h2
      className="mt-4 bg-clip-text pb-[0.1em] font-extrabold leading-[1.08] tracking-[-0.035em] text-transparent"
      style={{
        fontSize: "clamp(32px, 4.4vw, 50px)",
        backgroundImage:
          "linear-gradient(to bottom, var(--w-display-from), var(--w-display-to))",
      }}
    >
      {children}
    </h2>
  );
}

export function Lede({ children }: { children: ReactNode }) {
  return (
    <p className="mt-5 max-w-[620px] text-[19px] leading-relaxed text-[var(--w-t2)]">
      {children}
    </p>
  );
}

export function Section({
  children,
  id,
  className = "",
  bleed = false,
  tight = false,
}: {
  children: ReactNode;
  id?: string;
  className?: string;
  bleed?: boolean;
  tight?: boolean;
}) {
  return (
    <section
      id={id}
      className={`relative z-[1] ${bleed ? "" : "mx-auto max-w-[1140px] px-6 sm:px-10"} ${tight ? "py-12 sm:py-14" : "py-24 sm:py-32"} ${className}`}
    >
      {children}
    </section>
  );
}

export function Panel({
  children,
  className = "",
  tone = "default",
}: {
  children: ReactNode;
  className?: string;
  tone?: "default" | "good" | "bad";
}) {
  const border =
    tone === "good"
      ? "var(--w-green-dim)"
      : tone === "bad"
        ? "var(--w-red-border)"
        : "var(--w-border)";
  const bg =
    tone === "good"
      ? "var(--w-green-dim)"
      : tone === "bad"
        ? "var(--w-red-dim)"
        : "var(--w-card)";
  return (
    <div
      className={`rounded-2xl border p-6 ${className}`}
      style={{ borderColor: border, background: bg, boxShadow: "var(--w-glass)" }}
    >
      {children}
    </div>
  );
}

export function StatRow({
  items,
}: {
  items: { value: string; label: string }[];
}) {
  return (
    <div
      className="grid gap-3"
      style={{
        gridTemplateColumns: `repeat(auto-fit, minmax(180px, 1fr))`,
      }}
    >
      {items.map((item) => (
        <div
          key={item.label}
          className="rounded-2xl border border-[var(--w-border)] bg-[var(--w-card)] px-6 py-7 text-center"
          style={{ boxShadow: "var(--w-glass)" }}
        >
          <div className="font-mono text-[26px] font-bold tracking-tight text-[var(--w-t1)]">
            {item.value}
          </div>
          <div className="mt-1.5 text-sm text-[var(--w-t4)]">{item.label}</div>
        </div>
      ))}
    </div>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad";
}) {
  const map = {
    neutral: { bg: "var(--w-card-hover)", fg: "var(--w-t3)" },
    good: { bg: "var(--w-green-dim)", fg: "var(--w-green)" },
    warn: { bg: "var(--w-amber-dim)", fg: "var(--w-amber)" },
    bad: { bg: "var(--w-red-dim)", fg: "var(--w-red)" },
  }[tone];
  return (
    <span
      className="inline-flex shrink-0 items-center rounded-full px-2.5 py-1 font-mono text-[11px] font-semibold uppercase tracking-wider"
      style={{ background: map.bg, color: map.fg }}
    >
      {children}
    </span>
  );
}

export function Divider() {
  return (
    <div className="relative z-[1] mx-auto h-px max-w-[1140px] bg-[var(--w-border)]" />
  );
}
