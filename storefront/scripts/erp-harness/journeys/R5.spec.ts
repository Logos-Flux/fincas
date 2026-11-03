// R5 "Returns and refunds" (docs/release-hardening-plan.md section 4, R5 row).
//
// a paid, shipped order (seed_release_test: core.release_test.
// seed_paid_shipped_order, SO-HRN-PAID-01) -> refund from the back office
// -> payment status (stock untouched: a refund returns money, not goods;
// there is no returns model to restock through) -> the books. Reached by
// clicking, asserted through /harness/state/, with the runner's page,
// axe, 360 px and server-log checks. A product gap is a knownFinding; a
// step that gap leaves nothing to work on is `blocked(...)` under it
// (lib/harness.ts).
import { blocked, journey, expect, type Ctx } from "../lib/harness";
import { clickNav, expectMoney, moneyValue, num, str } from "../lib/erp";

const COMPANY =
  process.env.HARNESS_COMPANY ?? "0c0ffee0-4e1e-4a5e-9a11-0000000d0001";
const ORDER_NUMBER = "SO-HRN-PAID-01";

async function openOrder(ctx: Ctx) {
  const { page, state } = ctx;
  await clickNav(page, "Sales Orders");
  await page.locator("input[name='q']").first().fill(ORDER_NUMBER);
  await page.locator("input[name='q']").first().press("Enter");
  const row = page.locator("tr", { hasText: ORDER_NUMBER }).first();
  await expect(row).toBeVisible();
  await row.getByRole("link", { name: ORDER_NUMBER }).click();
  await expect(page).toHaveURL(new RegExp(`/erp/orders/${state.orderId}/edit/`));
}

journey("R5", "Returns and refunds", (step) => {
  step(1, "the seeded order: shipped, invoiced and paid, posted to the books", async (ctx) => {
    const { page, state, db, dbOne, gl, login } = ctx;
    await login("staff");
    await page.goto("/erp/");
    const order = await dbOne("orders.Order", { order_number: ORDER_NUMBER });
    state.orderId = str(order.id);
    expect(str(order.status)).toBe("shipped");
    expect(str(order.payment_status)).toBe("paid");
    const invoice = await dbOne("orders.SalesInvoice", { order: state.orderId });
    state.invoiceId = str(invoice.id);
    state.invoiceNumber = str(invoice.invoice_number);
    expect(str(invoice.status)).toBe("paid");
    expect(num(invoice.balance_due)).toBe(0);
    const payment = await dbOne("payments.Payment", { order: state.orderId, payment_type: "payment" });
    state.paymentId = str(payment.id);
    state.paymentNumber = str(payment.payment_number);
    expect(str(payment.status)).toBe("captured");
    expect(num(payment.amount)).toBe(num(order.grand_total));
    state.total = str(order.grand_total);
    const lines = await db("orders.OrderLineItem", { order: state.orderId });
    expect(lines.length).toBe(1);
    state.sku = str(lines[0].sku);
    state.qty = num(lines[0].quantity);
    const stock = await db("inventory.WarehouseStock", { variant__sku: state.sku });
    state.stockBefore = stock.reduce((s, r) => s + num(r.quantity), 0);
    const books = await gl(COMPANY);
    expect(books.balanced).toBe(true);
    const keys = (books.entries as { posting_key: string }[]).map((e) => str(e.posting_key));
    expect(keys).toContain(`payment:${state.paymentNumber}`);
    expect(keys.some((k) => k.includes(str(state.invoiceNumber))), `an entry for ${state.invoiceNumber} in ${keys.join(", ")}`).toBe(true);
    // Reach the order by clicking; the list's total is money per docs/formatting.md.
    await clickNav(page, "Sales Orders");
    await page.locator("input[name='q']").first().fill(ORDER_NUMBER);
    await page.locator("input[name='q']").first().press("Enter");
    const row = page.locator("tr", { hasText: ORDER_NUMBER }).first();
    const cells = await row.locator("td").allInnerTexts();
    const money = cells.map((c) => c.trim()).filter((c) => /^\(?\$/.test(c));
    expect(money.length, `a money cell in ${cells.join(" | ")}`).toBeGreaterThan(0);
    expectMoney(money[0], "order total in the list");
    expect(moneyValue(money[0])).toBe(num(state.total));
    await row.getByRole("link", { name: ORDER_NUMBER }).click();
    await expect(page).toHaveURL(new RegExp(`/erp/orders/${state.orderId}/edit/`));
    await expect(page.locator("#id_payment_status")).toHaveValue("paid");
  });

  step(2, "refund the order from the back office", async (ctx) => {
    const { page, state, db } = ctx;
    // Found 2026-10-17 (fixed the same day): no back-office screen issued a
    // refund; refunds existed only as API-created Payment rows. "Refund" on
    // the order editor leads to the invoice page's refund form, which posts
    // payments.services.refund_payment (the parent payment, an amount up to
    // what is still refundable, a reason), behind the _once guard.
    await openOrder(ctx);
    const control = page.getByRole("link", { name: /^Refund$/ });
    await expect(control.first(), "a Refund action on the order").toBeVisible({ timeout: 3000 });
    await control.first().click();
    await expect(page).toHaveURL(new RegExp(`/erp/payments/${state.invoiceId}/#refund`));
    const form = page.locator("#refund-form");
    await expect(form).toBeVisible();
    await form.locator("select[name='payment']").selectOption(str(state.paymentId));
    await form.locator("input[name='amount']").fill(str(state.total));
    await form.locator("[name='reason']").fill("Customer returned both units.");
    page.once("dialog", (d) => d.accept());
    await form.getByRole("button", { name: /^Refund$/ }).click();
    await expect(page.locator(".alert, [role=alert]").first()).toContainText(/Refund PAY-/);
    const refunds = await db("payments.Payment", { order: str(state.orderId), payment_type: "refund" });
    expect(refunds.length).toBe(1);
    state.refundId = str(refunds[0].id);
    state.refundNumber = str(refunds[0].payment_number);
    expect(num(refunds[0].amount)).toBe(num(state.total));
    expect(str(refunds[0].parent_payment_id)).toBe(state.paymentId);
    expect(str(refunds[0].status)).toBe("refunded");
    expect(str(refunds[0].refund_reason)).toBe("Customer returned both units.");
    // The invoice page shows the refund in its payments and offers no second one.
    await expect(page.locator("#invoice-payments")).toContainText(str(state.refundNumber));
    await expect(page.locator("#invoice-refunded")).toBeVisible();
    await expect(page.locator("#refund-form")).toHaveCount(0);
  });

  step(3, "the payment status after the refund; stock untouched", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    if (!state.refundId) blocked("R5-no-refund-ui", "no refund was issued in step 2");
    await openOrder(ctx);
    const order = await dbOne("orders.Order", { pk: str(state.orderId) });
    expect(str(order.payment_status)).toBe("refunded");
    expect(num(order.refunded_amount)).toBe(num(state.total));
    expect(str(order.status), "the order's one refund state").toBe("partially_refunded");
    await expect(page.locator("#id_payment_status")).toHaveValue("refunded");
    const payment = await dbOne("payments.Payment", { pk: str(state.paymentId) });
    expect(str(payment.status)).toBe("refunded");
    // SalesInvoice has no refund state: the invoice keeps `paid` and the
    // refund is a row in its payments (refund_payment's docstring).
    const invoice = await dbOne("orders.SalesInvoice", { pk: str(state.invoiceId) });
    expect(str(invoice.status)).toBe("paid");
    expect((await db("payments.Payment", { invoice: str(state.invoiceId), payment_type: "refund" })).length).toBe(1);
    // A refund returns money, not goods: there is no returns model, so
    // nothing restocks (documented in docs/release-testing.md, R5).
    const stock = await db("inventory.WarehouseStock", { variant__sku: str(state.sku) });
    const onHand = stock.reduce((s, r) => s + num(r.quantity), 0);
    expect(onHand, "stock on hand unchanged by a refund").toBe(num(state.stockBefore));
  });

  step(4, "the books: trial balance in balance, the invoice, payment and refund posted", async (ctx) => {
    const { page, state, gl } = ctx;
    const books = await gl(COMPANY);
    expect(books.balanced).toBe(true);
    expect(books.unbalanced).toEqual([]);
    const keys = (books.entries as { posting_key: string }[]).map((e) => str(e.posting_key));
    expect(keys).toContain(`payment:${state.paymentNumber}`);
    if (state.refundId) {
      expect(keys.some((k) => k.includes(str(state.refundNumber))), "the refund's reversing entry").toBe(true);
    }
    // Through the UI: Accounting > Trial Balance agrees with the ledger.
    await clickNav(page, "Accounting");
    await clickNav(page, "Trial Balance");
    await expect(page).toHaveURL(/\/erp\/accounting\/trial-balance\//);
    const foot = page.locator("tfoot tr").first();
    await expect(foot).toContainText(/in balance/);
    const cells = (await foot.locator("td").allInnerTexts()).map((c) => c.trim());
    const [debit, credit] = cells.filter((c) => /^\(?\$/.test(c));
    expectMoney(debit, "ledger debit total");
    expectMoney(credit, "ledger credit total");
    expect(moneyValue(debit)).toBe(moneyValue(credit));
    expect(moneyValue(debit)).toBe(Number(books.debit));
  });
});
