"use client";

const STEPS = [
  { id: 1, label: "Profile" },
  { id: 2, label: "Accounts" },
  { id: 3, label: "Brand" },
  { id: 4, label: "Voice" },
  { id: 5, label: "Launch" },
];

interface WizardShellProps {
  currentStep: number;
  children: React.ReactNode;
}

export function WizardShell({ currentStep, children }: WizardShellProps) {
  return (
    <div>
      {/* Step indicator */}
      <div
        role="list"
        aria-label="Onboarding progress"
        className="mb-10 flex min-w-0 items-start"
      >
        {STEPS.map((step, i) => (
          <div
            key={step.id}
            role="listitem"
            aria-current={step.id === currentStep ? "step" : undefined}
            aria-label={
              step.id < currentStep
                ? `${step.label}, completed`
                : step.id === currentStep
                  ? `${step.label}, current step`
                  : step.label
            }
            className="contents"
          >
            <div className="flex w-12 shrink-0 flex-col items-center sm:w-auto">
              <div
                className={`flex h-8 w-8 items-center justify-center rounded-full text-sm font-semibold ${
                  step.id < currentStep
                    ? "bg-violet-600 text-white"
                    : step.id === currentStep
                      ? "bg-gradient-to-br from-violet-500 to-purple-600 text-white"
                      : "border border-muted-foreground/30 text-muted-foreground"
                }`}
              >
                {step.id < currentStep ? "\u2713" : step.id}
              </div>
              <span
                className={`mt-1 text-xs ${
                  step.id <= currentStep
                    ? "font-medium text-foreground"
                    : "text-muted-foreground"
                }`}
              >
                {step.label}
              </span>
            </div>
            {i < STEPS.length - 1 && (
              <div
                data-wizard-connector
                className={`mx-1 mt-4 h-px min-w-2 flex-1 sm:mx-2 sm:w-12 sm:flex-none ${
                  step.id < currentStep
                    ? "bg-violet-600"
                    : "bg-muted-foreground/30"
                }`}
              />
            )}
          </div>
        ))}
      </div>

      {/* Step content */}
      {children}
    </div>
  );
}
