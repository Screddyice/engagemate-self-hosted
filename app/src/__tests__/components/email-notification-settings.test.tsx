import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EmailNotificationSettings } from "@/components/settings/email-notification-settings";

describe("EmailNotificationSettings", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("saves a switch as soon as it is flipped", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    render(
      <EmailNotificationSettings
        initialPreferences={{ activityEmails: true, onboardingEmails: true }}
      />,
    );

    await userEvent.click(screen.getByRole("switch", { name: "Daily activity summary" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/settings/email-preferences");
    expect(JSON.parse(init.body)).toEqual({
      activityEmails: false,
      onboardingEmails: true,
    });
    expect(
      screen.getByRole("switch", { name: "Daily activity summary" }),
    ).toHaveAttribute("aria-checked", "false");
  });

  it("puts the switch back when the save fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 500 })));
    render(
      <EmailNotificationSettings
        initialPreferences={{ activityEmails: true, onboardingEmails: true }}
      />,
    );

    await userEvent.click(screen.getByRole("switch", { name: "Setup emails" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("not saved");
    expect(screen.getByRole("switch", { name: "Setup emails" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });
});
