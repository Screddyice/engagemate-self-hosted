/** Connection disclosures must distinguish hosted credential relay from extension capture. */

import React from "react";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ExtensionPrivacyPage from "@/app/(marketing)/privacy/page";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("connection privacy policy", () => {
  it("names every session cookie the extension reads", () => {
    render(<ExtensionPrivacyPage />);

    for (const cookie of [
      "sessionid",
      "csrftoken",
      "ds_user_id",
      "mid",
      "ig_did",
      "rur",
    ]) {
      expect(screen.getByText(cookie)).toBeInTheDocument();
    }
  });

  it("states the transfer, encryption, and password boundary", () => {
    render(<ExtensionPrivacyPage />);
    const body = document.body.textContent ?? "";

    expect(body).toMatch(/HTTPS/);
    expect(body).toMatch(/encrypts the Instagram cookies at rest/i);
    expect(body).toMatch(/input passes through EngageMate/i);
    expect(body).toMatch(/does not read\s+or send the password/i);
    expect(body).not.toMatch(/never receives, requests, or stores/i);
  });

  it("describes transient IP geolocation without claiming typed identity or location", () => {
    render(<ExtensionPrivacyPage />);
    const body = document.body.textContent ?? "";

    expect(body).toMatch(/public IP address/i);
    expect(body).toMatch(/ipwho\.is/i);
    expect(body).toMatch(/not stored/i);
    expect(body).toMatch(/browser time zone/i);
    expect(body).not.toMatch(/Instagram handle you typed/i);
    expect(body).not.toMatch(/country you selected/i);
  });

  it("discloses hosted input relay, isolation, expiry, and the saved-session boundary", () => {
    render(<ExtensionPrivacyPage />);
    const body = document.body.textContent ?? "";

    expect(body).toMatch(/including credentials\s+and verification codes/i);
    expect(body).toMatch(/separate\s+sign-in tab/i);
    expect(body).toMatch(/no analytics or session-replay scripts/i);
    expect(body).toMatch(/not\s+show or store the temporary browser screen/i);
    expect(body).toMatch(/not store your\s+password, verification codes, or input logs/i);
    expect(body).toMatch(/expires after 10 minutes/i);
    expect(body).toMatch(/verify that the signed-in\s+Instagram account matches/i);
    expect(body).toMatch(/save its\s+encrypted session/i);
    expect(body).toMatch(/does not delete the saved session or\s+account connection records/i);
    expect(body).toMatch(/Logging out of a different browser does not revoke a hosted session/i);
  });

  it("states that the extension performs no engagement", () => {
    render(<ExtensionPrivacyPage />);

    expect(
      screen.getByText(/performs no likes, replies, DMs, follows, comments, or\s+posts/i),
    ).toBeInTheDocument();
  });

  it("gives a retention window, a deletion path, and a contact", () => {
    render(<ExtensionPrivacyPage />);
    const body = document.body.textContent ?? "";

    expect(body).toMatch(/while the account stays connected/i);
    expect(body).toMatch(/request deletion of a saved session or your account data/i);
    expect(body).not.toMatch(/deletes the stored\s+session immediately/i);
    expect(body).toMatch(/Neither action deletes the session EngageMate has already saved/i);
  });

  it("shows the configured support address", () => {
    vi.stubEnv("NEXT_PUBLIC_SUPPORT_EMAIL", "help@example.com");
    render(<ExtensionPrivacyPage />);

    expect(
      screen.getByRole("link", { name: "help@example.com" }),
    ).toHaveAttribute("href", "mailto:help@example.com");
  });

  it("names no support address when none is configured", () => {
    vi.stubEnv("NEXT_PUBLIC_SUPPORT_EMAIL", "");
    render(<ExtensionPrivacyPage />);
    const body = document.body.textContent ?? "";

    expect(screen.queryByRole("link", { name: /@/ })).toBeNull();
    expect(body).toMatch(/operator of this EngageMate service/i);
    expect(body).not.toMatch(/engagemate\.io/i);
  });
});
