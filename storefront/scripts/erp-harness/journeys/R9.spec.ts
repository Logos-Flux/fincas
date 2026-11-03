// R9 "Books" (docs/release-hardening-plan.md section 4, R9 row).
//
// every posting path balances (trial balance zero after the R1/R5
// documents) -> closing a fiscal period blocks a post into it, through the
// UI: an invoice emailed (draft -> sent posts it) while its period is
// closed must show the error and save nothing; a payment the same -> the
// period reopened, the same invoice posts. Reached by clicking, asserted through /harness/state/.
// Order matters: this journey closes the current period and reopens it at
// the end, so it runs last (R9 sorts after the others).
import { journey, expect, type Ctx } from "../lib/harness";
import {
  addLine,
  alerts,
  clickNav,
  DATE_ISO_RE,
  expectMoney,
  moneyValue,
  pickCustomer,
  str,
} from "../lib/erp";

const COMPANY =
  process.env.HARNESS_COMPANY ?? "0c0ffee0-4e1e-4a5e-9a11-0000000d0001";

async function openPeriods(ctx: Ctx) {
  const { page } = ctx;
  await clickNav(page, "Accounting");
  await expect(page).toHaveURL(/\/erp\/accounting\/$/);
  await page.getByRole("link", { name: /^Periods$/ }).click();
  await expect(page).toHaveURL(/\/erp\/accounting\/periods\//);
}

async function openOrderEditor(ctx: Ctx) {
  const { page, state } = ctx;
  await clickNav(page, "Sales Orders");
  await page.locator("input[name='q']").first().fill(str(state.orderNumber));
  await page.locator("input[name='q']").first().press("Enter");
  await page.getByRole("link", { name: str(state.orderNumber) }).first().click();
  await expect(page).toHaveURL(new RegExp(`/erp/orders/${state.orderId}/edit/`));
}

journey("R9", "Books", (step) => {
  step(1, "trial balance through the UI: in balance, totals are the ledger's", async (ctx) => {
    const { page, gl, login } = ctx;
    await login("bookkeeper");
    await page.goto("/erp/");
    await clickNav(page, "Accounting");
    await clickNav(page, "Trial Balance");
    await expect(page).toHaveURL(/\/erp\/accounting\/trial-balance\//);
    const books = await gl(COMPANY);
    expect(books.balanced).toBe(true);
    expect(Number(books.entry_count), "the R1/R5 documents posted something").toBeGreaterThan(0);
    const foot = page.locator("tfoot tr").first();
    await expect(foot).toContainText(/in balance/);
    const cells = (await foot.locator("td").allInnerTexts()).map((c) => c.trim());
    const [debit, credit] = cells.filter((c) => /^\(?\$/.test(c));
    expectMoney(debit, "ledger debit total");
    expectMoney(credit, "ledger credit total");
    expect(moneyValue(debit)).toBe(Number(books.debit));
    expect(moneyValue(credit)).toBe(Number(books.credit));
    expect(moneyValue(debit)).toBe(moneyValue(credit));
  });

  step(2, "every posting path balances: no entry out of balance, every source paired", async (ctx) => {
    const { page, gl } = ctx;
    const books = await gl(COMPANY);
    expect(books.unbalanced, "journal entries whose legs do not balance").toEqual([]);
    for (const row of books.by_source as { entry__entry_source: string; debit: string; credit: string }[]) {
      expect(Number(row.debit), `source ${row.entry__entry_source}`).toBe(Number(row.credit));
    }
    // Journal entries screen: dates in Y-m-d (docs/formatting.md 2.2).
    await clickNav(page, "Journal Entries");
    await expect(page).toHaveURL(/\/erp\/accounting\/entries\//);
    const firstRow = page.locator("tbody tr").first();
    await expect(firstRow).toBeVisible();
    const cells = (await firstRow.locator("td").allInnerTexts()).map((c) => c.trim());
    expect(cells.some((c) => DATE_ISO_RE.test(c)), `an ISO date cell in ${cells.join(" | ")}`).toBe(true);
  });

  step(3, "an order with a draft invoice, before the close", async (ctx) => {
    const { page, state, db, login } = ctx;
    await login("staff");
    await page.goto("/erp/");
    await clickNav(page, "Sales Orders");
    await page.getByRole("link", { name: /^New/ }).first().click();
    await expect(page).toHaveURL(/\/erp\/orders\/new\//);
    await pickCustomer(page, /Acme/);
    await addLine(page, 0, "BRK-90-HD", "5");
    await page.locator("button[name='_action'][value='save_continue']").click();
    await expect(page).toHaveURL(/\/erp\/orders\/[0-9a-f-]+\/edit\//);
    state.orderId = page.url().match(/\/orders\/([0-9a-f-]+)\/edit\//)?.[1] ?? "";
    const order = (await db("orders.Order", { pk: str(state.orderId) }))[0];
    state.orderNumber = str(order.order_number);
    // Found 2026-10-17 (R1.14, fixed on hardening/r1-fixes): "Create
    // Invoice" lands on the invoice's back-office page.
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: /Create Invoice/ }).click();
    await expect(page).toHaveURL(/\/erp\/payments\/[0-9a-f-]+\/$/);
    const invoices = await db("orders.SalesInvoice", { order: str(state.orderId) });
    expect(invoices.length).toBe(1);
    state.invoiceId = str(invoices[0].id);
    state.invoiceNumber = str(invoices[0].invoice_number);
    expect(str(invoices[0].status)).toBe("draft");
    await expect(page.locator("a.nav-link, a[href*='/erp/orders/']").first()).toBeVisible({ timeout: 3000 });
  });

  step(4, "close the open fiscal period", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    await page.goto(`/erp/orders/${state.orderId}/edit/`); // leave the preview (step 3)
    await openPeriods(ctx);
    const open = await db("accounting.FiscalPeriod", { company: COMPANY, status: "open", _order: "start_date" });
    expect(open.length).toBe(1);
    state.periodId = str(open[0].id);
    state.periodName = str(open[0].name);
    const row = page.locator("tr", { hasText: str(state.periodName) });
    await expect(row).toContainText("Open");
    // Close period asks first (F-091, #159): a confirm() that names the period.
    // Playwright dismisses dialogs it is not told about, which left the period
    // open (the nightly's R9.04 failure from 2026-10-20 to 2026-10-26).
    let confirmText = "";
    page.once("dialog", (d) => {
      confirmText = d.message();
      void d.accept();
    });
    await row.getByRole("button", { name: /Close period/ }).click();
    await expect(page).toHaveURL(/\/erp\/accounting\/periods\//);
    expect(confirmText, "the Close period button asks first").toContain(str(state.periodName));
    const period = await dbOne("accounting.FiscalPeriod", { pk: str(state.periodId) });
    expect(str(period.status)).toBe("closed");
    expect(str(period.closed_at)).not.toBe("");
    await expect(page.locator("tr", { hasText: str(state.periodName) })).toContainText("Closed");
  });

  step(5, "emailing the invoice into the closed period is refused; nothing saved", async (ctx) => {
    const { page, state, db, dbOne, gl } = ctx;
    const emailsBefore = (await db("communications.EmailLog", { document_type: "invoice", document_id: str(state.invoiceId) })).length;
    const entriesBefore = Number((await gl(COMPANY)).entry_count);
    await openOrderEditor(ctx);
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: /Email Invoice/ }).click();
    await expect(page).toHaveURL(/\/erp\//);
    const shown = (await alerts(page)).join(" | ");
    const invoice = await dbOne("orders.SalesInvoice", { pk: str(state.invoiceId) });
    const emailsAfter = (await db("communications.EmailLog", { document_type: "invoice", document_id: str(state.invoiceId) })).length;
    const entriesAfter = Number((await gl(COMPANY)).entry_count);
    expect(str(invoice.status), `invoice status after a refused post (page said: ${shown})`).toBe("draft");
    expect(str(invoice.sent_at)).toBe("");
    expect(entriesAfter, "no journal entry into the closed period").toBe(entriesBefore);
    expect(emailsAfter, "no email sent for an invoice that could not post").toBe(emailsBefore);
    expect(shown, "the refusal is shown to the user").toMatch(/period|closed/i);
    expect(shown).toContain(str(state.periodName));
  });

  step(6, "a payment into the closed period is refused", async (ctx) => {
    const { page, state } = ctx;
    // Found 2026-10-17 (R1.16, fixed on hardening/r1-fixes): "Record
    // payment" on the order editor leads to the invoice page's form; a
    // payment dated in the closed period is refused with a message and
    // nothing is stored (payments.services.record_payment checks the
    // period before the row exists).
    const { db, dbOne } = ctx;
    const paymentsBefore = (await db("payments.Payment", { order: str(state.orderId) })).length;
    await page.goto(`/erp/orders/${state.orderId}/edit/`);
    const control = page.getByRole("link", { name: /record payment/i });
    await expect(control.first()).toBeVisible({ timeout: 3000 });
    await control.first().click();
    await expect(page).toHaveURL(new RegExp(`/erp/payments/${state.invoiceId}/#record-payment`));
    const form = page.locator("#record-payment-form");
    await form.locator("input[name='amount']").fill("1.00");
    await form.getByRole("button", { name: /record payment/i }).click();
    await expect(page.locator(".alert, [role=alert]").first()).toContainText(/period|closed/i);
    const shown = (await alerts(page)).join(" | ");
    expect(shown).toContain(str(state.periodName));
    expect((await db("payments.Payment", { order: str(state.orderId) })).length).toBe(paymentsBefore);
    const invoice = await dbOne("orders.SalesInvoice", { pk: str(state.invoiceId) });
    expect(str(invoice.status)).toBe("draft");
  });

  step(7, "reopen the period", async (ctx) => {
    const { page, state, dbOne } = ctx;
    await page.goto(`/erp/orders/${state.orderId}/edit/`);
    await openPeriods(ctx);
    const row = page.locator("tr", { hasText: str(state.periodName) });
    await row.getByRole("button", { name: /^Reopen$/ }).click();
    await expect(page).toHaveURL(/\/erp\/accounting\/periods\//);
    const period = await dbOne("accounting.FiscalPeriod", { pk: str(state.periodId) });
    expect(str(period.status)).toBe("open");
    await expect(page.locator("tr", { hasText: str(state.periodName) })).toContainText("Open");
  });

  step(8, "with the period open again, the same invoice posts and the books balance", async (ctx) => {
    const { page, state, db, dbOne, gl } = ctx;
    const invoiceBefore = await dbOne("orders.SalesInvoice", { pk: str(state.invoiceId) });
    if (str(invoiceBefore.status) === "draft") {
      await openOrderEditor(ctx);
      page.once("dialog", (d) => d.accept());
      await page.getByRole("button", { name: /Email Invoice/ }).click();
      await expect(page).toHaveURL(/\/erp\//);
    }
    const invoice = await dbOne("orders.SalesInvoice", { pk: str(state.invoiceId) });
    expect(str(invoice.status)).toBe("sent");
    const books = await gl(COMPANY);
    expect(books.balanced).toBe(true);
    expect(books.unbalanced).toEqual([]);
    const keys = (books.entries as { posting_key: string; source_reference: string }[]);
    expect(keys.some((e) => str(e.source_reference).includes(str(state.invoiceNumber)) || str(e.posting_key).includes(str(state.invoiceNumber))),
      `an entry for ${state.invoiceNumber}`).toBe(true);
    void db;
  });
});
