import { Card } from "@/components/ui/card";

interface AgentStatusCardProps {
  platform: string;
  username: string;
  lastRunAt: Date | null;
  lastRunStatus: string | null;
  postsThisWeek: number;
  commentsThisWeek: number;
}

export function AgentStatusCard({
  platform,
  username,
  lastRunAt,
  lastRunStatus,
  postsThisWeek,
  commentsThisWeek,
}: AgentStatusCardProps) {
  const statusColor =
    lastRunStatus === "completed" ? "bg-green-500" :
    lastRunStatus === "running" ? "bg-yellow-500" :
    lastRunStatus === "error" ? "bg-red-500" :
    "bg-muted-foreground";

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between">
        <div>
          <p className="font-medium capitalize">{platform}</p>
          <p className="text-sm text-muted-foreground">{username}</p>
        </div>
        <div className="flex items-center gap-2">
          <div className={`h-2 w-2 rounded-full ${statusColor}`} />
          <span className="text-xs text-muted-foreground capitalize">
            {lastRunStatus || "never run"}
          </span>
        </div>
      </div>
      <div className="mt-3 flex gap-4 text-sm">
        <div>
          <span className="font-semibold">{postsThisWeek}</span>
          <span className="ml-1 text-muted-foreground">posts</span>
        </div>
        <div>
          <span className="font-semibold">{commentsThisWeek}</span>
          <span className="ml-1 text-muted-foreground">comments</span>
        </div>
      </div>
      {lastRunAt && (
        <p className="mt-2 text-xs text-muted-foreground">
          Last run: {new Date(lastRunAt).toLocaleString()}
        </p>
      )}
    </Card>
  );
}
