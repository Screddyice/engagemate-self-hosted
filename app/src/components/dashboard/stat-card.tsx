import { Card } from "@/components/ui/card";

interface StatCardProps {
  title: string;
  value: string | number;
  description?: string;
  delta?: number | null;
  deltaLabel?: string;
}

export function StatCard({ title, value, description, delta, deltaLabel }: StatCardProps) {
  return (
    <Card className="p-6">
      <p className="text-sm font-medium text-muted-foreground">{title}</p>
      <p className="mt-2 text-3xl font-bold">{value}</p>
      {delta !== undefined && delta !== null && (
        <p className={`mt-1 text-xs font-medium ${delta >= 0 ? "text-green-500" : "text-red-500"}`}>
          {delta >= 0 ? "+" : ""}{delta}{deltaLabel || ""}
        </p>
      )}
      {description && (
        <p className="mt-1 text-xs text-muted-foreground">{description}</p>
      )}
    </Card>
  );
}
