// R12, the adopter's first invoice: docs/getting-started.md step 7, clicked
// through as the page describes it, on the project the page just built.
// Writes what the page showed to R12_OUT; r12_fresh_install.sh then checks it
// against the database through the project's own manage.py.
import { expect, test } from "@playwright/test";
import { writeFileSync } from "node:fs";

const USER = process.env.R12_USER ?? "";
const PASS = process.env.R12_PASS ?? "";
const OUT = process.env.R12_OUT ?? "";
// The page's step 7 names these; keep them in step with it.
const CUSTOMER = "Acme Industrial";
const SKU = "ACT-500W";
const QTY = "2";

test("first invoice, as docs/getting-started.md step 7 describes it", async ({ page }) => {
  expect(USER && PASS && OUT, "R12_USER, R12_PASS and R12_OUT are set").toBeTruthy();
  const problems: string[] = [];
  page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));
  page.on("response", (r) => {
    if (r.status() >= 500) problems.push(`${r.status()} ${r.url()}`);
  });

  await test.step("sign in at /erp/", async () => {
    await page.goto("/erp/");
    await expect(page).toHaveURL(/\/erp\/login\//);
    await page.locator("input[name=username]").fill(USER);
    await page.locator("input[name=password]").fill(PASS);
    await page.locator("button[type=submit]").first().click();
    await expect(page).toHaveURL(/\/erp\/$/);
  });

  await test.step("Sales Orders, New Sales Order", async () => {
    await page
      .locator("a.nav-link .nav-link-title", { hasText: /^Sales Orders$/ })
      .first()
      .click();
    await expect(page).toHaveURL(/\/erp\/orders\/$/);
    await page.getByRole("link", { name: /New Sales Order/ }).first().click();
    await expect(page).toHaveURL(/\/erp\/orders\/new\/$/);
  });

  await test.step(`customer: type "Acme", pick ${CUSTOMER}`, async () => {
    const search = page.getByPlaceholder("Search customers...");
    await search.fill("Acme");
    await page
      .locator(".ajax-search-widget a.list-group-item", { hasText: CUSTOMER })
      .first()
      .click();
  });

  await test.step(`line: ${SKU} x ${QTY}`, async () => {
    const row = page.locator("tr.line-item-row").first();
    await row.getByPlaceholder("Search SKU or name...").fill(SKU);
    await row.locator(".variant-search-results a", { hasText: SKU }).first().click();
    await expect(row.locator("[name$='-item_name']")).not.toHaveValue("");
    const qty = row.locator("input[name$='-quantity']");
    await qty.fill(QTY);
    await qty.dispatchEvent("input");
  });

  let orderId = "";
  await test.step("Save & Continue: the order is numbered and stays open", async () => {
    await page.getByRole("button", { name: "Save & Continue" }).click();
    await expect(page).toHaveURL(/\/erp\/orders\/[0-9a-f-]{36}\/edit\/$/);
    orderId = page.url().match(/orders\/([0-9a-f-]{36})\//)![1];
    await expect(page.locator("body")).toContainText(/SO-/);
  });

  await test.step("Create Invoice: the invoice page", async () => {
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: /Create Invoice/ }).click();
    await expect(page).toHaveURL(/\/erp\/payments\/[0-9a-f-]{36}\/$/);
    await expect(page.getByRole("link", { name: /Print preview/ })).toBeVisible();
  });

  const invoiceId = page.url().match(/payments\/([0-9a-f-]{36})\//)![1];
  const text = await page.locator("body").innerText();
  const invoiceNumber = text.match(/\bINV-[A-Z0-9-]+/)?.[0] ?? "";
  expect(invoiceNumber, "the invoice page shows its number").not.toBe("");
  expect(problems, "no 5xx and no page errors on the way").toEqual([]);
  writeFileSync(
    OUT,
    JSON.stringify({
      order_id: orderId,
      invoice_id: invoiceId,
      invoice_number: invoiceNumber,
    }),
  );
});
