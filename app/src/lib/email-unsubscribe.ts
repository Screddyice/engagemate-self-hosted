import { createHmac, timingSafeEqual } from "node:crypto";

/**
 * Login-free unsubscribe links. The Python email senders sign
 * `<user id>:<category>` with a key derived from INTERNAL_API_KEY
 * (api/engagemate/reports/unsubscribe.py); this file checks the same HMAC, so
 * a link can only turn off the one category it names for the user it was sent
 * to. Both containers hold the same INTERNAL_API_KEY.
 */
export const EMAIL_CATEGORIES = ["activity", "onboarding", "weekly_report"] as const;
export type EmailCategory = (typeof EMAIL_CATEGORIES)[number];

export const EMAIL_CATEGORY_LABELS: Record<EmailCategory, string> = {
  activity: "daily activity summary emails",
  onboarding: "setup emails",
  weekly_report: "weekly report emails",
};

const LABEL = "engagemate-email-unsubscribe-v1";
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function isCategory(value: unknown): value is EmailCategory {
  return (
    typeof value === "string" &&
    (EMAIL_CATEGORIES as readonly string[]).includes(value)
  );
}

export function unsubscribeToken(
  secret: string,
  userId: string,
  category: EmailCategory,
): string {
  const key = createHmac("sha256", secret).update(LABEL).digest();
  return createHmac("sha256", key)
    .update(`${userId.toLowerCase()}:${category}`)
    .digest("base64url");
}

export function verifyUnsubscribe(
  userId: unknown,
  category: unknown,
  token: unknown,
  secret: string | undefined = process.env.INTERNAL_API_KEY,
): { userId: string; category: EmailCategory } | null {
  if (!secret) return null;
  if (typeof userId !== "string" || !UUID_PATTERN.test(userId)) return null;
  if (!isCategory(category) || typeof token !== "string") return null;
  const expected = Buffer.from(unsubscribeToken(secret, userId, category));
  const given = Buffer.from(token);
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) {
    return null;
  }
  return { userId: userId.toLowerCase(), category };
}
