import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WeeklyReportSettings } from "@/components/settings/weekly-report-settings";

const INITIAL_PREFERENCE = {
  enabled: true,
  timezone: "Asia/Makassar",
  sendHour: 9,
};

describe("WeeklyReportSettings", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the report controls, recipient, and time-saved method", () => {
    render(
      <WeeklyReportSettings
        initialPreference={INITIAL_PREFERENCE}
        recipientEmail="manager@example.test"
      />,
    );

    expect(
      screen.getByRole("switch", { name: "Weekly growth report" }),
    ).toHaveAttribute("aria-checked", "true");
    expect(screen.getByLabelText("Report timezone")).toHaveValue(
      "Asia/Makassar",
    );
    const sendHour = screen.getByRole("combobox", { name: "Send hour" });
    expect(sendHour).toHaveValue("9");
    expect(sendHour.querySelectorAll("option")).toHaveLength(24);
    const recipient = screen.getByLabelText("Report recipient");
    expect(recipient).toHaveValue("manager@example.test");
    expect(recipient).toHaveAttribute("readonly");
    recipient.focus();
    expect(recipient).toHaveFocus();
    expect(recipient).toHaveClass(
      "focus-visible:ring-2",
      "focus-visible:ring-ring",
      "focus-visible:ring-offset-2",
    );
    expect(
      screen.getByText(
        "Estimated manual time saved = 30 seconds per Story opportunity reviewed, plus 60 seconds per sent reply or DM.",
      ),
    ).toBeInTheDocument();
  });

  it("sends only the exact preference shape and confirms the save", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({
        ...INITIAL_PREFERENCE,
        enabled: false,
      }),
    });
    const user = userEvent.setup();
    render(
      <WeeklyReportSettings
        initialPreference={INITIAL_PREFERENCE}
        recipientEmail="manager@example.test"
      />,
    );

    await user.click(
      screen.getByRole("switch", { name: "Weekly growth report" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/weekly-report",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: false,
          timezone: "Asia/Makassar",
          sendHour: 9,
        }),
      },
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Weekly report settings saved.",
    );
  });

  it.each([400, 422])(
    "shows validation guidance for an invalid preference response (%s)",
    async (status) => {
      fetchMock.mockResolvedValue({ ok: false, status });
      const user = userEvent.setup();
      render(
        <WeeklyReportSettings
          initialPreference={INITIAL_PREFERENCE}
          recipientEmail="manager@example.test"
        />,
      );

      await user.click(screen.getByRole("button", { name: "Save changes" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Settings were not saved. Check the timezone and send hour, then try again.",
      );
    },
  );

  it.each([401, 403, 404, 500, 503])(
    "shows stable retry guidance for auth and server failures (%s)",
    async (status) => {
      fetchMock.mockResolvedValue({
        ok: false,
        status,
        json: vi.fn(() => {
          throw new Error("server detail must not be read");
        }),
      });
      const user = userEvent.setup();
      render(
        <WeeklyReportSettings
          initialPreference={INITIAL_PREFERENCE}
          recipientEmail="manager@example.test"
        />,
      );

      await user.click(screen.getByRole("button", { name: "Save changes" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Settings were not saved. Try again.",
      );
      expect(screen.getByRole("alert")).not.toHaveTextContent(
        "Check the timezone",
      );
    },
  );

  it("shows stable retry guidance for a network failure", async () => {
    fetchMock.mockRejectedValue(new Error("provider socket detail"));
    const user = userEvent.setup();
    render(
      <WeeklyReportSettings
        initialPreference={INITIAL_PREFERENCE}
        recipientEmail="manager@example.test"
      />,
    );

    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Settings were not saved. Try again.",
    );
  });
});
