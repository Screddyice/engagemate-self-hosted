export const ONBOARDING_STEP = {
  profile: 1,
  accounts: 2,
  brand: 3,
  voice: 4,
  launch: 5,
} as const;

export type OnboardingStep =
  (typeof ONBOARDING_STEP)[keyof typeof ONBOARDING_STEP];
