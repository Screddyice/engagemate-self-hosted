import { randomBytes } from "node:crypto";
import { hostedLoginDocument } from "@/lib/hosted-login-guided-view";
import { PRIVATE_HEADERS, sessionRequest } from "@/lib/hosted-login-proxy";
export const dynamic = "force-dynamic";
export async function GET(
  req: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  // Ownership is checked by the private service before serving even the viewer.
  const status = await sessionRequest(req, (await params).id);
  if (!status.ok) return status;
  const body = (await status.json()) as { state: string; expires_at: string; username?: string };
  if (body.state !== "active" && body.state !== "connected")
    return new Response("This connection has finished. Return to EngageMate.", {
      status: 410,
      headers: {
        ...PRIVATE_HEADERS,
        "Content-Type": "text/plain; charset=utf-8",
      },
    });
  const nonce = randomBytes(24).toString("base64");
  return new Response(hostedLoginDocument((await params).id, nonce, body.expires_at, body.username ?? ""), {
    headers: {
      ...PRIVATE_HEADERS,
      "Content-Type": "text/html; charset=utf-8",
      "Content-Security-Policy": `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'`,
      "Cross-Origin-Opener-Policy": "same-origin",
      "X-Frame-Options": "DENY",
      "Permissions-Policy":
        "camera=(), microphone=(), geolocation=(), payment=(), clipboard-read=(), clipboard-write=()",
    },
  });
}
