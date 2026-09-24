import { test as base, expect } from "@playwright/test";

/** Every test runs without internet: Google Fonts requests fail fast and the fallback fonts are used. */
export const test = base.extend<{ offlineFonts: void }>({
  offlineFonts: [
    async ({ context }, use) => {
      await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, (route) => route.abort());
      await use();
    },
    { auto: true },
  ],
});

export { expect };

/** Mock mode, with scripted runs 4x faster than the demo speed. */
export const MOCK_URL = "/?mock=1&mockSpeed=4";
