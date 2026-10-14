interface ActivityItem {
  id: string;
  platform: string;
  username: string;
  actionType: string;
  targetUser: string | null;
  createdAt: Date | null;
}

interface ActivityFeedProps {
  activities: ActivityItem[];
}

export function ActivityFeed({ activities }: ActivityFeedProps) {
  if (activities.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">No recent activity.</p>
    );
  }

  return (
    <div className="space-y-3">
      {activities.map((activity) => (
        <div key={activity.id} className="flex items-start gap-3 text-sm">
          <div className="mt-0.5 h-2 w-2 shrink-0 rounded-full bg-violet-500" />
          <div className="flex-1">
            <p>
              <span className="font-medium capitalize">{activity.platform}</span>
              {" "}
              <span className="text-muted-foreground">{activity.actionType}</span>
              {activity.targetUser && (
                <span className="text-muted-foreground"> on {activity.targetUser}</span>
              )}
            </p>
            {activity.createdAt && (
              <p className="text-xs text-muted-foreground">
                {new Date(activity.createdAt).toLocaleString()}
              </p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
