import { beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ auth: vi.fn(), user: vi.fn(), list: vi.fn(), save: vi.fn() }));
vi.mock("@/lib/owner-auth", () => ({ auth: mocks.auth }));
vi.mock("@/lib/queries", () => ({ getUserByOwnerKey: mocks.user }));
vi.mock("@/db", () => ({ db: {} }));
vi.mock("@/lib/notification-recipients", async importOriginal => ({
  ...await importOriginal<typeof import("@/lib/notification-recipients")>(),
  getNotificationRecipients: mocks.list,
  saveNotificationRecipients: mocks.save,
}));
import { GET, PUT } from "@/app/api/settings/notification-recipients/route";
import { parseRecipientList } from "@/lib/notification-recipients";
const request = (body: unknown) => new Request("http://localhost/api/settings/notification-recipients", { method: "PUT", body: JSON.stringify(body) });
beforeEach(() => {
  vi.clearAllMocks(); mocks.auth.mockResolvedValue({ userId: "clerk-owner" });
  mocks.user.mockResolvedValue({ id: "owner", email: "owner@example.com" });
  mocks.list.mockResolvedValue(["extra@example.com"]); mocks.save.mockResolvedValue(undefined);
});
describe("notification recipient API", () => {
  it("requires authentication", async () => {
    mocks.auth.mockResolvedValue({ userId: null });
    expect((await GET()).status).toBe(401); expect((await PUT(request({ emails: [] }))).status).toBe(401);
    expect(mocks.save).not.toHaveBeenCalled();
  });
  it("uses authenticated ownership and excludes primary and duplicates", async () => {
    const response = await PUT(request({ userId: "other-owner", emails: [" Extra@Example.com ", "extra@example.com", "OWNER@example.com"] }));
    expect(response.status).toBe(200);
    expect(mocks.save).toHaveBeenCalledWith("owner", ["extra@example.com"]);
    expect(await response.json()).toEqual({ emails: ["extra@example.com"] });
  });
  it("reads only the owner's recipients", async () => {
    expect((await GET()).status).toBe(200); expect(mocks.list).toHaveBeenCalledWith("owner");
  });
  it("allows removing all extra addresses", async () => {
    expect((await PUT(request({ emails: [] }))).status).toBe(200);
    expect(mocks.save).toHaveBeenCalledWith("owner", []);
  });
  it.each([null, {}, { emails: "x@example.com" }, { emails: ["bad"] }, { emails: ["a\r\nBcc:x@example.com"] }, { emails: Array(6).fill("x@example.com") }])("rejects invalid input %j", async body => {
    expect((await PUT(request(body))).status).toBe(400); expect(mocks.save).not.toHaveBeenCalled();
  });
  it.each(["a..b@example.com", "a@-example.com", "a@foo..com", ".a@example.com", "a.@example.com"])('rejects malformed address %s', email => {
    expect(parseRecipientList([email])).toBeNull();
  });
});
