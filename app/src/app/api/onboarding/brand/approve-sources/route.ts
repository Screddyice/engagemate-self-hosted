import { proxyOnboardingJsonPost } from "@/lib/onboarding-route-proxy";

export async function POST(request: Request) {
  return proxyOnboardingJsonPost(request, "/v1/onboarding/brand/approve-sources");
}
