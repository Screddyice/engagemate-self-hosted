import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { identityFromCookies, selectInstagramCookies } = require("./cookie-selection.js");

test("prefers the exact account origin when cookie names collide", () => {
  const cookies = selectInstagramCookies(
    [
      {
        name: "sessionid",
        value: "parent-session",
        domain: ".instagram.com",
        path: "/",
        secure: true,
        httpOnly: true,
        sameSite: "lax",
      },
      {
        name: "sessionid",
        value: "origin-session",
        domain: "www.instagram.com",
        path: "/",
        secure: true,
        httpOnly: true,
        sameSite: "lax",
      },
      {
        name: "ds_user_id",
        value: "123",
        domain: "www.instagram.com",
        path: "/",
        secure: true,
        httpOnly: false,
        sameSite: "lax",
      },
    ],
    ["sessionid", "ds_user_id"],
  );

  assert.deepEqual(
    cookies.map(({ name, value, domain }) => ({ name, value, domain })),
    [
      { name: "sessionid", value: "origin-session", domain: "www.instagram.com" },
      { name: "ds_user_id", value: "123", domain: "www.instagram.com" },
    ],
  );
  assert.equal(identityFromCookies(cookies), "123");
});
