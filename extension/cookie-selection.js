// Small pure helpers shared by the service worker and its Node regression test.
(function installCookieSelection(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.EngageMateCookieSelection = api;
  }
})(globalThis, () => {
  function mapSameSite(sameSite) {
    if (sameSite === "no_restriction") return "None";
    if (sameSite === "lax") return "Lax";
    if (sameSite === "strict") return "Strict";
    if (sameSite === "unspecified" || sameSite == null) return "unspecified";
    return String(sameSite);
  }

  function cookieToPayload(cookie) {
    const payload = {
      name: cookie.name,
      value: cookie.value,
      domain: cookie.domain,
      path: cookie.path || "/",
      secure: Boolean(cookie.secure),
      httpOnly: Boolean(cookie.httpOnly),
      sameSite: mapSameSite(cookie.sameSite),
    };
    if (
      typeof cookie.expirationDate === "number" &&
      Number.isFinite(cookie.expirationDate) &&
      cookie.expirationDate > 0
    ) {
      payload.expirationDate = cookie.expirationDate;
    }
    return payload;
  }

  function selectInstagramCookies(cookies, allowedNames) {
    if (!Array.isArray(cookies)) return [];
    const selected = new Map();
    const rank = (domain) =>
      domain === "www.instagram.com" ? 2 : domain === ".instagram.com" ? 1 : 0;
    for (const cookie of cookies) {
      if (!allowedNames.includes(cookie.name)) continue;
      const current = selected.get(cookie.name);
      const domain = String(cookie.domain || "").toLowerCase();
      const currentDomain = String(current?.domain || "").toLowerCase();
      if (
        !current ||
        rank(domain) > rank(currentDomain) ||
        (rank(domain) === rank(currentDomain) &&
          String(cookie.path || "/").length > String(current.path || "/").length)
      ) {
        selected.set(cookie.name, cookie);
      }
    }
    return Array.from(selected.values()).map(cookieToPayload);
  }

  function identityFromCookies(cookies) {
    const identity = cookies.find((cookie) => cookie.name === "ds_user_id");
    return identity && typeof identity.value === "string"
      ? identity.value
      : null;
  }

  return { identityFromCookies, selectInstagramCookies };
});
