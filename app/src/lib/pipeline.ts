const PIPELINE_URL = process.env.PIPELINE_API_URL || "http://localhost:8000";
const PIPELINE_KEY = process.env.PIPELINE_API_KEY || "";

export async function triggerPipeline(params: {
  accountId: string;
  platform: string;
  accountUsername: string;
  accountConfig: Record<string, unknown>;
  runType: string;
}) {
  const res = await fetch(`${PIPELINE_URL}/pipeline/run`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": PIPELINE_KEY,
    },
    body: JSON.stringify({
      account_id: params.accountId,
      platform: params.platform,
      account_username: params.accountUsername,
      account_config: params.accountConfig,
      run_type: params.runType,
    }),
  });

  if (!res.ok) throw new Error(`Pipeline API error: ${res.status}`);
  return res.json();
}

export async function getPipelineStatus(runId: string) {
  const res = await fetch(`${PIPELINE_URL}/pipeline/status/${runId}`, {
    headers: { "X-API-Key": PIPELINE_KEY },
  });

  if (!res.ok) throw new Error(`Pipeline API error: ${res.status}`);
  return res.json();
}
