import { DEFAULT_FOLLOW_REEL_MESSAGE } from "@/lib/follow-reel-settings";
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BehaviorSettings } from "@/components/settings/behavior-settings";

const initial = { dailyViewLimit: 100, dailyLikeLimit: 6, dailyReplyLimit: 5, dailyDmLimit: 0, followAfterEngagement: true, dailyFollowLimit: 20, autoUnfollowEnabled: true };
const mockFetch = vi.fn();
beforeEach(() => { vi.clearAllMocks(); mockFetch.mockReset(); global.fetch = mockFetch; });
const save = () => screen.getByRole("button", { name: "Save behavior choices" });

describe("behavior choices", () => {
  it("starts with likes only and lets new users opt into reels without Story replies", async () => {
    mockFetch.mockResolvedValue({ok:true});
    render(<BehaviorSettings accountId="new" initial={{dailyViewLimit:0,dailyLikeLimit:11,dailyReplyLimit:0,dailyDmLimit:0,dailyReelMessageLimit:null,followAfterEngagement:false,dailyFollowLimit:3,autoUnfollowEnabled:false,followReelMessageEnabled:false}} planDailyActions={11} />);
    expect(screen.getByRole("checkbox",{name:"Like Stories"})).toBeChecked();
    for (const name of ["View Stories","Reply to Stories","Follow after engagement","Send a reel after following","Automatically unfollow after 5 days"]) expect(screen.getByRole("checkbox",{name})).not.toBeChecked();
    fireEvent.click(screen.getByRole("checkbox",{name:"Follow after engagement"}));
    fireEvent.click(screen.getByRole("checkbox",{name:"Send a reel after following"}));
    expect(screen.getByRole("spinbutton",{name:"Reel DMs daily target"})).toHaveValue(1);
    fireEvent.click(save());
    await screen.findByText("Behavior choices saved.");
    expect(JSON.parse(mockFetch.mock.calls[0][1].body)).toMatchObject({dailyReplyLimit:0,dailyReelMessageLimit:1,followAfterEngagement:true,followReelMessageEnabled:true,dailyViewLimit:0});
  });
  it("preserves separate three-reply and three-reel targets on save", async () => {
    mockFetch.mockResolvedValue({ok:true});
    render(<BehaviorSettings accountId="one" initial={{...initial,dailyReplyLimit:3,dailyReelMessageLimit:3,followReelMessageEnabled:true,warmupExempt:true}} planDailyActions={11} />);
    expect(screen.getByRole("spinbutton",{name:"Reel DMs daily target"})).toHaveValue(3);
    expect(screen.getByText(/Warm-up is waived/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox",{name:"Like Stories"}));
    fireEvent.click(save());
    await screen.findByText("Behavior choices saved.");
    const patch=JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(patch).toMatchObject({dailyReplyLimit:3,dailyReelMessageLimit:3});
    expect(patch).not.toHaveProperty("warmupExempt");
  });
  it("allows disabling reels after an invalid quota draft without overwriting the saved quota", async () => {
    mockFetch.mockResolvedValue({ok:true});
    render(<BehaviorSettings accountId="one" initial={{...initial,dailyReelMessageLimit:3,followReelMessageEnabled:true}} />);
    fireEvent.change(screen.getByRole("spinbutton",{name:"Reel DMs daily target"}),{target:{value:"-1"}});
    expect(save()).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox",{name:"Send a reel after following"}));
    fireEvent.click(save());
    await screen.findByText("Behavior choices saved.");
    const patch=JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(patch.followReelMessageEnabled).toBe(false);
    expect(patch).not.toHaveProperty("dailyReelMessageLimit");
  });
  it("loads mature targets without enabling a disabled DM behavior", () => {
    render(<BehaviorSettings accountId="one" initial={initial} />);
    expect(screen.getByRole("checkbox", { name: "Reply to Stories" })).toBeChecked();
    expect(screen.getByRole("spinbutton", { name: "Story replies daily target" })).toHaveValue(5);
    expect(screen.queryByRole("checkbox", { name: "Send direct messages" })).not.toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Follows daily target" })).toHaveValue(20);
    expect(save()).toBeDisabled();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("saves unchecked actions as zero and preserves independent automatic unfollows", async () => {
    mockFetch.mockResolvedValue({ ok: true });
    const pending = vi.fn();
    render(<BehaviorSettings accountId="one" initial={initial} onPending={pending} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "Like Stories" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Follow after engagement" }));
    fireEvent.click(save());
    await screen.findByText("Behavior choices saved.");
    expect(JSON.parse(mockFetch.mock.calls[0][1].body)).toEqual({
      dailyViewLimit:100, dailyLikeLimit:0, dailyReplyLimit:5, dailyDmLimit:0, dailyReelMessageLimit:null,
      followAfterEngagement:false, dailyFollowLimit:0, autoUnfollowEnabled:true,
      followReelMessageEnabled:false, followReelMessageTemplate:DEFAULT_FOLLOW_REEL_MESSAGE,
    });
    expect(pending).toHaveBeenLastCalledWith(false);
  });

  it("keeps launch blocked after a failed save", async () => {
    mockFetch.mockResolvedValue({ ok:false, json:async () => ({ error:"Plan allowance exceeded" }) });
    const pending = vi.fn();
    render(<BehaviorSettings accountId="one" initial={initial} onPending={pending} />);
    fireEvent.click(screen.getByRole("checkbox", { name:"Reply to Stories" }));
    fireEvent.click(save());
    await screen.findByRole("alert");
    expect(pending).toHaveBeenLastCalledWith(true);
    expect(save()).toBeEnabled();
  });

  it("does not save an enabled behavior with an invalid target", () => {
    render(<BehaviorSettings accountId="one" initial={initial} />);
    fireEvent.change(screen.getByRole("spinbutton", { name:"Story replies daily target" }), { target:{value:"0"} });
    expect(save()).toBeDisabled();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("blocks edits and duplicate saves while a request is in flight", async () => {
    let finish!: (value: {ok:boolean}) => void;
    mockFetch.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    render(<BehaviorSettings accountId="one" initial={initial} />);
    fireEvent.click(screen.getByRole("checkbox", { name:"Like Stories" }));
    fireEvent.click(save());
    expect(screen.getByRole("checkbox", { name:"Reply to Stories" })).toBeDisabled();
    expect(screen.getByRole("button", { name:"Saving behaviors…" })).toBeDisabled();
    await act(async () => finish({ok:true}));
    await waitFor(() => expect(mockFetch).toHaveBeenCalledOnce());
  });
  it("does not clear a new account's pending edits when an old save completes", async () => {
    let finish!: (value: {ok:boolean}) => void;
    mockFetch.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const pending = vi.fn();
    const view = render(<BehaviorSettings key="one" accountId="one" initial={initial} onPending={pending} />);
    fireEvent.click(screen.getByRole("checkbox", {name:"Like Stories"}));
    fireEvent.click(save());
    view.rerender(<BehaviorSettings key="two" accountId="two" initial={initial} onPending={pending} />);
    fireEvent.click(screen.getByRole("checkbox", {name:"Reply to Stories"}));
    pending.mockClear();
    await act(async () => finish({ok:true}));
    expect(pending).not.toHaveBeenCalled();
    expect(save()).toBeEnabled();
  });

  it("enforces the displayed plan ceilings without enabling follow-only execution", () => {
    render(<BehaviorSettings accountId="one" initial={initial} planDailyActions={11} planDailyViews={100} />);
    fireEvent.change(screen.getByRole("spinbutton", {name:"Story replies daily target"}), {target:{value:"12"}});
    expect(save()).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", {name:"Reply to Stories"}));
    fireEvent.click(screen.getByRole("checkbox", {name:"Like Stories"}));
    expect(screen.getByRole("alert")).toHaveTextContent("Enable Story likes or replies");
    expect(save()).toBeDisabled();
  });

  it("defaults reel messaging off and saves opt-in with an editable signature", async () => {
    mockFetch.mockResolvedValue({ok:true});
    render(<BehaviorSettings accountId="one" initial={initial} />);
    const toggle = screen.getByRole("checkbox", {name:"Send a reel after following"});
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    fireEvent.change(screen.getByRole("textbox", {name:"Reel message"}), {target:{value:"Thanks for your support! -Sam"}});
    fireEvent.click(save());
    await screen.findByText("Behavior choices saved.");
    expect(JSON.parse(mockFetch.mock.calls[0][1].body)).toMatchObject({followReelMessageEnabled:true, followReelMessageTemplate:"Thanks for your support! -Sam"});
  });
  it("blocks custom links and preserves saved opt-in when loaded", () => {
    render(<BehaviorSettings accountId="one" initial={{...initial, followReelMessageEnabled:true, followReelMessageTemplate:"Thanks!"}} />);
    expect(screen.getByRole("checkbox", {name:"Send a reel after following"})).toBeChecked();
    fireEvent.change(screen.getByRole("textbox", {name:"Reel message"}), {target:{value:"Visit example.com"}});
    expect(save()).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("without links");
  });

  it("lets users turn messaging off even after an invalid draft edit", async () => {
    mockFetch.mockResolvedValue({ok:true});
    render(<BehaviorSettings accountId="one" initial={{...initial, followReelMessageEnabled:true}} />);
    fireEvent.change(screen.getByRole("textbox", {name:"Reel message"}), {target:{value:"Visit example.com"}});
    fireEvent.click(screen.getByRole("checkbox", {name:"Send a reel after following"}));
    fireEvent.click(save());
    await screen.findByText("Behavior choices saved.");
    const patch = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(patch.followReelMessageEnabled).toBe(false);
    expect(patch).not.toHaveProperty("followReelMessageTemplate");
  });

});
