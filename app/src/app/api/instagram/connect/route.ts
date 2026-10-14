import {
  loginBody,
  loginError,
  loginUser,
  requestLogin,
} from "@/lib/hosted-login-proxy";

const UUID_PATTERN = /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i;
export const dynamic = "force-dynamic";
export async function POST(req: Request) {
  const user = await loginUser(req, true);
  if (user instanceof Response) return user;
  const body = await loginBody(req);
  const username =
    typeof body?.username === "string"
      ? body.username.trim().replace(/^@/, "")
      : "";
  const timezone = body?.timezone;
  const accountId = body?.account_id;
  if (
    !/^[A-Za-z0-9_.]{1,30}$/.test(username) ||
    typeof timezone !== "string" ||
    timezone.length > 128
  )
    return loginError("invalid_input", 400);
  try {
    new Intl.DateTimeFormat("en", { timeZone: timezone });
  } catch {
    return loginError("invalid_input", 400);
  }
  if (accountId !== undefined &&
    (typeof accountId !== "string" || !UUID_PATTERN.test(accountId)))
    return loginError("invalid_input", 400);
  return requestLogin(user, "/v1/connect", "POST", {
    username,
    timezone,
    ...(typeof accountId === "string" ? { account_id: accountId } : {}),
  });
}
