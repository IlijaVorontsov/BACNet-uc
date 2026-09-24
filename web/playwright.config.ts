import { defineConfig } from "@playwright/test";

// One random high port per run. Playwright evaluates this file again in each
// worker; workers inherit the main process's environment, so they agree on it.
process.env.UC_WEB_E2E_PORT ??= String(20000 + Math.floor(Math.random() * 30000));
const port = Number(process.env.UC_WEB_E2E_PORT);
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: true,
  workers: 2,
  forbidOnly: !!process.env.CI,
  reporter: [["list"]],
  use: {
    baseURL,
    // The app registers a service worker in production builds; tests that
    // check it opt in, the others keep the network path simple.
    serviceWorkers: "block",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop",
      testMatch: /desktop\.spec\.ts$/,
      use: { browserName: "chromium", viewport: { width: 1366, height: 820 } },
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
  webServer: {
    // Build first so the preview never serves a stale dist/.
    command: `pnpm exec vite build --logLevel warn && pnpm exec vite preview --host 127.0.0.1 --port ${port} --strictPort`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
