export const DEFAULT_FOLLOW_REEL_MESSAGE = "Hey, hope you're doing well! Wanted to ask for your support on my latest reel. A like or comment would go a long way as I'm growing my account. Appreciate you!";

export function validFollowReelTemplate(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= 500 &&
    !/(?:[a-z][a-z0-9+.-]*:\/\/|www\.|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:\b|\/))/i.test(value) &&
    !/[\u0000-\u001f\u007f]/.test(value);
}
