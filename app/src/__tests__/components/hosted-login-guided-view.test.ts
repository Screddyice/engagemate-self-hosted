import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { hostedLoginDocument } from "@/lib/hosted-login-guided-view";
const id = "12345678-1234-1234-1234-123456789abc";
const calls: Array<{ path: string; method?: string; body?: Record<string, unknown> }> = [];
let step: Record<string, unknown>;
let failInput = false;
let completed = false;
const navigate = vi.fn();
function runDocument() {
  new Function("window", document.querySelector("script")!.textContent!)({
    location: { replace: navigate },
    addEventListener: window.addEventListener.bind(window),
  });
}
const fetchMock = vi.fn(async (url: string, options: RequestInit) => {
  calls.push({ path: url, method: options.method,
    ...(options.body ? { body: JSON.parse(String(options.body)) } : {}) });
  if (url.endsWith("/step")) return Response.json(step);
  if (url.endsWith("/input")) {
    if (failInput) throw new Error("uncertain transport");
    const body = JSON.parse(String(options.body));
    if (body.type === "sign_in") step = { kind: "code", channel: "sms", invalid: false, options: ["resend_code"] };
    if (body.type === "submit_code") step = { kind: "ready", options: [] };
    return new Response(null, { status: 204 });
  }
  if (url.endsWith("/complete")) return completed
    ? Response.json({ state: "connected" })
    : Response.json({ code: "sign_in_required" }, { status: 422 });
  if (options.method === "DELETE") return new Response(null, { status: 204 });
  return Response.json({ state: "active" });
});
async function settle() { await vi.advanceTimersByTimeAsync(1); }
function input(id: string) { return document.getElementById(id) as HTMLInputElement; }
function button(id: string) { return document.getElementById(id) as HTMLButtonElement; }
beforeEach(async () => {
  vi.useFakeTimers();
  calls.length = 0; failInput = false; completed = false; navigate.mockClear();
  step = { kind: "login", options: [] };
  vi.stubGlobal("fetch", fetchMock);
  document.body.innerHTML = hostedLoginDocument(id, "fixture-nonce", new Date(Date.now() + 600_000).toISOString(), "demo_user");
  runDocument();
  await vi.waitFor(() => expect(document.getElementById("login")!.hidden).toBe(false));
});
afterEach(() => {
  window.dispatchEvent(new Event("pagehide"));
  vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals();
  document.body.innerHTML = "";
});
it("shows guided controls without exposing the private browser", () => {
  expect(input("username").value).toBe("demo_user");
  expect(document.querySelector("img,iframe,canvas,#screen")).toBeNull();
  expect(calls.some((call) => call.path.endsWith("/frame"))).toBe(false);
});
it("sends login once, clears the password, then shows the security-code step", async () => {
  input("password").value = "fixture-password";
  document.getElementById("login")!.dispatchEvent(new Event("submit", { cancelable: true }));
  expect(input("password").value).toBe("");
  expect(document.getElementById("status")!.textContent).toContain("Submitting your username and password");
  await settle();
  expect(calls.filter((call) => call.body?.type === "sign_in")).toHaveLength(1);
  expect(input("code").closest("form")!.hidden).toBe(false);
  expect(document.body.innerHTML).not.toContain("fixture-password");
  expect(document.getElementById("code-instructions")!.textContent).toContain("text message");
});
it("submits a code once and requires explicit identity verification", async () => {
  step = { kind: "code", channel: "authenticator", invalid: false, options: [] };
  button("check").click(); await settle();
  input("code").value = "123456";
  document.getElementById("code-form")!.dispatchEvent(new Event("submit", { cancelable: true }));
  expect(input("code").value).toBe("");
  await settle();
  expect(calls.filter((call) => call.body?.type === "submit_code")).toHaveLength(1);
  expect(button("finish").hidden).toBe(false);
  expect(calls.some((call) => call.path.endsWith("/complete"))).toBe(false);
  button("finish").click(); await settle();
  expect(document.getElementById("status")!.textContent).toContain("sign-in step");
  expect(navigate).not.toHaveBeenCalled();
  completed = true; button("finish").click(); await settle();
  expect(navigate).toHaveBeenCalledExactlyOnceWith("/onboarding");
  expect(document.getElementById("return")!.getAttribute("href")).toBe("/onboarding");
  expect(document.getElementById("done")!.hidden).toBe(false);
  expect(document.getElementById("controls")!.hidden).toBe(true);
});
it("offers only named security actions and never retries an uncertain input", async () => {
  step = { kind: "method", options: ["try_another_way"] };
  button("check").click(); await settle();
  expect(document.getElementById("choices")!.textContent).toContain("Try another way");
  failInput = true;
  (document.getElementById("choices")!.querySelector("button") as HTMLButtonElement).click();
  await settle(); await vi.advanceTimersByTimeAsync(3000);
  expect(calls.filter((call) => call.path.endsWith("/input"))).toHaveLength(1);
  expect(document.getElementById("status")!.textContent).toContain("interrupted");
});
it("cancels and stops polling", async () => {
  button("cancel").click(); await settle();
  expect(calls.some((call) => call.method === "DELETE")).toBe(true);
  expect(document.getElementById("controls")!.hidden).toBe(true);
  const count = calls.length;
  await vi.advanceTimersByTimeAsync(5000);
  expect(calls).toHaveLength(count);
});

it("puts WhatsApp instructions above the code field and updates when delivery changes", async () => {
  step = { kind: "code", channel: "whatsapp", invalid: false, options: [] };
  button("check").click(); await settle();
  expect(document.getElementById("code-title")!.textContent).toBe("Open WhatsApp on your phone");
  expect(document.getElementById("code-instructions")!.textContent).toContain("phone number linked to your Instagram account");
  expect(document.getElementById("code-instructions")!.compareDocumentPosition(input("code")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(document.getElementById("options-panel")!.hidden).toBe(true);
  step = { kind: "code", channel: "email", invalid: true, options: [] };
  button("check").click(); await settle();
  expect(document.getElementById("code-title")!.textContent).toBe("Check your email inbox");
  expect(document.getElementById("code-instructions")!.textContent).toContain("Spam or Junk");
  expect(document.getElementById("status")!.textContent).toContain("did not accept that code");
});

it("prefers SMS over WhatsApp once when Instagram offers both", async () => {
  step = { kind: "method", options: ["use_whatsapp", "use_email", "use_sms"] };
  button("check").click(); await settle();
  expect(calls.filter(call => call.body?.action === "use_sms")).toHaveLength(1);
  expect(calls.filter(call => call.body?.action === "use_whatsapp")).toHaveLength(0);
  await vi.advanceTimersByTimeAsync(5000);
  expect(calls.filter(call => call.body?.action === "use_sms")).toHaveLength(1);
});
it("opens an offered alternative from WhatsApp without requesting another WhatsApp code", async () => {
  step = { kind: "code", channel: "whatsapp", invalid: false, options: ["try_another_way", "resend_code"] };
  button("check").click(); await settle();
  expect(calls.filter(call => call.body?.action === "try_another_way")).toHaveLength(1);
  expect(calls.filter(call => call.body?.action === "resend_code")).toHaveLength(0);
});

it("uses a direct SMS alternative from the WhatsApp code screen", async () => {
  step = { kind: "code", channel: "whatsapp", invalid: false, options: ["use_sms", "use_backup_code"] };
  button("check").click(); await settle();
  expect(calls.filter(call => call.body?.action === "use_sms")).toHaveLength(1);
  expect(calls.filter(call => call.body?.action === "try_another_way")).toHaveLength(0);
  step = { kind: "code", channel: "sms", invalid: false, options: [] };
  button("check").click(); await settle();
  expect(document.getElementById("code-title")!.textContent).toBe("Check your text messages");
});

it("recovers a committed result even after the browser deadline", async () => {
  window.dispatchEvent(new Event("pagehide"));
  vi.clearAllTimers();
  fetchMock.mockImplementationOnce(async () => Response.json({
    state: "connected", account: { healthStatus: "paused" },
  }));
  document.body.innerHTML = hostedLoginDocument(id, "fixture-nonce", new Date(Date.now() - 60000).toISOString(), "demo_user");
  runDocument();
  await settle();
  expect(document.getElementById("status")!.textContent).toBe("Connection confirmed.");
  expect(navigate).toHaveBeenCalledExactlyOnceWith("/onboarding");
  expect(document.getElementById("controls")!.hidden).toBe(true);
});
