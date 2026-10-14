import React from "react";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { WizardShell } from "@/components/onboarding/wizard-shell";

describe("WizardShell progress semantics regression", () => {
  it("identifies the current and completed onboarding steps", () => {
    render(
      <WizardShell currentStep={2}>
        <div>Accounts form</div>
      </WizardShell>,
    );

    const progress = screen.getByRole("list", {
      name: "Onboarding progress",
    });
    const completed = within(progress).getByRole("listitem", {
      name: "Profile, completed",
    });
    const current = within(progress).getByRole("listitem", {
      name: "Accounts, current step",
    });

    expect(completed).not.toHaveAttribute("aria-current");
    expect(current).toHaveAttribute("aria-current", "step");
    expect(
      within(progress).getByRole("listitem", { name: "Brand" }),
    ).not.toHaveAttribute("aria-current");
  });
});
