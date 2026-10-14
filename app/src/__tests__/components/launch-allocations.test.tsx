import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LaunchStep } from "@/components/onboarding/launch-step";

const mockFetch = vi.fn();
const props = { name: "Sam", accountCount: 1, hasPersona: true, hasTarget: true, igAccountId: "account-1", onBack: vi.fn() };
const saved = { followAfterEngagement: false, autoUnfollowEnabled: true, dailyFollowLimit: 3, igAccountId: "account-1", dailyViewLimit: 0, dailyLikeLimit: 0, dailyReplyLimit: 0, dailyDmLimit: 0 };
const response = (body = saved) => ({ ok: true, json: async () => body });
const launch = () => screen.getByRole("button", { name: "Launch EngageMate" });

describe("Launch saved allocations", () => {
  beforeEach(() => { vi.clearAllMocks(); mockFetch.mockReset(); global.fetch = mockFetch; });

  it("shows all saved zeros and explains that launch does not raise them", async () => {
    mockFetch.mockResolvedValueOnce(response());
    render(<LaunchStep {...props} />);
    await screen.findByText(/All four allocations are zero/);
    for (const label of ["Story views", "Likes", "Replies"]) {
      expect(within(screen.getByText(label).parentElement!).getByText("0")).toBeInTheDocument();
    }
    expect(launch()).toBeEnabled();
    expect(mockFetch).toHaveBeenCalledOnce();
    expect(mockFetch).toHaveBeenCalledWith("/api/accounts/account-1/config", expect.objectContaining({ cache: "no-store" }));
    expect(mockFetch.mock.calls[0][1].method).toBeUndefined();
  });

  it("shows the saved allocations rather than defaults or today's warm-up caps", async () => {
    mockFetch.mockResolvedValueOnce(response({ ...saved, dailyViewLimit: 73, dailyLikeLimit: 9, dailyReplyLimit: 2, dailyDmLimit: 1 }));
    render(<LaunchStep {...props} />);
    await screen.findByText("73");
    expect(within(screen.getByText("Likes").parentElement!).getByText("9")).toBeInTheDocument();
    expect(screen.getByText(/A zero allocation disables that action/)).toBeInTheDocument();
    expect(screen.queryByText(/All four allocations are zero/)).not.toBeInTheDocument();
  });

  it("shows unavailable allocations as unknown and supports retry", async () => {
    mockFetch.mockRejectedValueOnce(new Error("network unavailable"));
    mockFetch.mockResolvedValueOnce(response());
    render(<LaunchStep {...props} />);
    await screen.findByText(/Could not load saved allocations/);
    expect(screen.getAllByText("—")).toHaveLength(3);
    expect(launch()).toBeDisabled();
    expect(screen.queryByText(/All four allocations are zero/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry allocations" }));
    await waitFor(() => expect(launch()).toBeEnabled());
  });

  it("rejects an allocation response for a different account", async () => {
    mockFetch.mockResolvedValueOnce(response({ ...saved, igAccountId: "foreign-account" }));
    render(<LaunchStep {...props} />);
    await screen.findByText(/Could not load saved allocations/);
    expect(launch()).toBeDisabled();
  });

  it("clears the prior account's allocations while a newly selected account loads", async () => {
    mockFetch.mockResolvedValueOnce(response({ ...saved, dailyViewLimit: 73 }));
    let finish!: (value: ReturnType<typeof response>) => void;
    mockFetch.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const view = render(<LaunchStep {...props} />);
    await screen.findByText("73");
    view.rerender(<LaunchStep {...props} igAccountId="account-2" />);
    expect(screen.queryByText("73")).not.toBeInTheDocument();
    expect(launch()).toBeDisabled();
    await act(async () => { finish(response({ ...saved, igAccountId: "account-2", dailyLikeLimit: 4 })); });
    await waitFor(() => expect(launch()).toBeEnabled());
    expect(screen.getByText("4")).toBeInTheDocument();
  });
  it("blocks launch until edited behavior choices are saved and reloaded", async () => {
    mockFetch.mockResolvedValueOnce(response({...saved, dailyReplyLimit:5}));
    render(<LaunchStep {...props} />);
    await waitFor(() => expect(launch()).toBeEnabled());
    fireEvent.click(screen.getByRole("checkbox", {name:"Reply to Stories"}));
    expect(launch()).toBeDisabled();
    mockFetch.mockResolvedValueOnce({ok:true});
    mockFetch.mockResolvedValueOnce(response(saved));
    fireEvent.click(screen.getByRole("button", {name:"Save behavior choices"}));
    await waitFor(() => expect(launch()).toBeEnabled());
    expect(JSON.parse(mockFetch.mock.calls[1][1].body).dailyReplyLimit).toBe(0);
    expect(screen.getByRole("checkbox", {name:"Reply to Stories"})).not.toBeChecked();
  });

});
