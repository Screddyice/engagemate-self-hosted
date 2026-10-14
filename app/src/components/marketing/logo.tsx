import { useId } from "react";

/**
 * The EngageMate mark: a speech-bubble ring and an avatar that together read
 * as a lowercase "e" — a thin story ring, a floating avatar, a diagonal
 * aperture at four o'clock, and a tail that makes it a conversation. Vector
 * recreation of the refined 2026-08-04 brand image (v2); color comes from currentColor so
 * each surface sets it (brand blue on dark, logo blue on light).
 */
export function EngageMateLogo({ className = "" }: { className?: string }) {
  const maskId = useId();
  return (
    <svg viewBox="0 0 100 100" className={className} aria-hidden="true">
      <mask id={maskId}>
        <rect width="100" height="100" fill="black" />
        <circle cx="50" cy="47" r="44" fill="white" />
        <path d="M56 88 L75 76 L67 100 Z" fill="white" />
        <circle cx="50" cy="47" r="34.5" fill="black" />
        <path d="M67 64 L94 85 L90 91 L63 70 Z" fill="black" />
        <circle cx="50" cy="35" r="10" fill="white" />
        <path
          d="M33 68 L33 60 Q33 50 43 50 L57 50 Q67 50 67 60 L67 68 Z"
          fill="white"
        />
      </mask>
      <rect
        width="100"
        height="100"
        fill="currentColor"
        mask={`url(#${maskId})`}
      />
    </svg>
  );
}

/** The two-tone wordmark that accompanies the mark. */
export function EngageMateWordmark({
  className = "",
}: {
  className?: string;
}) {
  return (
    <span className={`font-bold tracking-[-0.03em] ${className}`}>
      <span className="text-[var(--w-t1)]">Engage</span>
      <span className="text-[var(--w-blue)]">Mate</span>
    </span>
  );
}
