import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NotificationRecipients } from "@/components/settings/notification-recipients";
afterEach(() => vi.unstubAllGlobals());
describe("notification recipient settings", () => {
  it("adds an address and preserves the primary", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ emails: ["extra@example.com"] })));
    vi.stubGlobal("fetch", fetchMock);
    render(<NotificationRecipients primaryEmail="owner@example.com" initialEmails={[]} />);
    await userEvent.type(screen.getByLabelText("Additional email address"), "extra@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Add email" }));
    expect(await screen.findByRole("button", { name: "Remove extra@example.com" })).toBeInTheDocument();
    expect(screen.getByText("owner@example.com")).toBeInTheDocument();
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ emails: ["extra@example.com"] });
  });
  it("removes an extra address", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"emails":[]}')));
    render(<NotificationRecipients primaryEmail="owner@example.com" initialEmails={["extra@example.com"]} />);
    await userEvent.click(screen.getByRole("button", { name: "Remove extra@example.com" }));
    await waitFor(() => expect(screen.queryByText("extra@example.com")).not.toBeInTheDocument());
  });
  it("keeps an address when saving fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"error":"Could not save"}', { status: 500 })));
    render(<NotificationRecipients primaryEmail="owner@example.com" initialEmails={["extra@example.com"]} />);
    await userEvent.click(screen.getByRole("button", { name: "Remove extra@example.com" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not save");
    expect(screen.getByText("extra@example.com")).toBeInTheDocument();
  });
});
