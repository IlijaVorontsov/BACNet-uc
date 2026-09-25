import { defineConfig } from "@playwright/test";

// The app against the real hub: each test starts its own `uc-hub demo`
// (e2e-real/hub.ts, ../hub/.venv/bin/uc-hub or $UC_HUB_BIN), which serves
// dist/ as built by the global setup.
export default defineConfig({
  testDir: "e2e-real",
  globalSetup: "./e2e-real/build.ts",
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: true,
  workers: 2,
  forbidOnly: !!process.env.CI,
  reporter: [["list"]],
  use: {
    // Tests that check the worker live in the mock suite; here the network path stays simple.
    serviceWorkers: "block",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop",
      testMatch: /desktop\.spec\.ts$/,
      use: { browserName: "chromium", viewport: { width: 1366, height: 900 } },
    },
    {
      name: "phone",
      testMatch: /phone\.spec\.ts$/,
      use: {
        browserName: "chromium",
        viewport: { width: 390, height: 844 },
        deviceScaleFactor: 3,
        isMobile: true,
        hasTouch: true,
      },
    },
  ],
});
