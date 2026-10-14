import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { WarmupOptIn } from "@/components/dashboard/warmup-opt-in";

afterEach(() => vi.unstubAllGlobals());

it("requires confirmation and sends only the displayed limit increases", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true });
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();
  render(<WarmupOptIn accountId="owned" planDailyActions={11}
    current={{ like: 3, reply: 0, dm: 0 }} />);
  expect(fetchMock).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Review higher warm-up limits" }));
  expect(screen.getByText(/3 → 11 likes, 0 → 11 Story replies/)).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Confirm higher limits" }));
  expect(fetchMock).toHaveBeenCalledWith("/api/accounts/owned/config", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dailyLikeLimit: 11, dailyReplyLimit: 11 }),
  });
  expect(screen.getByRole("status")).toHaveTextContent("Maximums saved");
});

it("cancels without changing limits", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();
  render(<WarmupOptIn accountId="owned" planDailyActions={27}
    current={{ like: 3, reply: 0, dm: 0 }} />);
  await user.click(screen.getByRole("button", { name: "Review higher warm-up limits" }));
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(fetchMock).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Confirm higher limits" })).not.toBeInTheDocument();
});

it("does not offer an increase for already configured maximums", () => {
  render(<WarmupOptIn accountId="owned" planDailyActions={27}
    current={{ like: 27, reply: 20, dm: 0 }} />);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("retains higher existing limits and reports failed writes without success", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: false });
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();
  render(<WarmupOptIn accountId="owned" planDailyActions={27}
    current={{ like: 25, reply: 5, dm: 0 }} />);
  await user.click(screen.getByRole("button", { name: "Review higher warm-up limits" }));
  await user.click(screen.getByRole("button", { name: "Confirm higher limits" }));
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ dailyLikeLimit: 27, dailyReplyLimit: 20 });
  expect(screen.getByRole("alert")).toHaveTextContent("Could not save");
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
