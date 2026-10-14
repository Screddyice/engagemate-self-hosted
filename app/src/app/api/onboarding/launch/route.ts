import { proxyOnboardingJsonPost } from "@/lib/onboarding-route-proxy";

export function POST(request: Request) {
  return proxyOnboardingJsonPost(request, "/v1/onboarding/launch");
}
