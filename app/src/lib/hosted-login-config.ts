/** Server-only rollout flag. Never read a public build-time environment flag. */
export function hostedLoginEnabled(): boolean {
  return process.env.HOSTED_LOGIN_ENABLED === "true";
}
