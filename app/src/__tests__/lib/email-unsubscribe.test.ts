import { describe, expect, it } from "vitest";
import {
  unsubscribeToken,
  verifyUnsubscribe,
} from "@/lib/email-unsubscribe";

const SECRET = "test-internal-key";
const USER = "11111111-1111-4111-8111-111111111111";

describe("email unsubscribe tokens", () => {
  it("matches the token the Python senders put in email footers", () => {
    // tests/reports/test_unsubscribe.py asserts the same values.
    expect(unsubscribeToken(SECRET, USER, "activity")).toBe(
      "eybfO-LGLXcdkNUa3FpsBKwzAa-pdPhbwXu2Y4kfL1Q",
    );
    expect(unsubscribeToken(SECRET, USER, "weekly_report")).toBe(
      "PR1dqN1IIxGL1YhP_59jCSkaWTuBLCY_0Ks8SPR1CmY",
    );
  });

  it("accepts a link only for the user, category and key it was signed for", () => {
    const token = unsubscribeToken(SECRET, USER, "activity");
    expect(verifyUnsubscribe(USER, "activity", token, SECRET)).toEqual({
      userId: USER,
      category: "activity",
    });
    expect(verifyUnsubscribe(USER.toUpperCase(), "activity", token, SECRET)).toEqual({
      userId: USER,
      category: "activity",
    });
    expect(verifyUnsubscribe(USER, "onboarding", token, SECRET)).toBeNull();
    expect(
      verifyUnsubscribe("22222222-2222-4222-8222-222222222222", "activity", token, SECRET),
    ).toBeNull();
    expect(verifyUnsubscribe(USER, "activity", token, "rotated-key")).toBeNull();
    expect(verifyUnsubscribe(USER, "activity", `${token}x`, SECRET)).toBeNull();
    expect(verifyUnsubscribe(USER, "everything", token, SECRET)).toBeNull();
    expect(verifyUnsubscribe("not-a-uuid", "activity", token, SECRET)).toBeNull();
    expect(verifyUnsubscribe(USER, "activity", token, undefined)).toBeNull();
    expect(verifyUnsubscribe(USER, "activity", undefined, SECRET)).toBeNull();
  });
});
