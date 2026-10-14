type LaunchErrorBody = {
  error?: string;
  detail?: string;
};

export async function launchOnboardingAccount(
  igAccountId: string,
  fallbackMessage = "Launch failed",
): Promise<void> {
  const selected = await fetch("/api/accounts/active", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ igAccountId }),
  });
  if (!selected.ok) {
    throw new Error("Could not select the Instagram account for launch");
  }

  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const response = await fetch("/api/onboarding/launch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ig_account_id: igAccountId, timezone }),
  });

  if (response.ok) return;

  const body = (await response.json().catch(() => ({}))) as LaunchErrorBody;
  throw new Error(body.error || body.detail || fallbackMessage);
}
