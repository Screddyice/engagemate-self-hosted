export function setupDestination(value: string | null, origin: string): string {
  if (!value?.startsWith("/")) return "/onboarding";
  try {
    const url = new URL(value, origin);
    return url.origin === origin ? url.pathname + url.search + url.hash : "/onboarding";
  } catch {
    return "/onboarding";
  }
}
