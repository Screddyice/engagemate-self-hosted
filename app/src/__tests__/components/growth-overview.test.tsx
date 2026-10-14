import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FollowerTrendChart } from "@/components/dashboard/follower-trend-chart";
import { GrowthOverview } from "@/components/dashboard/growth-overview";
import type { GrowthDashboard } from "@/lib/growth";

const COMPLETE_DATA: GrowthDashboard = {
  points: [
    { date: "2026-07-15", value: 1_080 },
    { date: "2026-08-07", value: 1_100 },
    { date: "2026-08-14", value: 1_108 },
  ],
  currentFollowers: 1_108,
  currentEngagementRateBps: 475,
  followerTrend7d: { start: 1_100, end: 1_108, delta: 8 },
  followerTrend30d: { start: 1_080, end: 1_108, delta: 28 },
  engagementTrend7d: { start: 430, end: 475, delta: 45 },
  engagementTrend30d: { start: 355, end: 475, delta: 120 },
  growthGoal: 2_000,
  activity7d: {
    reviewed: 4,
    likes: 2,
    replies: 1,
    dms: 2,
    views: 3,
    skipped: 2,
    gated: 1,
    failed: 1,
  },
  estimatedTimeSavedSeconds: 300,
};

describe("GrowthOverview", () => {
  it("renders a complete, accessible account growth brief", () => {
    render(<GrowthOverview data={COMPLETE_DATA} />);

    expect(screen.getByText("1,108")).toBeInTheDocument();
    expect(screen.getByText("4.75%")).toBeInTheDocument();
    expect(screen.getByText("+8")).toBeInTheDocument();
    expect(screen.getByText("+28")).toBeInTheDocument();
    expect(screen.getByText("+0.45 pp")).toBeInTheDocument();
    expect(screen.getByText("+1.20 pp")).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: /follower trend.*1,080.*1,108/i }),
    ).toBeInTheDocument();

    expect(screen.getByText("4 opportunities reviewed")).toBeInTheDocument();
    expect(
      screen.getByText("2 likes · 1 reply · 2 DMs · 3 views"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("2 skipped · 1 gated · 1 failed"),
    ).toBeInTheDocument();
    expect(screen.getByText("5 min")).toBeInTheDocument();
    expect(
      screen.getByText(
        "30 seconds per opportunity reviewed + 60 seconds per sent reply or DM.",
      ),
    ).toBeInTheDocument();

    expect(
      screen.getByRole("progressbar", { name: "Growth goal" }),
    ).toHaveAttribute("aria-valuenow", "55");
    const timePanel = screen.getByText("Estimated manual time saved").parentElement;
    expect(timePanel?.parentElement).toHaveClass("xl:grid-cols-3");
    expect(timePanel).toHaveClass("xl:border-r");
    expect(screen.getByText("1,108 of 2,000 followers")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Account trends are observational. EngageMate does not attribute follower or engagement changes to any individual target or action.",
      ),
    ).toBeInTheDocument();
  });

  it("marks every trend unavailable when only one snapshot exists", () => {
    render(
      <GrowthOverview
        data={{
          ...COMPLETE_DATA,
          points: [{ date: "2026-08-14", value: 1_108 }],
          followerTrend7d: null,
          followerTrend30d: null,
          engagementTrend7d: null,
          engagementTrend30d: null,
        }}
      />,
    );

    expect(screen.getAllByText("Not available yet")).toHaveLength(4);
    expect(screen.queryByText(/^0(?:\.00)?(?: pp)?$/)).not.toBeInTheDocument();
  });

  it("uses two wide columns and no trailing border when no goal is set", () => {
    render(
      <GrowthOverview data={{ ...COMPLETE_DATA, growthGoal: null }} />,
    );

    const timePanel = screen.getByText("Estimated manual time saved").parentElement;
    expect(timePanel?.parentElement).toHaveClass("xl:grid-cols-2");
    expect(timePanel?.parentElement).not.toHaveClass("xl:grid-cols-3");
    expect(timePanel).not.toHaveClass("xl:border-r");
  });
});

describe("FollowerTrendChart", () => {
  it("spaces irregular observations by elapsed date", () => {
    render(
      <FollowerTrendChart
        points={[
          { date: "2026-08-01", value: 100 },
          { date: "2026-08-02", value: 101 },
          { date: "2026-08-11", value: 110 },
        ]}
      />,
    );

    const polyline = screen.getByRole("img").querySelector("polyline");
    const xCoordinates = (polyline?.getAttribute("points") ?? "")
      .split(" ")
      .map((point) => Number(point.split(",")[0]));
    expect(xCoordinates).toHaveLength(3);
    expect(xCoordinates[1] - xCoordinates[0]).toBeLessThan(
      xCoordinates[2] - xCoordinates[1],
    );
  });

  it("renders no trend line for malformed snapshot dates", () => {
    render(
      <FollowerTrendChart
        points={[
          { date: "2026-02-30", value: 100 },
          { date: "2026-03-02", value: 101 },
        ]}
      />,
    );

    const chart = screen.getByRole("img", {
      name: "Follower trend is not available yet.",
    });
    expect(chart.querySelector("polyline")).not.toBeInTheDocument();
  });

  it("renders no trend line when separate values share one date", () => {
    render(
      <FollowerTrendChart
        points={[
          { date: "2026-08-14", value: 100 },
          { date: "2026-08-14", value: 101 },
        ]}
      />,
    );

    const chart = screen.getByRole("img", {
      name: "Follower trend is not available yet.",
    });
    expect(chart.querySelector("polyline")).not.toBeInTheDocument();
  });
});
