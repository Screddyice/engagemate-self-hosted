import { hostedLoginEnabled } from "@/lib/hosted-login-config";
import { redirect } from "next/navigation";
import {
  getAgentConfig,
  hasEnabledTarget,
} from "@/lib/queries";
import { ensureUser } from "@/lib/ensure-user";
import { brandVoiceFromAnalysis } from "@/lib/brand-voice";
import { isHealthyAccount } from "@/lib/instagram-account-health";
import { getOnboardingBrandProfile } from "@/lib/onboarding-control-api";
import { ONBOARDING_STEP } from "@/lib/onboarding-steps";
import { resolveSelectedAccount } from "@/lib/selected-account";
import { OnboardingWizard } from "./wizard-client";

export default async function OnboardingPage() {
  const user = await ensureUser();
  if (!user) redirect("/setup");

  const { accounts, selected } = await resolveSelectedAccount(user.id);
  const account = selected && isHealthyAccount(selected) ? selected : null;
  const configPromise = selected
    ? getAgentConfig(user.id, selected.id)
    : Promise.resolve(null);
  const hasTargetPromise = account
    ? hasEnabledTarget(user.id, account.id)
    : Promise.resolve(false);
  const brandProfilePromise = account
    ? getOnboardingBrandProfile(user.id, account.id).then(
        (profile) => ({ profile, unavailable: false }),
        () => ({ profile: null, unavailable: true }),
      )
    : Promise.resolve({ profile: null, unavailable: false });
  const config = await configPromise;

  if (selected?.healthStatus === "paused" && config?.launchedAt) {
    return redirect("/account-health");
  }
  if (account && config?.enabled) return redirect("/dashboard");

  const [hasManualTarget, brandProfileResult] = await Promise.all([
    hasTargetPromise,
    brandProfilePromise,
  ]);
  const brandProfile = brandProfileResult.profile;
  const accountBrandProfile =
    account && brandProfile?.ig_account_id === account.id ? brandProfile : null;

  const brandVoice = brandVoiceFromAnalysis(accountBrandProfile?.analysis);
  const hasTarget = hasManualTarget || accountBrandProfile?.follower_audience_approved === true;
  const initialStep = account
    ? config?.persona && hasTarget
      ? ONBOARDING_STEP.launch
      : ONBOARDING_STEP.brand
    : ONBOARDING_STEP.profile;

  return (
    <OnboardingWizard
      hostedLoginEnabled={hostedLoginEnabled()}
      name={user.name ?? ""}
      accounts={accounts.map((a) => ({
        id: a.id,
        username: a.username,
        healthStatus: a.healthStatus,
      }))}
      persona={config?.persona ?? brandVoice ?? ""}
      initialAccountId={selected?.id ?? null}
      hasTarget={hasTarget}
      initialStep={initialStep}
      brandProfile={accountBrandProfile}
      brandProfileUnavailable={brandProfileResult.unavailable}
    />
  );
}
