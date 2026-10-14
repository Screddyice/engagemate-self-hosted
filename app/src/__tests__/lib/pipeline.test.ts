import { describe, it, expect, vi, beforeEach } from "vitest";

const mockFetch = vi.fn();
global.fetch = mockFetch;

describe("Pipeline Service", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    process.env.PIPELINE_API_URL = "http://test-pipeline:8000";
    process.env.PIPELINE_API_KEY = "test-key";
  });

  // Test 11
  it("should export triggerPipeline function", async () => {
    const { triggerPipeline } = await import("@/lib/pipeline");
    expect(triggerPipeline).toBeDefined();
  });

  // Test 12
  it("should export getPipelineStatus function", async () => {
    const { getPipelineStatus } = await import("@/lib/pipeline");
    expect(getPipelineStatus).toBeDefined();
  });

  // Test 13
  it("triggerPipeline should POST to pipeline API", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ runId: "run-123" }),
    });

    const { triggerPipeline } = await import("@/lib/pipeline");
    await triggerPipeline({
      accountId: "acc-1",
      platform: "x",
      accountUsername: "testuser",
      accountConfig: {},
      runType: "daily",
    });

    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/pipeline/run"),
      expect.objectContaining({ method: "POST" })
    );
  });

  // Test 14
  it("triggerPipeline should include account_id in body", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ runId: "run-123" }),
    });

    const { triggerPipeline } = await import("@/lib/pipeline");
    await triggerPipeline({
      accountId: "acc-1",
      platform: "reddit",
      accountUsername: "reddituser",
      accountConfig: { autoMode: true },
      runType: "daily",
    });

    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body.account_id).toBe("acc-1");
    expect(body.platform).toBe("reddit");
  });

  // Test 15
  it("triggerPipeline should throw on non-ok response", async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 500 });

    const { triggerPipeline } = await import("@/lib/pipeline");
    await expect(
      triggerPipeline({
        accountId: "acc-1",
        platform: "x",
        accountUsername: "user",
        accountConfig: {},
        runType: "daily",
      })
    ).rejects.toThrow("Pipeline API error: 500");
  });

  // Test 16
  it("getPipelineStatus should GET status by runId", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: "running" }),
    });

    const { getPipelineStatus } = await import("@/lib/pipeline");
    const result = await getPipelineStatus("run-456");

    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/pipeline/status/run-456"),
      expect.any(Object)
    );
    expect(result.status).toBe("running");
  });

  // Test 17
  it("getPipelineStatus should throw on non-ok response", async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 404 });

    const { getPipelineStatus } = await import("@/lib/pipeline");
    await expect(getPipelineStatus("bad-id")).rejects.toThrow(
      "Pipeline API error: 404"
    );
  });

  // Test 18
  it("triggerPipeline should send X-API-Key header", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
    });

    const { triggerPipeline } = await import("@/lib/pipeline");
    await triggerPipeline({
      accountId: "acc-1",
      platform: "x",
      accountUsername: "u",
      accountConfig: {},
      runType: "daily",
    });

    const headers = mockFetch.mock.calls[0][1].headers;
    expect(headers["X-API-Key"]).toBe("test-key");
  });
});
