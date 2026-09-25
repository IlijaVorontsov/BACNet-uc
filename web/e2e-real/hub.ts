/**
 * Fixtures for the tests against the real hub: every test gets a fresh
 * `uc-hub demo` (simulated devices on free ports, the hub on a free HTTP
 * port, a new database) that serves the built app from web/dist. So each
 * scenario starts from the same demo site, whatever ran before it.
 */

import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { fileURLToPath } from "node:url";
import { test as base, expect, type APIRequestContext } from "@playwright/test";

const HUB_BIN = process.env.UC_HUB_BIN ?? fileURLToPath(new URL("../../hub/.venv/bin/uc-hub", import.meta.url));
const START_TIMEOUT_MS = 45_000;
const STOP_TIMEOUT_MS = 15_000;

export interface HubOptions {
  /** Require this bearer token (`--token-env`) instead of dev mode. */
  token?: string;
  /** "zai" without an API key: the hub runs, but its model is not configured. */
  llm?: "scripted" | "zai";
}

export interface DemoHub {
  url: string;
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => (address && typeof address === "object" ? resolve(address.port) : reject(new Error("no free port"))));
    });
  });
}

async function startDemo(options: HubOptions): Promise<{ hub: DemoHub; log: () => string; stop: () => Promise<void> }> {
  const port = await freePort();
  const args = ["demo", "--port", String(port), "--free-ports", "--llm", options.llm ?? "scripted"];
  // A test never reaches a real model, whatever the developer's shell holds.
  const env: NodeJS.ProcessEnv = { ...process.env, ZAI_API_KEY: "" };
  if (options.token) {
    env.UC_E2E_TOKEN = options.token;
    args.push("--token-env", "UC_E2E_TOKEN");
  }
  const child = spawn(HUB_BIN, args, { env, stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  const keep = (chunk: Buffer): void => {
    output = (output + chunk.toString()).slice(-50_000);
  };
  child.stdout.on("data", keep);
  child.stderr.on("data", keep);
  const exited = new Promise<void>((resolve) => child.once("exit", () => resolve()));
  let running = true;
  void exited.then(() => (running = false));
  child.once("error", (err) => keep(Buffer.from(`cannot start ${HUB_BIN}: ${err.message}\n`)));

  const url = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + START_TIMEOUT_MS;
  for (;;) {
    if (!running) throw new Error(`uc-hub demo exited before it served ${url}:\n${output}`);
    const ok = await fetch(`${url}/api/health`).then((r) => r.ok, () => false);
    if (ok) break;
    if (Date.now() > deadline) {
      child.kill("SIGKILL");
      throw new Error(`uc-hub demo did not serve ${url} within ${START_TIMEOUT_MS} ms:\n${output}`);
    }
    await new Promise((r) => setTimeout(r, 100));
  }

  const stop = async (): Promise<void> => {
    if (!running) return;
    // SIGTERM lets the demo stop the hub (releasing leases), mosquitto and the devices.
    child.kill("SIGTERM");
    const late = new Promise<boolean>((resolve) => setTimeout(() => resolve(true), STOP_TIMEOUT_MS));
    if (await Promise.race([exited.then(() => false), late])) {
      child.kill("SIGKILL");
      await exited;
    }
  };
  return { hub: { url }, log: () => output, stop };
}

export const test = base.extend<{ hubOptions: HubOptions; hub: DemoHub; offlineFonts: void }>({
  hubOptions: [{}, { option: true }],

  hub: async ({ hubOptions }, use, testInfo) => {
    const demo = await startDemo(hubOptions);
    try {
      await use(demo.hub);
    } finally {
      await demo.stop();
      if (testInfo.status !== testInfo.expectedStatus) {
        await testInfo.attach("uc-hub.log", { body: demo.log(), contentType: "text/plain" });
      }
    }
  },

  baseURL: async ({ hub }, use) => {
    await use(hub.url);
  },

  // No internet in the tests: Google Fonts requests fail fast and the fallback fonts are used.
  offlineFonts: [
    async ({ context }, use) => {
      await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, (route) => route.abort());
      await use();
    },
    { auto: true },
  ],
});

export { expect };

interface ApprovalRow {
  id: string;
  tool: string;
  title: string;
  state: string;
}

/** The hub's pending approvals, straight from the API. */
export async function pendingApprovals(request: APIRequestContext): Promise<ApprovalRow[]> {
  const res = await request.get("/api/approvals?state=pending");
  expect(res.ok()).toBe(true);
  return ((await res.json()) as { approvals: ApprovalRow[] }).approvals;
}
