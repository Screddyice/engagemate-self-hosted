/**
 * Public identity of this deployment, from configuration only.
 *
 * There is no built-in domain. The project's original one lapsed, and a
 * hardcoded name would send metadata, copy, and links to whoever registers it.
 * Both values are inlined at build time.
 */

/** NEXT_PUBLIC_APP_URL as a URL, or null when it is unset or malformed. */
export function appUrl(): URL | null {
  const value = process.env.NEXT_PUBLIC_APP_URL?.trim();
  if (!value) return null;
  try {
    return new URL(value);
  } catch {
    return null;
  }
}

/** Host to show in copy, such as "app.example.com". Null when unconfigured. */
export function appHost(): string | null {
  return appUrl()?.host ?? null;
}

/** NEXT_PUBLIC_SUPPORT_EMAIL, or null to leave the address out of the page. */
export function supportEmail(): string | null {
  const value = process.env.NEXT_PUBLIC_SUPPORT_EMAIL?.trim();
  return value && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value) ? value : null;
}
