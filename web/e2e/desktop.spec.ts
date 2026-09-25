import { expect, MOCK_URL, test } from "./fixtures";

test("shows points with live values and applies the commissioning plan", async ({ page }) => {
  await page.goto(MOCK_URL);

  const temp = page.locator('tr[data-point="hq/r205-ctl/analog-input:1"]');
  await expect(temp).toContainText("R205 Temp");
  await expect(temp.locator("td.v")).toHaveText(/^\d+\.\d °C$/);
  const values = page.locator("tbody td.v");
  const first = (await values.allInnerTexts()).join("|");
  await expect.poll(async () => (await values.allInnerTexts()).join("|"), { timeout: 20_000 }).not.toBe(first);

  await page.getByRole("button", { name: /show runs/ }).click();
  await page.getByRole("button", { name: /Commission room 204/ }).click();
  const card = page.getByRole("region", { name: "Approval: Apply plan p17" });
  await expect(card).toBeVisible();
  // Tier C: approving needs the expanded card with the diff.
  await expect(card.getByRole("button", { name: "Approve and apply" })).toHaveCount(0);
  await card.getByRole("button", { name: "Review diff" }).click();
  await expect(card.locator(".code.diff")).toContainText("io.json");
  await card.getByRole("button", { name: "Approve and apply" }).click();
  await expect(card).toContainText("Approved by dev");

  const agent = page.getByRole("complementary", { name: "Agent" });
  await expect(agent.getByTestId("run-stream")).toContainText("Room 204 is commissioned", { timeout: 30_000 });
  await expect(agent.locator(".ahead")).toContainText("Done");

  await page.getByRole("tab", { name: /^Changes/ }).click();
  await expect(page.getByRole("heading", { name: "No pending changes" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Changes, 0 pending" })).toBeVisible();

  await page.getByRole("tab", { name: "Tests" }).click();
  await expect(page.locator('[data-col="live"]')).toHaveText(Array<RegExp>(3).fill(/Passed/));

  await page.getByRole("tab", { name: "Points" }).click();
  await expect(page.locator('tr[data-point="hq/r204-ctl/analog-value:20"]')).toContainText("R204 CO2");
});

test("search filters the point table and the tree scopes it", async ({ page }) => {
  await page.goto(MOCK_URL);
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await page.keyboard.press("Control+k");
  await expect(page.getByRole("searchbox", { name: "Search points" })).toBeFocused();
  await page.keyboard.type("zone_air_co2");
  await expect(page.locator("tbody tr")).toHaveCount(1);
  await expect(page.locator("tbody tr")).toContainText("R204 CO2 sensor");
  await page.keyboard.press("Escape");

  await page.getByRole("navigation", { name: "Site tree" }).getByRole("button", { name: /^Room 205\W+1 device$/ }).click();
  await expect(page.getByRole("heading", { level: 2 })).toHaveText("Room 205");
  await expect(page.locator("tbody tr")).toHaveCount(4);
  await page.getByRole("tab", { name: "Devices" }).click();
  await page.getByRole("button", { name: /r205-ctl/ }).last().click();
  await expect(page.getByRole("tab", { name: "Device" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel")).toContainText("thermostat");
});

test("a new run answers from live data", async ({ page }) => {
  await page.goto(MOCK_URL);
  await expect(page.getByRole("button", { name: /show runs/ })).toContainText("IO checkout");
  await page.getByRole("button", { name: "New run", exact: true }).click();
  const box = page.getByRole("textbox", { name: "Message to the agent" });
  await box.fill("Which rooms are above 24 °C?");
  await box.press("Enter");
  const stream = page.getByTestId("run-stream");
  await expect(stream.locator(".msg-user")).toHaveText("Which rooms are above 24 °C?");
  await expect(stream).toContainText("R205 Temp", { timeout: 20_000 });
  await expect(page.getByRole("button", { name: /show runs/ })).toContainText("Which rooms are above 24 °C?");
});

test.describe("PWA", () => {
  test.use({ serviceWorkers: "allow" });

  test("links the manifest and caches the shell but never the API", async ({ page }) => {
    await page.goto(MOCK_URL);
    await expect(page.locator('link[rel="manifest"]')).toHaveAttribute("href", "/manifest.webmanifest");
    const manifest = await (await page.request.get("/manifest.webmanifest")).json();
    expect(manifest).toMatchObject({ short_name: "uc-hub", display: "standalone" });
    const cached = await page.evaluate(async () => {
      await navigator.serviceWorker.ready;
      await fetch("/api/health");
      const cache = await caches.open("uc-hub-shell-v1");
      return (await cache.keys()).map((r) => new URL(r.url).pathname);
    });
    expect(cached).toEqual(expect.arrayContaining(["/", "/index.html", "/icon.svg"]));
    expect(cached.some((p) => p.startsWith("/api"))).toBe(false);
  });

  // Offline starts need every script and stylesheet of the page in the cache
  // after the first visit, although they load before the worker takes over.
  // (Playwright cannot cut the worker's own network, so an offline reload
  // would not prove it.)
  test("caches the page's scripts and styles on the first visit", async ({ page }) => {
    await page.goto(MOCK_URL);
    const { loaded, cached } = await page.evaluate(async () => {
      await navigator.serviceWorker.ready;
      const refs = document.querySelectorAll<HTMLScriptElement | HTMLLinkElement>(
        'script[src], link[rel="stylesheet"][href], link[rel="modulepreload"][href]',
      );
      const urls = [...refs].map((el) => new URL(el instanceof HTMLScriptElement ? el.src : el.href));
      const cache = await caches.open("uc-hub-shell-v1");
      return {
        loaded: urls.filter((u) => u.origin === location.origin).map((u) => u.pathname),
        cached: (await cache.keys()).map((r) => new URL(r.url).pathname),
      };
    });
    expect(loaded.length).toBeGreaterThanOrEqual(2);
    expect(cached).toEqual(expect.arrayContaining(loaded));
  });
});
