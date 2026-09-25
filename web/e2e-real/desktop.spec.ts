import { expect, pendingApprovals, test } from "./hub";

test("commissions room 204: live values, the plan's approval, the tool results and the tests", async ({ page }) => {
  await page.goto("/");
  const tree = page.getByRole("navigation", { name: "Site tree" });
  // The demo site is one building space ("HQ"); the tree shows it once, as the site.
  await expect(tree.getByRole("button", { name: /^Floor 2\s*,/ })).toBeVisible();
  await expect(tree.getByText("HQ", { exact: true })).toHaveCount(1);

  // Values arrive over /api/live; a changed one flashes.
  const supply = page.locator('tr[data-point="hq/ahu1-ctl/analog-value:3"] td.v');
  await expect(supply).toHaveText(/^\d+\.\d °C$/);
  await expect(page.locator("tbody td.v .flash").first()).toBeAttached({ timeout: 30_000 });

  const box = page.getByRole("textbox", { name: "Message to the agent" });
  await box.fill("Commission room 204");
  await box.press("Enter");
  const agent = page.getByRole("complementary", { name: "Agent" });
  const stream = agent.getByTestId("run-stream");
  await expect(stream.locator(".msg-user")).toHaveText("Commission room 204");

  const apply = stream.getByRole("region", { name: /^Approval: Apply plan p\d+/ });
  await expect(apply).toBeVisible({ timeout: 30_000 });
  await expect(agent.locator(".ahead")).toContainText("Waiting for approval");
  // Each message once, although the hub completes it after the step's tool calls.
  await expect(stream.locator(".msg-ai").filter({ hasText: "I'll start by finding the devices in room 204." })).toHaveCount(1);
  await expect(stream.getByRole("button", { name: /manifest_edit 2 JSON-patch ops/ })).toBeVisible();
  await expect(stream.locator(".tool").filter({ hasText: "plan p2: 9 changes on r204-ctl, gateway" })).toBeVisible();

  // Watch the board while it is configured: it has no points yet.
  await tree.getByRole("button", { name: /r204-ctl/ }).click();
  await expect(page.getByText("No points here yet.")).toBeVisible();

  // Tier C: Approve only inside the expanded card, next to the diff.
  await expect(apply.getByRole("button", { name: "Approve and apply" })).toHaveCount(0);
  await apply.getByRole("button", { name: "Review diff" }).click();
  await expect(apply.locator(".code.diff")).toContainText("r204-ctl: r204");
  await apply.getByRole("button", { name: "Approve and apply" }).click();
  await expect(apply).toContainText("Approved by dev");

  // Running the acceptance tests is a live (tier L) call: approved on its own card.
  const tests = stream.getByRole("region", { name: "Approval: Run 4 tests on the live site" });
  await tests.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(stream).toContainText("Room 204 is commissioned: 4 of 4 tests passed", { timeout: 60_000 });
  await expect(agent.locator(".ahead")).toContainText("Done");
  await expect(stream.locator(".tool.ok").filter({ hasText: /applied plan p2: 9 changes; revision 2 is live/ })).toBeVisible();
  await expect(stream.locator(".tool.ok").filter({ hasText: "4 of 4 tests passed" })).toBeVisible();
  expect(await pendingApprovals(page.request)).toEqual([]);
  // Its new points appear, and the hub watches them: their values go on changing after the run.
  await expect(page.locator('tr[data-point="hq/r204-ctl/analog-input:1"] td.v')).toHaveText(/^\d+\.\d °C$/);
  // (The valve and the AHU link: nothing else on the hub watches them.)
  const r204 = page.locator('tr[data-point="hq/r204-ctl/analog-output:1"] td.v, tr[data-point="hq/r204-ctl/analog-value:10"] td.v');
  const settled = (await r204.allInnerTexts()).join("|");
  await expect.poll(async () => (await r204.allInnerTexts()).join("|"), { timeout: 30_000 }).not.toBe(settled);

  await page.getByRole("tab", { name: /^Changes/ }).click();
  await expect(page.getByRole("heading", { name: "No pending changes" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Changes, 0 pending" })).toBeVisible();
  await expect(page.getByText("Revision 2 live")).toBeVisible();

  await page.getByRole("tab", { name: "Tests" }).click();
  await expect(page.locator('[data-col="live"]')).toHaveText(Array<RegExp>(4).fill(/^Passed/));
  // The hub runs the tests on the live site only, so there is no simulation column.
  await expect(page.getByRole("columnheader", { name: "Simulation" })).toHaveCount(0);

  await tree.getByRole("button", { name: /^Room 204\W/ }).click();
  await expect(page.locator(".crumb")).toHaveText("HQ / Floor 2 / Room 204");
  await tree.getByRole("button", { name: /r204-ctl/ }).click();
  await expect(page.locator(".crumb")).toHaveText("HQ / Floor 2 / Room 204 / r204-ctl");
  await page.getByRole("tab", { name: "Device" }).click();
  const apps = page.getByRole("tabpanel").locator(".side-card").filter({ hasText: "Apps" });
  await expect(apps.locator("tbody tr")).toHaveText([/thermostat\s*running/, /link\s*running/]);
});

test("a reload in the middle of a run replays it and the run goes on", async ({ page }) => {
  await page.goto("/");
  const box = page.getByRole("textbox", { name: "Message to the agent" });
  await box.fill("Run IO checkout for r202-ctl.");
  await box.press("Enter");
  const stream = page.getByTestId("run-stream");
  const force = stream.getByRole("region", { name: "Approval: Force r202-ctl ao0 to 100 for 300 s" });
  await expect(force).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(".ahead")).toContainText("Waiting for approval");
  const before = await stream.innerText();

  await page.reload();
  // The same run opens again (#run=...) and its events replay into the same view.
  await expect(force).toBeVisible();
  await expect(page.getByRole("button", { name: /show runs/ })).toContainText("Run IO checkout for r202-ctl");
  await expect(stream.locator(".msg-user")).toHaveText("Run IO checkout for r202-ctl.");
  await expect(stream.locator(".msg-ai").filter({ hasText: "First the valve output ao0" })).toHaveCount(1);
  await expect.poll(() => stream.innerText()).toBe(before);

  await force.getByRole("button", { name: "Approve for this run" }).click();
  await expect(force).toContainText("Approved for this run by dev");
  for (const question of ["Is the valve fully open?", "Does the heater get power?"]) {
    const card = stream.getByRole("region", { name: "Question from the agent" }).filter({ hasText: question });
    await card.getByRole("button", { name: "Yes" }).click();
    await expect(card).toContainText("Answered");
  }
  await expect(stream).toContainText("IO checkout of r202-ctl done", { timeout: 30_000 });
  await expect(page.locator(".ahead")).toContainText("Done");
  const done = await stream.innerText();

  // A replay from the start folds into exactly what the live stream built.
  await page.reload();
  await expect(stream).toContainText("IO checkout of r202-ctl done");
  await expect.poll(() => stream.innerText()).toBe(done);
});

test.describe("with a bearer token", () => {
  const token = "e2e-demo-token-5f1c";
  test.use({ hubOptions: { token } });

  test("asks for the sign-in link, then keeps the token out of the address bar", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("Sign-in needed", { exact: true })).toBeVisible();
    await expect(page.getByRole("alert").first()).toContainText("open the sign-in link");

    await page.goto(`/?token=${token}`);
    await expect(page.getByText("Gateway online")).toBeVisible();
    expect(page.url()).not.toContain("token");
    await expect(page.getByRole("img", { name: "Signed in as demo (admin)" })).toBeVisible();

    // The run's event stream carries the token too.
    const box = page.getByRole("textbox", { name: "Message to the agent" });
    await box.fill("What is the temperature in room 201?");
    await box.press("Enter");
    await expect(page.getByTestId("run-stream")).toContainText(/Room 201: .* degrees-celsius \(good\)/, { timeout: 30_000 });

    await page.reload();
    await expect(page.getByText("Gateway online")).toBeVisible();
    await expect(page.getByRole("img", { name: "Signed in as demo (admin)" })).toBeVisible();
  });
});

test.describe("without a configured model", () => {
  test.use({ hubOptions: { llm: "zai" } });

  test("shows why the agent cannot answer", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("No LLM", { exact: true })).toBeVisible();
    await expect(page.getByText("The hub has no LLM configured")).toBeVisible();
    const box = page.getByRole("textbox", { name: "Message to the agent" });
    await box.fill("hello");
    await box.press("Enter");
    const agent = page.getByRole("complementary", { name: "Agent" });
    await expect(agent.getByRole("alert").filter({ hasText: "Error" })).toContainText(
      "Error (llm): the model could not answer: Z.ai API key missing",
    );
    await expect(agent.locator(".ahead")).toContainText("Stopped by an error");
  });
});
