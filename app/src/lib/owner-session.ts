// Shared with Edge middleware; never send the owner access key to client bundles.
export const OWNER_COOKIE = "engagemate_owner";
const AGE = 30 * 24 * 60 * 60;
export async function ownerSessionValue(now = Date.now()): Promise<string | null> {
  const secret = process.env.OWNER_ACCESS_TOKEN;
  if (!secret || secret.length < 32) return null;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  const day = Math.floor(now / 86400000);
  const bytes = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`owner:${day}`));
  return `${day}.${Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2,"0")).join("")}`;
}
export async function validOwnerSession(value: string | undefined): Promise<boolean> {
  if (!value || !/^\d+\.[a-f0-9]{64}$/.test(value)) return false;
  const day = Number(value.split(".")[0]);
  const now = Date.now();
  if (day * 86400000 > now || now - day * 86400000 > AGE * 1000) return false;
  const expected = await ownerSessionValue(day * 86400000);
  if (!expected || expected.length !== value.length) return false;
  let diff = 0; for (let i=0;i<value.length;i++) diff |= expected.charCodeAt(i) ^ value.charCodeAt(i);
  return diff === 0;
}
