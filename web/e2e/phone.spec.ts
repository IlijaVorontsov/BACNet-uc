import { expect, MOCK_URL, test } from "./fixtures";

test("answers the IO checkout and applies the plan only with a hold", async ({ page }) => {
  await page.goto(MOCK_URL);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("IO checkout · r204-ctl");

  for (const question of ["Did the relay click", "Open the window", "Does your reference read"]) {
    const card = page.getByRole("region", { name: "Question from the agent" }).filter({ hasText: question });
    await card.getByRole("button", { name: "Yes" }).click();
    await expect(card).toContainText("Answered");
  }
  const stream = page.getByTestId("run-stream");
  await expect(stream).toContainText("Checkout finished: 4 passed", { timeout: 20_000 });

  await page.getByRole("tab", { name: /^Changes/ }).click();
  const sheet = page.getByRole("region", { name: "Approval: Apply plan p17" });
  const hold = sheet.getByRole("button", { name: "Hold to apply" });
  await expect(hold).toBeVisible();

  // A short tap must not apply.
  await hold.tap();
  await expect(sheet.locator(".holdhint")).toContainText("A short tap does nothing");
  await page.waitForTimeout(400);
  const pending = await page.evaluate(async () => (await (await fetch("/api/approvals?state=pending")).json()) as { approvals: unknown[] });
  expect(pending.approvals).toHaveLength(1);
  await expect(hold).toBeEnabled();

  // Releasing early cancels too.
  const box = (await hold.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(700);
  await page.mouse.up();
  await expect(sheet.getByRole("button", { name: "Hold to apply" })).toBeEnabled();

  await page.mouse.down();
  await page.waitForTimeout(1900);
  await page.mouse.up();
  await expect(sheet).toContainText("Approved by dev");
  await expect(page.getByText("No pending changes")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("tab", { name: "Changes, 0 waiting" })).toBeVisible();

  await page.getByRole("tab", { name: "Agent" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Commission room 204");
  await expect(page.getByTestId("run-stream")).toContainText("Room 204 is commissioned", { timeout: 30_000 });
});

test("field tab identifies a device picked by its label id", async ({ page }) => {
  await page.goto(MOCK_URL);
  await page.getByRole("tab", { name: "Field" }).click();
  // Headless Chromium on Linux has no BarcodeDetector, so the manual entry is shown.
  await page.getByRole("textbox", { name: "Device id from the label" }).fill("2041");
  await page.getByRole("button", { name: "Open" }).click();
  await expect(page.getByRole("status")).toHaveText("Opened r204-ctl.");
  await expect(page.getByTestId("field-device")).toContainText("r204-ctl");
  await page.getByRole("button", { name: /Identify/ }).click();
  await expect(page.getByRole("button", { name: /Identify/ })).toContainText("Blinking now");
  await expect(page.locator(".led.blink")).toHaveCount(1);

  await page.getByRole("combobox", { name: "Device" }).selectOption("r203-ctl");
  await expect(page.getByRole("button", { name: /Identify/ })).toBeDisabled();
});

test("site tab shows rooms with live temperatures", async ({ page }) => {
  await page.goto(MOCK_URL);
  await page.getByRole("tab", { name: "Site" }).click();
  const room = page.getByRole("button", { name: /Room 205/ });
  await expect(room).toContainText(/2\d\.\d°/);
  await expect(room).toContainText("Above setpoint");
  await room.click();
  await expect(page.locator(".roomdetail")).toContainText("R205 Reheat valve");
});

test("starts on a site network where the font server never answers", async ({ page }) => {
  // Registered after the fixture's abort route, so it wins: font requests hang.
  await page.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, () => undefined);
  await page.goto(MOCK_URL, { waitUntil: "commit" });
  await expect(page.getByRole("tablist", { name: "Sections" })).toBeVisible();
});
