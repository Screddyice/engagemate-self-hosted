import { NotificationRecipients } from "@/components/settings/notification-recipients";
import { getNotificationRecipients } from "@/lib/notification-recipients";
import { planLimits } from "@/lib/plans";
import { auth } from "@/lib/owner-auth";
import { redirect } from "next/navigation";
import { BehaviorSettings } from "@/components/settings/behavior-settings";
import { FollowStatus } from "@/components/dashboard/follow-status";
import { resolveSelectedAccount } from "@/lib/selected-account";
import { EmailNotificationSettings } from "@/components/settings/email-notification-settings";
import { WeeklyReportSettings } from "@/components/settings/weekly-report-settings";
import {
  getEmailPreferences,
  getOrCreateWeeklyReportPreference,
  getUserByOwnerKey,
  getAgentConfig,
} from "@/lib/queries";

export default async function SettingsPage() {
  const { userId } = await auth();
  if (!userId) redirect("/setup");

  const user = await getUserByOwnerKey(userId);
  if (!user) redirect("/setup");

  const { selected: account } = await resolveSelectedAccount(user.id);
  const config = account ? await getAgentConfig(user.id, account.id) : null;
  const preference = await getOrCreateWeeklyReportPreference(user.id);
  const emailPreferences = await getEmailPreferences(user.id);
  const recipientEmails = await getNotificationRecipients(user.id);

  return (
    <div>
      <h1 className="text-2xl font-bold">Settings</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Choose engagement behaviors, daily targets, emails for your account.
      </p>
      <div className="mt-8 space-y-6">
        {account && config && <section>
          <h2 className="text-lg font-semibold">Engagement behaviors · @{account.username}</h2>
          <BehaviorSettings key={`${account.id}-${config.version}`} accountId={account.id} initial={config} planDailyActions={planLimits(user.plan).dailyActions} planDailyViews={planLimits(user.plan).dailyViews} />
          <FollowStatus key={account.id} accountId={account.id} autoUnfollowEnabled={config.autoUnfollowEnabled} timezone={config.timezone} />
        </section>}
        <NotificationRecipients primaryEmail={user.email} initialEmails={recipientEmails} />
        <EmailNotificationSettings initialPreferences={emailPreferences} />
        <WeeklyReportSettings
          initialPreference={{
            enabled: preference.enabled,
            timezone: preference.timezone,
            sendHour: preference.sendHour,
          }}
          recipientEmail={user.email}
        />
      </div>
    </div>
  );
}
