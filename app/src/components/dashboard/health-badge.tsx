const HEALTH_STYLES: Record<string, string> = {
  active: "bg-green-100 text-green-800",
  degraded: "bg-amber-100 text-amber-800",
  paused: "bg-amber-100 text-amber-800",
  challenged: "bg-red-100 text-red-800",
  banned: "bg-red-100 text-red-800",
};

export function HealthBadge({ status }: { status: string }) {
  return (
    <span
      className={`rounded-full px-2 py-1 text-xs capitalize ${
        HEALTH_STYLES[status] ?? "bg-muted text-muted-foreground"
      }`}
    >
      {status}
    </span>
  );
}
