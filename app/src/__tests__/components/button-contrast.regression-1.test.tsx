/**
 * Every onboarding call-to-action rendered dark text on saturated violet.
 *
 * The gradient was passed through className. That replaces `bg-primary` but
 * leaves `text-primary-foreground`, which in dark mode is near-black. The
 * button looked deliberate and was unreadable, and nothing failed, because a
 * contrast problem is not a runtime error.
 *
 * A `gradient` variant now pairs the background with `text-white` in one
 * place. These tests pin the pairing and stop the old pattern coming back a
 * component at a time.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..", "..");
const BUTTON = join(SRC, "components", "ui", "button.tsx");

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) return walk(full);
    return full.endsWith(".tsx") ? [full] : [];
  });
}

describe("gradient buttons stay readable", () => {
  it("ships a gradient variant that sets its own text colour", () => {
    const source = readFileSync(BUTTON, "utf-8");
    const variant = source.match(/gradient:\s*\n?\s*"([^"]+)"/);
    expect(
      variant,
      "button.tsx should define a gradient variant",
    ).not.toBeNull();
    expect(variant![1]).toContain("bg-gradient-to-r");
    // The whole point. A gradient background without a foreground inherits
    // text-primary-foreground, which is near-black in dark mode.
    expect(variant![1]).toContain("text-white");
  });

  it("no component paints a violet gradient without white text", () => {
    const offenders: string[] = [];
    for (const file of walk(join(SRC, "components")).concat(
      walk(join(SRC, "app")),
    )) {
      if (file.endsWith(join("ui", "button.tsx"))) continue;
      const source = readFileSync(file, "utf-8");
      // Array.from rather than iterating the iterator directly: the project's
      // TS target does not allow the latter without --downlevelIteration.
      const matches = Array.from(
        source.matchAll(/className="([^"]*bg-gradient-to-[^"]*)"/g),
      );
      for (const match of matches) {
        if (!match[1].includes("text-white")) {
          offenders.push(`${file.replace(SRC, "")}: ${match[1]}`);
        }
      }
    }
    expect(
      offenders,
      'use variant="gradient" instead of passing a gradient through className',
    ).toEqual([]);
  });
});
