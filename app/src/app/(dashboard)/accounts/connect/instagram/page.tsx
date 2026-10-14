import { hostedLoginEnabled } from "@/lib/hosted-login-config";
import { auth } from "@/lib/owner-auth";
import { redirect } from "next/navigation";
import { InstagramConnectWizard } from "@/components/platform-login/instagram-connect-wizard";
import { getUserByOwnerKey } from "@/lib/queries";
import { resolveSelectedAccount } from "@/lib/selected-account";

export default async function ConnectInstagramPage() {
  const { userId } = await auth();
  if (!userId) redirect("/setup");
  const user = await getUserByOwnerKey(userId);
  if (!user) redirect("/setup");
  const { selected } = await resolveSelectedAccount(user.id);

  const enabled = hostedLoginEnabled();
  const configured = Boolean(process.env.INTERNAL_API_KEY?.trim());

  return (
    <div className="mx-auto max-w-2xl py-10">
      <h1 className="text-2xl font-bold">Connect Instagram</h1>
      <p className="mt-2 text-muted-foreground">
        Connect the Instagram account you want EngageMate to use for Story engagement.
      </p>

      {enabled && !configured ? (
        <p role="alert" className="mt-8 text-sm text-muted-foreground">
          Instagram connection is unavailable in this environment. EngageMate’s
          sign-in service needs to be configured before you can connect an account.
        </p>
      ) : (
        <InstagramConnectWizard hostedLoginEnabled={enabled} selectedAccountId={selected?.id ?? null} />
      )}
    </div>
  );
}
