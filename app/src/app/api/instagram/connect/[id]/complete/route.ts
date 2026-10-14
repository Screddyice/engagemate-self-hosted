import { sessionRequest } from "@/lib/hosted-login-proxy";
export const dynamic = "force-dynamic";
export async function POST(req: Request, { params }: { params: Promise<{ id: string }> }) {
  return sessionRequest(req, (await params).id, "/complete", "POST");
}
