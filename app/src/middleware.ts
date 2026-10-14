import { NextResponse, type NextRequest } from "next/server";
import { OWNER_COOKIE, validOwnerSession } from "@/lib/owner-session";
export default async function middleware(req: NextRequest) {
  const path = req.nextUrl.pathname;
  if (path === "/api/health") return NextResponse.next();
  const origin = process.env.NEXT_PUBLIC_APP_URL || "http://localhost:3030";
  if (req.headers.get("host") !== new URL(origin).host) return new NextResponse("Untrusted host", {status: 403});
  const extension = ["/api/extension/session", "/api/extension/suggest-geo", "/api/accounts/auth-sync", "/api/accounts/health"].includes(path) && req.headers.has("authorization");
  if (!["GET", "HEAD", "OPTIONS"].includes(req.method) && !extension && req.headers.get("origin") !== new URL(origin).origin) {
    return new NextResponse("Untrusted origin", {status: 403});
  }
  if (path === "/setup" || path === "/api/setup" || path === "/api/health" || path.startsWith("/unsubscribe") || path === "/api/email/unsubscribe" || extension) return NextResponse.next();
  if (await validOwnerSession(req.cookies.get(OWNER_COOKIE)?.value)) return NextResponse.next();
  if (path.startsWith("/api/")) return NextResponse.json({error: "Owner setup required"}, {status: 401});
  const setup = new URL("/setup", req.url);
  setup.searchParams.set("return_to", req.nextUrl.pathname + req.nextUrl.search);
  return NextResponse.redirect(setup);
}
export const config = { matcher: ["/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|zip|webmanifest)).*)", "/api/:path*"] };
