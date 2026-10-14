import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

const PREFIX = "em_xt_";
const AUDIENCE = "engagemate-extension-session";
const TTL_SECONDS = 30 * 24 * 60 * 60;

type ExtensionTicketPayload = {
  v: 1;
  aud: typeof AUDIENCE;
  sub: string;
  iat: number;
  exp: number;
  nonce: string;
};

function signingKey() {
  const key = process.env.INTERNAL_API_KEY || process.env.PIPELINE_API_KEY || "";
  if (!key) throw new Error("Extension ticket signing key is not configured");
  return createHmac("sha256", key)
    .update("engagemate-extension-ticket-v1")
    .digest();
}

function signature(payload: string) {
  return createHmac("sha256", signingKey()).update(payload).digest("base64url");
}

export function mintExtensionTicket(userId: string, now = Date.now()) {
  const issuedAt = Math.floor(now / 1000);
  const body: ExtensionTicketPayload = {
    v: 1,
    aud: AUDIENCE,
    sub: userId,
    iat: issuedAt,
    exp: issuedAt + TTL_SECONDS,
    nonce: randomBytes(16).toString("base64url"),
  };
  const payload = Buffer.from(JSON.stringify(body)).toString("base64url");
  return `${PREFIX}${payload}.${signature(payload)}`;
}

export function verifyExtensionTicket(token: string, now = Date.now()): string | null {
  if (!token.startsWith(PREFIX) || token.length > 2_048) return null;
  const encoded = token.slice(PREFIX.length);
  const separator = encoded.lastIndexOf(".");
  if (separator <= 0) return null;
  const payload = encoded.slice(0, separator);
  const supplied = encoded.slice(separator + 1);
  let expected: string;
  try {
    expected = signature(payload);
  } catch {
    return null;
  }
  const suppliedBytes = Buffer.from(supplied);
  const expectedBytes = Buffer.from(expected);
  if (
    suppliedBytes.length !== expectedBytes.length ||
    !timingSafeEqual(suppliedBytes, expectedBytes)
  ) {
    return null;
  }

  try {
    const body = JSON.parse(
      Buffer.from(payload, "base64url").toString("utf8"),
    ) as Partial<ExtensionTicketPayload>;
    const current = Math.floor(now / 1000);
    if (
      body.v !== 1 ||
      body.aud !== AUDIENCE ||
      typeof body.sub !== "string" ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
        body.sub,
      ) ||
      typeof body.iat !== "number" ||
      typeof body.exp !== "number" ||
      body.iat > current + 60 ||
      body.exp <= current ||
      body.exp - body.iat !== TTL_SECONDS
    ) {
      return null;
    }
    return body.sub;
  } catch {
    return null;
  }
}

export const EXTENSION_TICKET_TTL_SECONDS = TTL_SECONDS;
