import { auth } from "@/lib/owner-auth";
import { redirect } from "next/navigation";

import { ConsentClient } from "./consent-client";

export default async function McpAuthorizePage({
  searchParams,
}: {
  searchParams: Promise<{ state?: string }>;
}) {
  const { userId } = await auth();
  const params = await searchParams;
  const state = typeof params.state === "string" ? params.state : "";
  if (!userId) {
    redirect(`/setup?return_to=${encodeURIComponent(`/mcp/authorize?state=${state}`)}`);
  }
  return <ConsentClient state={state} />;
}
