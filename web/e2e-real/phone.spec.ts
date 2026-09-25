import { expect, pendingApprovals, test } from "./hub";

test("IO checkout: the first force approved for the run, the answers from the stream", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /IO checkout/ }).click();
  const box = page.getByRole("textbox", { name: "Message to the agent" });
  await expect(box).toHaveValue("Run IO checkout for ");
  await box.fill("Run IO checkout for r201-ctl.");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("IO checkout");

  // Live and reversible (tier L): decided right in the stream.
  const stream = page.getByTestId("run-stream");
  const force = stream.getByRole("region", { name: "Approval: Force r201-ctl ao0 to 100 for 300 s" });
  await expect(force).toContainText("R201 Valve", { timeout: 30_000 });
  await expect(page.getByRole("tab", { name: "Changes, 1 waiting" })).toBeVisible();
  await force.getByRole("button", { name: "Approve for this run" }).click();
  await expect(force).toContainText("Approved for this run by dev");

  const valve = stream.getByRole("region", { name: "Question from the agent" }).filter({ hasText: "Is the valve fully open?" });
  await valve.getByRole("button", { name: "Yes" }).click();
  await expect(valve).toContainText("Answered Yes");

  // The heater force is covered by the run-wide approval: no second card.
  const heater = stream.getByRole("region", { name: "Question from the agent" }).filter({ hasText: "Does the heater get power?" });
  await expect(heater).toBeVisible({ timeout: 30_000 });
  await expect(stream.locator(".tool.ok").filter({ hasText: "forced r201-ctl do0 (binary-output:1) to 1" })).toBeVisible();
  await expect(stream.getByRole("region", { name: /^Approval: / })).toHaveCount(1);
  await heater.getByRole("button", { name: "Yes" }).click();

  await expect(stream).toContainText("IO checkout of r201-ctl done", { timeout: 30_000 });
  await expect(page.locator(".pbar .sub")).toHaveText("HQ · Done");
  await expect(stream.locator(".tool.ok").filter({ hasText: /released the force of r201-ctl (ao0|do0)/ })).toHaveCount(2);
  await expect(page.getByRole("tab", { name: "Changes, 0 waiting" })).toBeVisible();
});

test("applies a pending plan only with a hold", async ({ page }) => {
  // Set up: a commissioning run that waits for the apply of its plan.
  const created = await page.request.post("/api/runs", { data: { message: "Commission room 204" } });
  expect(created.status()).toBe(201);
  await expect.poll(async () => (await pendingApprovals(page.request)).map((a) => a.tool), { timeout: 30_000 }).toEqual(["apply"]);

  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Commission room 204");
  // Tier C is not decided in the stream on a phone.
  const card = page.getByTestId("run-stream").getByRole("region", { name: /^Approval: Apply plan/ });
  await expect(card.getByRole("button", { name: /Approve/ })).toHaveCount(0);
  await card.getByRole("button", { name: "Review in Changes" }).click();

  const sheet = page.getByRole("region", { name: /^Approval: Apply plan p\d+/ });
  await expect(sheet).toContainText("r204-ctl");
  await sheet.getByRole("button", { name: "Show full diff" }).click();
  await expect(sheet.locator(".code.diff")).toContainText("io.json");
  const hold = sheet.getByRole("button", { name: "Hold to apply" });

  // A short tap must not apply.
  await hold.tap();
  await expect(sheet.locator(".holdhint")).toContainText("A short tap does nothing");
  await page.waitForTimeout(500);
  expect((await pendingApprovals(page.request)).map((a) => a.tool)).toEqual(["apply"]);
  await expect(hold).toBeEnabled();

  const box = (await hold.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(1900);
  await page.mouse.up();
  await expect(sheet).toContainText("Approved by dev");

  // Then the run asks to run the acceptance tests (tier L): a tap is enough.
  const tests = page.getByRole("region", { name: "Approval: Run 4 tests on the live site" });
  await tests.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(page.getByText("No pending changes")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Revision 2 live")).toBeVisible();

  await page.getByRole("tab", { name: "Agent" }).click();
  await expect(page.getByTestId("run-stream")).toContainText("Room 204 is commissioned: 4 of 4 tests passed", { timeout: 60_000 });
  await page.getByRole("tab", { name: "Site" }).click();
  await expect(page.getByRole("button", { name: /^Room 204/ })).toContainText(/\d+\.\d°/);
});

test("identifies a device from the Field tab", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("tab", { name: "Field" }).click();
  await page.getByRole("textbox", { name: "Device id from the label" }).fill("2021");
  await page.getByRole("button", { name: "Open" }).click();
  await expect(page.getByRole("status")).toHaveText("Opened r202-ctl.");
  await expect(page.getByRole("region", { name: "Live values of r202-ctl" })).toContainText(/R202 Temp\s*\d+\.\d °C/);

  await page.getByRole("button", { name: /Identify/ }).click();
  await expect(page.getByRole("button", { name: /Identify/ })).toContainText("Blinking now");
  // The hub ran it as a tier L call the user approved themselves, and audited it.
  const audit = (await (await page.request.get("/api/audit?limit=5")).json()) as { entries: { tool: string }[] };
  expect(audit.entries.find((e) => e.tool === "device_identify")).toMatchObject({
    user: "dev",
    outcome: "ok",
    args: { device: "r202-ctl", seconds: 30 },
  });
});
