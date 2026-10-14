import { sessionRequest } from "@/lib/hosted-login-proxy";
export const dynamic = "force-dynamic";
export async function GET(req: Request, { params }: { params: Promise<{ id: string }> }) {
  return sessionRequest(req, (await params).id);
}
export async function DELETE(req: Request, { params }: { params: Promise<{ id: string }> }) {
  return sessionRequest(req, (await params).id, "", "DELETE");
}
