"use client";
import { ManualAudienceStep } from "@/components/onboarding/manual-audience-step";

import { useEffect, useState } from "react";
import { useConnectedAccounts } from "@/components/platform-login/use-connected-accounts";
import { Button } from "@/components/ui/button";
import { WizardShell } from "@/components/onboarding/wizard-shell";
import { ProfileStep } from "@/components/onboarding/profile-step";
import { BrandStrategistStep } from "@/components/onboarding/brand-strategist-step";
import { AccountsStep } from "@/components/onboarding/accounts-step";
import { VoiceStep } from "@/components/onboarding/voice-step";
import { LaunchStep } from "@/components/onboarding/launch-step";
import {
  healthyAccount,
  isHealthyAccount,
  type ConnectableAccount,
} from "@/lib/instagram-account-health";
import type { OnboardingBrandProfile } from "@/lib/onboarding-profile";
import {
  ONBOARDING_STEP,
  type OnboardingStep,
} from "@/lib/onboarding-steps";

type OnboardingAccount = ConnectableAccount;

interface OnboardingWizardProps {
  hostedLoginEnabled?: boolean;
  name: string;
  accounts: OnboardingAccount[];
  persona: string;
  initialAccountId?: string | null;
  hasTarget?: boolean;
  initialStep?: OnboardingStep;
  brandProfile?: OnboardingBrandProfile | null;
  brandProfileUnavailable?: boolean;
}

export function OnboardingWizard({
  hostedLoginEnabled = false,
  name,
  accounts,
  persona,
  initialAccountId = null,
  hasTarget = false,
  initialStep = ONBOARDING_STEP.profile,
  brandProfile = null,
  brandProfileUnavailable = false,
}: OnboardingWizardProps) {
  const [step, setStep] = useState(initialStep);
  const [displayName, setDisplayName] = useState(name);
  const [currentPersona, setCurrentPersona] = useState(persona);
  const [targetConfigured, setTargetConfigured] = useState(hasTarget);

  // The account list is server-rendered, but a connection made during this
  // wizard has to be usable by Voice and Launch immediately. Holding it in
  // state is what lets the newly connected account flow forward without the
  // page reload the previous version needed.
  const { accounts: refreshedAccounts, setAccounts: setConnectedAccounts, refreshFailed } = useConnectedAccounts(accounts);
  const connectedAccounts = refreshedAccounts ?? accounts;
  const [activeAccountId, setActiveAccountId] = useState(
    initialAccountId ?? healthyAccount(accounts)?.id ?? null,
  );
  const addConnectedAccount = (account: OnboardingAccount) => {
    setConnectedAccounts((current) => {
      const rows = current ?? [];
      const existing = rows.findIndex((item) => item.id === account.id);
      if (existing === -1) return [...rows, account];
      return rows.map((item, index) => (index === existing ? account : item));
    });
    setActiveAccountId(account.id);
    if (isHealthyAccount(account)) setStep(ONBOARDING_STEP.brand);
  };
  const activeAccount = connectedAccounts.find(
    (account) => account.id === activeAccountId && isHealthyAccount(account),
  );
  useEffect(() => {
    if (initialAccountId) setActiveAccountId(initialAccountId);
  }, [initialAccountId]);
  useEffect(() => {
    if (!activeAccount && step > ONBOARDING_STEP.accounts) setStep(ONBOARDING_STEP.accounts);
  }, [activeAccount, step]);

  return (
    <WizardShell currentStep={step}>
      {refreshFailed && <p role="status" className="mb-4 text-sm text-amber-400">
        We couldn&apos;t refresh account health. The status below may be out of date.
      </p>}
      {step === ONBOARDING_STEP.profile && (
        <ProfileStep
          defaultName={displayName}
          onSaved={(saved) => {
            setDisplayName(saved);
            setStep(ONBOARDING_STEP.accounts);
          }}
        />
      )}
      {step === ONBOARDING_STEP.accounts && (
        <AccountsStep
          hostedLoginEnabled={hostedLoginEnabled}
          connectedAccounts={connectedAccounts}
          activeAccountId={activeAccountId}
          onConnected={addConnectedAccount}
          onNext={() => setStep(ONBOARDING_STEP.brand)}
          onBack={() => setStep(ONBOARDING_STEP.profile)}
        />
      )}
      {step === ONBOARDING_STEP.brand && brandProfileUnavailable && (
        <div>
          <h2 className="text-2xl font-bold">Brand strategy unavailable</h2>
          <p className="mt-2 text-muted-foreground">
            We couldn&apos;t load your saved brand strategy.
          </p>
          <p className="mt-1 text-sm text-muted-foreground">
            Retry before editing this account so your saved strategy stays intact.
          </p>
          <div className="mt-8 flex gap-3">
            <Button
              variant="outline"
              type="button"
              onClick={() => setStep(ONBOARDING_STEP.accounts)}
            >
              Back
            </Button>
            <Button
              variant="gradient"
              type="button"
              onClick={() => window.location.reload()}
            >
              Retry
            </Button>
          </div>
        </div>
      )}
      {step === ONBOARDING_STEP.brand && !brandProfileUnavailable && (
        <BrandStrategistStep
          igAccountId={activeAccount?.id}
          initialProfile={
            brandProfile?.ig_account_id === activeAccount?.id
              ? brandProfile
              : null
          }
          onTargetConfigured={() => setTargetConfigured(true)}
          onSaved={({ persona: savedPersona }) => {
            setCurrentPersona(savedPersona);
            setStep(ONBOARDING_STEP.voice);
          }}
          onBack={() => setStep(ONBOARDING_STEP.accounts)}
        />
      )}
      {step === ONBOARDING_STEP.brand && (
        <ManualAudienceStep accountId={activeAccount?.id} onSaved={saved=>{setTargetConfigured(true);setCurrentPersona(saved);setStep(ONBOARDING_STEP.voice)}} />
      )}
      {step === ONBOARDING_STEP.voice && (
        <VoiceStep
          igAccountId={activeAccount?.id}
          defaultPersona={currentPersona}
          onSaved={(saved) => {
            setCurrentPersona(saved);
            setStep(ONBOARDING_STEP.launch);
          }}
          onBack={() => setStep(ONBOARDING_STEP.brand)}
        />
      )}
      {step === ONBOARDING_STEP.launch && (
        <LaunchStep
          name={displayName}
          igAccountId={activeAccount?.id}
          accountCount={connectedAccounts.filter(isHealthyAccount).length}
          hasPersona={currentPersona.trim().length > 0}
          hasTarget={targetConfigured}
          onBack={() => setStep(ONBOARDING_STEP.voice)}
        />
      )}
    </WizardShell>
  );
}
