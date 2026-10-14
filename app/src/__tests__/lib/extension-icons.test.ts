import { resolve } from "node:path";
import sharp from "sharp";
import { describe, expect, it } from "vitest";

const BRAND_BLUE = [0x23, 0x74, 0xf2, 0xff] as const;

describe("Chrome extension icons", () => {
  it.each([16, 48, 128])(
    "renders the EngageMate mark at %d pixels instead of a placeholder",
    async (size) => {
      const path = resolve(
        process.cwd(),
        `../extension/icons/icon${size}.png`,
      );
      const image = sharp(path);
      const metadata = await image.metadata();
      const { data, info } = await image.ensureAlpha().raw().toBuffer({
        resolveWithObject: true,
      });

      expect(metadata.format).toBe("png");
      expect(metadata.width).toBe(size);
      expect(metadata.height).toBe(size);
      expect(info.channels).toBe(4);

      let brandPixels = 0;
      let transparentPixels = 0;
      for (let offset = 0; offset < data.length; offset += info.channels) {
        if (
          BRAND_BLUE.every((channel, index) => data[offset + index] === channel)
        ) {
          brandPixels += 1;
        }
        if (data[offset + 3] === 0) {
          transparentPixels += 1;
        }
      }

      expect(brandPixels).toBeGreaterThan(size);
      expect(transparentPixels).toBeGreaterThan(size);
    },
  );
});
