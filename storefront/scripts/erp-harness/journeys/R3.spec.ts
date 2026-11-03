// R3 "Several quotes, one deal" (docs/release-hardening-plan.md section 4, R3 row).
//
// two quotes on one engagement -> one converted to an order -> the other
// stays listed on the engagement with its own status; nothing is
// auto-cancelled. Reached by clicking, asserted through /harness/state/,
// with the runner's page, axe, 360 px and server-log checks. A product gap
// is a knownFinding; a step that an earlier gap leaves nothing to work on
// is `blocked(...)` under that finding (lib/harness.ts).
import { blocked, journey, expect, type Ctx } from "../lib/harness";
import { addLine, clickNav, expectMoney, pickCustomer, str } from "../lib/erp";

async function openEngagementFromQuotes(ctx: Ctx, quoteNumber: string) {
  const { page, state } = ctx;
  await clickNav(page, "Quotes");
  const row = page.locator("tr", { hasText: quoteNumber }).first();
  await row.locator("a[href*='/erp/crm/engagements/']").first().click();
  await expect(page).toHaveURL(new RegExp(`/erp/crm/engagements/${state.engagementId}/`));
}

const STATUS_LABEL: Record<string, string> = {
  draft: "Draft", sent: "Sent", accepted: "Accepted", lost: "Lost", expired: "Expired",
  superseded: "Superseded", cancelled: "Canceled",
};

journey("R3", "Several quotes, one deal", (step) => {
  step(1, "quote A for a customer opens the deal", async (ctx) => {
    const { page, state, dbOne, login } = ctx;
    await login("staff");
    await page.goto("/erp/");
    await clickNav(page, "Quotes");
    await page.getByRole("link", { name: /^New/ }).first().click();
    await expect(page).toHaveURL(/\/erp\/orders\/quotes\/new\//);
    await pickCustomer(page, /Whitfield/);
    await addLine(page, 0, "BRK-90-HD", "20");
    await page.locator("button[name='_action'][value='save_continue']").click();
    await expect(page).toHaveURL(/\/erp\/orders\/quotes\/[0-9a-f-]+\/edit\//);
    const pk = page.url().match(/\/quotes\/([0-9a-f-]+)\/edit\//)?.[1] ?? "";
    const quote = await dbOne("orders.Quote", { pk });
    state.quoteAId = str(quote.id);
    state.quoteANumber = str(quote.quote_number);
    state.quoteATotal = str(quote.grand_total);
    state.customerId = str(quote.customer_id);
    state.engagementId = str(quote.engagement_id);
    expect(state.engagementId).not.toBe("");
    const eng = await dbOne("crm.Engagement", { pk: state.engagementId });
    expect(str(eng.status)).toBe("open");
  });

  step(2, "quote B started from the deal lands on the same engagement", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    // Found 2026-10-17 (R1.06's root cause) and fixed the same day on
    // hardening/r1-fixes: "New quote" on the engagement page opens the
    // editor with the deal preselected, so the second quote joins it.
    await openEngagementFromQuotes(ctx, str(state.quoteANumber));
    const control = page.getByRole("link", { name: /^New quote/i });
    await expect(control.first(), "a 'New quote' action on the engagement").toBeVisible({ timeout: 3000 });
    await control.first().click();
    await expect(page).toHaveURL(/\/erp\/orders\/quotes\/new\//);
    await addLine(page, 0, "SNR-LASER-30", "2");
    await page.locator("button[name='_action'][value='save_continue']").click();
    await expect(page).toHaveURL(/\/erp\/orders\/quotes\/[0-9a-f-]+\/edit\//);
    const pk = page.url().match(/\/quotes\/([0-9a-f-]+)\/edit\//)?.[1] ?? "";
    const quote = await dbOne("orders.Quote", { pk });
    expect(str(quote.engagement_id), "quote B on the deal's engagement").toBe(state.engagementId);
    state.quoteBId = str(quote.id);
    state.quoteBNumber = str(quote.quote_number);
    expect((await db("orders.Quote", { engagement: str(state.engagementId) })).length).toBe(2);
  });

  step(3, "convert quote A to a sales order", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    await clickNav(page, "Quotes");
    await page.getByRole("link", { name: str(state.quoteANumber), exact: true }).first().click();
    await expect(page).toHaveURL(new RegExp(`/erp/orders/quotes/${state.quoteAId}/edit/`));
    const already = await db("orders.Order", { source_quotes__id: str(state.quoteAId) });
    if (already.length === 0) {
      page.once("dialog", (d) => d.accept());
      await page.getByRole("button", { name: /Convert to .*Order/i }).click();
    }
    const orders = await db("orders.Order", { source_quotes__id: str(state.quoteAId) });
    expect(orders.length).toBe(1);
    state.orderId = str(orders[0].id);
    state.orderNumber = str(orders[0].order_number);
    const quoteA = await dbOne("orders.Quote", { pk: str(state.quoteAId) });
    expect(str(quoteA.status)).toBe("accepted");
    expect(str(quoteA.converted_to_order_id)).toBe(state.orderId);
    expect(str(orders[0].engagement_id), "the order joins the deal").toBe(state.engagementId);
    await expect(page).toHaveURL(new RegExp(`/erp/orders/${state.orderId}/edit/`));
  });

  step(4, "quote B stays on the deal with its own status; nothing auto-cancelled", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    if (!state.quoteBId) blocked("R3-no-second-quote-on-engagement", "no second quote exists on the deal");
    await openEngagementFromQuotes(ctx, str(state.quoteANumber));
    const quoteB = await dbOne("orders.Quote", { pk: str(state.quoteBId) });
    expect(str(quoteB.status), "quote B untouched by A's conversion").toBe("draft");
    expect(str(quoteB.converted_to_order_id)).toBe("");
    expect(str(quoteB.superseded_by_id)).toBe("");
    const docs = page.locator("#documents li");
    await expect(docs.filter({ hasText: str(state.quoteANumber) })).toContainText("Accepted");
    const rowB = docs.filter({ hasText: str(state.quoteBNumber) });
    await expect(rowB).toContainText(STATUS_LABEL[str(quoteB.status)] ?? str(quoteB.status));
    expectMoney((await rowB.innerText()).split("·").pop() ?? "", "quote B total");
    await expect(docs.filter({ hasText: str(state.orderNumber) })).toBeVisible();
    const quotes = await db("orders.Quote", { engagement: str(state.engagementId) });
    expect(quotes.map((q) => str(q.status)).sort()).toEqual(["accepted", "draft"]);
  });

  step(5, "the deal itself: still open, no quote cancelled by the conversion", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    await openEngagementFromQuotes(ctx, str(state.quoteANumber));
    const eng = await dbOne("crm.Engagement", { pk: str(state.engagementId) });
    // Converting a quote wins the deal (spec 1.6, win_for_conversion) and
    // leaves every other quote on it alone; it is never marked lost.
    state.engagementStatusAfterConvert = str(eng.status);
    expect(str(eng.status)).toBe("won");
    const cancelled = await db("orders.Quote", { engagement: str(state.engagementId), status: "cancelled" });
    expect(cancelled.length).toBe(0);
    const history = await db("crm.EngagementStageChange", { engagement: str(state.engagementId) });
    expect(history.filter((h) => str(h.to_status) === "lost").length).toBe(0);
    await expect(page.locator("#documents")).toContainText(str(state.orderNumber));
  });
});
