import { DEFAULT_FOLLOW_REEL_MESSAGE } from "@/lib/follow-reel-settings";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { FollowSettings } from "@/components/dashboard/follow-settings";
import { FollowStatus } from "@/components/dashboard/follow-status";
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
const fetchMock = vi.fn();
beforeEach(() => { vi.clearAllMocks(); vi.stubGlobal("fetch", fetchMock); });
const props = { accountId: "owned", enabled: false, dailyLimit: 3, autoUnfollowEnabled: true };
it("requires an explicit save and preserves independent follow/unfollow choices", async () => {
  fetchMock.mockResolvedValue({ ok: true }); render(<FollowSettings {...props} />);
  expect(screen.getByRole("checkbox", { name: "Follow after engagement" })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Automatically unfollow after 5 days" })).toBeChecked();
  expect(screen.getByLabelText("Daily follow limit")).toHaveValue(3);
  fireEvent.click(screen.getByRole("checkbox", { name: "Follow after engagement" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "Automatically unfollow after 5 days" }));
  expect(fetchMock).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Save follow settings" }));
  await screen.findByText("Follow settings saved.");
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ followAfterEngagement: true, dailyFollowLimit: 3, autoUnfollowEnabled: false, followReelMessageEnabled:false, followReelMessageTemplate:DEFAULT_FOLLOW_REEL_MESSAGE });
  expect(screen.getByText(/including overdue unfollows/)).toBeInTheDocument();
});
it("accepts zero without enabling follow and blocks invalid cap input", async () => {
  fetchMock.mockResolvedValue({ ok: true }); render(<FollowSettings {...props} />);
  fireEvent.change(screen.getByLabelText("Daily follow limit"), { target: { value: "21" } });
  expect(screen.getByRole("button", { name: "Save follow settings" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Daily follow limit"), { target: { value: "0" } });
  fireEvent.click(screen.getByRole("button", { name: "Save follow settings" }));
  await screen.findByText("Follow settings saved.");
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({ followAfterEngagement: false, dailyFollowLimit: 0 });
});
it("shows save failures without claiming settings were saved", async () => {
  fetchMock.mockResolvedValue({ ok: false }); render(<FollowSettings {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Save follow settings" }));
  await screen.findByRole("alert"); expect(screen.queryByText("Follow settings saved.")).not.toBeInTheDocument();
});
const pending = { igAccountId: "owned", pendingUnfollows: 1, uncertain: 1, records: [
  { id: "record", username: "prospect", status: "following", unfollowDueAt: "2026-09-19T00:00:00Z", cancellable: true },
  { id: "uncertain", username: "other", status: "uncertain", unfollowDueAt: null, cancellable: false },
] };
const response = (body: unknown) => ({ ok: true, json: async () => body });
it("shows paused pending unfollows and cancels only the selected record", async () => {
  fetchMock.mockResolvedValueOnce(response(pending)).mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce(response({ ...pending, pendingUnfollows: 0, records: [] }));
  render(<FollowStatus accountId="owned" autoUnfollowEnabled={false} />);
  await screen.findByText("1 pending · 1 uncertain"); expect(screen.getByText(/Scheduled unfollows are paused/)).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "Cancel unfollow" })).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Cancel unfollow" }));
  await screen.findByText("0 pending · 1 uncertain");
  expect(fetchMock.mock.calls[1][0]).toBe("/api/accounts/owned/follows");
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ id: "record" });
});
it("renders follow and reel receipt times in the account timezone", async () => {
  fetchMock.mockResolvedValue(response({...pending, records:[{...pending.records[0], messageStatus:"sent", messageSentAt:"2026-09-19T00:00:00Z", reelUrl:"https://www.instagram.com/reel/ABC_123/"}]}));
  render(<FollowStatus accountId="owned" autoUnfollowEnabled timezone="America/Los_Angeles" />);
  const expectedDue = new Date("2026-09-19T00:00:00Z").toLocaleString("en-US", { timeZone: "America/Los_Angeles", timeZoneName: "short" });
  const expectedSent = new Date("2026-09-19T00:00:00Z").toLocaleString("en-US", { timeZone: "America/Los_Angeles", timeZoneName: "short" });
  expect(await screen.findByText(`Unfollow due ${expectedDue}`)).toBeInTheDocument();
  expect(screen.getByText(new RegExp(`Reel message: Sent · ${expectedSent.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`))).toBeInTheDocument();
});
it("reports a cancellation race and preserves displayed history", async () => {
  fetchMock.mockResolvedValueOnce(response(pending)).mockResolvedValueOnce({ ok: false });
  render(<FollowStatus accountId="owned" autoUnfollowEnabled />); await screen.findByText("@prospect");
  fireEvent.click(screen.getByRole("button", { name: "Cancel unfollow" }));
  await screen.findByRole("alert"); expect(screen.getByText("@prospect")).toBeInTheDocument();
});
it("does not display another account's follow history while loading", async () => {
  fetchMock.mockResolvedValueOnce(response(pending)); let finish!: (value: unknown) => void;
  fetchMock.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const view = render(<FollowStatus accountId="owned" autoUnfollowEnabled />); await screen.findByText("@prospect");
  view.rerender(<FollowStatus accountId="second" autoUnfollowEnabled />); expect(screen.queryByText("@prospect")).not.toBeInTheDocument();
  await act(async () => finish(response({ ...pending, igAccountId: "foreign" })));
  await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  expect(screen.queryByText("@prospect")).not.toBeInTheDocument();
});

it.each([["pending", "Queued"], ["running", "Sending"], ["sent", "Sent"], ["uncertain", "Needs review"], ["cancelled", "Cancelled"]])("shows %s reel message status", async (messageStatus, label) => {
  fetchMock.mockResolvedValue(response({...pending, records:[{...pending.records[0], messageStatus, messageSentAt:"2026-09-26T12:00:00Z", reelUrl:"https://www.instagram.com/reel/ABC_123/"}]}));
  render(<FollowStatus accountId="owned" autoUnfollowEnabled />);
  expect(await screen.findByText(new RegExp("Reel message: " + label))).toBeInTheDocument();
  if (messageStatus === "sent") expect(screen.getByRole("link", {name:"View sent reel"})).toHaveAttribute("href", "https://www.instagram.com/reel/ABC_123/");
  else expect(screen.queryByRole("link", {name:"View sent reel"})).not.toBeInTheDocument();
});
it.each(["javascript:alert(1)", "https://evil.example/reel/a/", "https://www.instagram.com/reel/a/?redirect=evil"])("does not link an invalid sent reel %s", async (reelUrl) => {
  fetchMock.mockResolvedValue(response({...pending, records:[{...pending.records[0], messageStatus:"sent", reelUrl}]}));
  render(<FollowStatus accountId="owned" autoUnfollowEnabled />);
  await screen.findByText("Reel message: Sent");
  expect(screen.queryByRole("link", {name:"View sent reel"})).not.toBeInTheDocument();
});

it("keeps sent reel receipts visible for completed unfollows", async () => {
  fetchMock.mockResolvedValue(response({...pending, pendingUnfollows:0, records:[{...pending.records[0], status:"unfollowed", cancellable:false, messageStatus:"sent", reelUrl:"https://www.instagram.com/reel/ABC_123/"}]}));
  render(<FollowStatus accountId="owned" autoUnfollowEnabled />);
  await screen.findByText("unfollowed");
  expect(screen.getByRole("link", {name:"View sent reel"})).toBeInTheDocument();
  expect(screen.queryByRole("button", {name:"Cancel unfollow"})).not.toBeInTheDocument();
});
