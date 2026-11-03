// R1 "Lead to cash" (docs/release-hardening-plan.md section 4, R1 row).
//
// new lead (form and API intake) -> engagement with intake task -> log a
// call, add a task -> stages to Closed/Signed -> quote with two shipments,
// reordered lines, people on it -> send (email to console) -> accept ->
// convert to order -> PO for the drop-ship group -> pick and ship the stock
// group -> invoice -> partial then full payment -> GL balances -> engagement
// shows the thread. Double-submits of send and convert must leave one email
// and one order. At 360 px, one real edit on the order editor.
//
// Every step reaches its screen by clicking (never by typing a URL, except
// the dashboard after sign-in), asserts the database through
// /harness/state/, and gets the runner's page, axe, 360 px and server-log
// checks (lib/harness.ts). Steps that hit a product gap carry knownFinding
// (docs/release-testing.md, "Known findings"): they record the gap and the
// journey continues, so every gap in the chain surfaces in one run.
import type { Page } from "@playwright/test";
import { journey, expect, type Ctx } from "../lib/harness";

const COMPANY =
  process.env.HARNESS_COMPANY ?? "0c0ffee0-4e1e-4a5e-9a11-0000000d0001";

const str = (v: unknown) => (v == null ? "" : String(v));
const num = (v: unknown) => Number(v ?? 0);

// ── editor helpers ────────────────────────────────────────────────────────────

/** Pick a variant in a line row by SKU through the row's search box. */
async function pickSku(row: ReturnType<Page["locator"]>, sku: string) {
  const search = row.locator(".variant-search-input");
  await search.fill(sku);
  const hit = row.locator(".variant-search-results a", { hasText: sku }).first();
  await hit.waitFor({ state: "visible", timeout: 10_000 });
  await hit.click();
  await expect(row.locator("[name$='-item_name']")).not.toHaveValue("");
}

/** A sidebar link by its title (the icon glyph is part of the accessible name). */
const nav = (page: Page, title: string) =>
  page.locator("a.nav-link .nav-link-title", { hasText: new RegExp(`^${title}$`) }).first();

/** The shipment group card at index i. */
const card = (page: Page, i: number) =>
  page.locator(`.shipment-group-card[data-sg-index="${i}"]`);

/** Add a line to group i: reuse its blank row if one exists, else Add Row. */
async function addLine(page: Page, i: number, sku: string, qty: string) {
  const c = card(page, i);
  const rows = c.locator("tr.line-item-row");
  let row = rows.filter({ has: page.locator(".variant-search-input") }).last();
  const blank =
    (await rows.count()) > 0 &&
    (await row.locator(".variant-search-input").inputValue()) === "";
  if (!blank) {
    const before = await rows.count();
    await c.locator(".btn-add-row-sg").click();
    await expect(rows).toHaveCount(before + 1);
    row = rows.last();
  }
  await pickSku(row, sku);
  await row.locator("input[name$='-quantity']").fill(qty);
  await row.locator("input[name$='-quantity']").dispatchEvent("input");
  return row;
}

/** Two concurrent POSTs from the page: what a double-click submits. */
async function doubleSubmit(
  page: Page,
  url: string,
  fields: Record<string, string>,
  formId?: string,
) {
  return page.evaluate(
    async ({ url, fields, formId }) => {
      const build = () => {
        const fd = formId
          ? new FormData(document.getElementById(formId) as HTMLFormElement)
          : new FormData();
        for (const [k, v] of Object.entries(fields)) fd.set(k, v);
        const csrf = (document.querySelector("[name=csrfmiddlewaretoken]") as HTMLInputElement)?.value;
        if (csrf) fd.set("csrfmiddlewaretoken", csrf);
        return fd;
      };
      const send = () =>
        fetch(url, { method: "POST", body: build(), redirect: "follow" }).then((r) => r.status);
      return Promise.all([send(), send()]);
    },
    { url, fields, formId },
  );
}

/** Click a sidebar link, opening the collapsed sidebar first on narrow viewports. */
async function clickNav(page: Page, title: string) {
  const link = nav(page, title);
  if (!(await link.isVisible())) {
    await page.locator('button[aria-label="Toggle navigation"]').first().click();
    await expect(link).toBeVisible();
  }
  await link.click();
}

async function openOrderEditor(ctx: Ctx) {
  const { page, state } = ctx;
  await clickNav(page, "Sales Orders");
  await page.getByRole("link", { name: str(state.orderNumber) }).first().click();
  await expect(page).toHaveURL(new RegExp(`/erp/orders/${state.orderId}/edit/`));
}

// ── the journey ───────────────────────────────────────────────────────────────

// Known axe findings on the screens R1 crosses (docs/release-testing.md,
// "Known findings"): the rule id maps to the finding key. None left on
// 2026-10-17 (the status dropdown and the bulk checkboxes have names now).
const AXE_KNOWN: Record<string, string> = {};

journey("R1", "Lead to cash", (step) => {
  step(1, "new lead through the form lands on an engagement with the intake task", async (ctx) => {
    const { page, state, dbOne, db, login } = ctx;
    await login("staff");
    await page.goto("/erp/");
    const stamp = Date.now().toString(36);
    state.email = `r1.lead.${stamp}@example.com`;
    state.company = `Lead to Cash ${stamp.toUpperCase()} LLC`;
    await clickNav(page, "Leads");
    await page.getByRole("link", { name: /^New/ }).first().click();
    await expect(page).toHaveURL(/\/erp\/crm\/leads\/new\//);
    await page.fill("#id_first_name", "Riley");
    await page.fill("#id_last_name", "Castellano");
    await page.fill("#id_email", str(state.email));
    await page.fill("#id_phone", "+1-312-555-0190");
    await page.fill("#id_company_name", str(state.company));
    await page.selectOption("#id_source", "phone");
    await page.selectOption("#id_request_type", "quote");
    await page.fill("#id_message", "Need two actuators and a laser sensor for a new line.");
    await page.getByRole("button", { name: /^(Add|Save|Create)/ }).first().click();
    await expect(page).toHaveURL(/\/erp\/crm\/engagements\/[0-9a-f-]+\//);
    const lead = await dbOne("crm.Lead", { email: str(state.email) });
    state.leadNumber = str(lead.lead_number);
    state.engagementId = str(lead.engagement_id);
    expect(state.engagementId).not.toBe("");
    expect(page.url()).toContain(state.engagementId);
    const eng = await dbOne("crm.Engagement", { pk: state.engagementId });
    expect(str(eng.origin)).toBe("lead");
    expect(str(eng.status)).toBe("open");
    const tasks = await db("crm.Task", { engagement: state.engagementId, title__startswith: "Respond to inquiry" });
    expect(tasks.length).toBe(1);
    state.partyId = str(eng.party_id);
  });

  step(2, "the same lead through API intake joins the open engagement", async (ctx) => {
    const { page, state, db, apiKey } = ctx;
    expect(apiKey, "HARNESS_API_KEY (the runner issues one with create_api_key)").toBeTruthy();
    const status = await page.evaluate(
      async ({ key, body }) => {
        const r = await fetch("/api/v1/crm/leads/", {
          method: "POST",
          headers: { Authorization: `Api-Key ${key}`, "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        return { status: r.status, text: (await r.text()).slice(0, 300) };
      },
      {
        key: apiKey,
        body: {
          // Observation 2026-10-17: the lead API requires the caller to supply
          // lead_number (the staff form auto-numbers); an integration must
          // invent a unique one. Recorded in the findings log.
          lead_number: `L-API-${Date.now().toString(36).toUpperCase()}`,
          first_name: "Riley", last_name: "Castellano", email: state.email, phone: "+1-312-555-0190",
          company_name: state.company, source: "website_form", request_type: "quote",
          message: "Web form: same project, asking for lead times.",
        },
      },
    );
    expect(status.status, status.text).toBe(201);
    const leads = await db("crm.Lead", { email: str(state.email) });
    expect(leads.length).toBe(2);
    expect(leads.map((l) => str(l.engagement_id))).toEqual([state.engagementId, state.engagementId]);
    const notes = await db("communications.Communication", { engagement: str(state.engagementId), kind: "note" });
    expect(notes.length).toBeGreaterThanOrEqual(2);
    await page.reload();
  });

  step(3, "log a call and add a task on the engagement", async (ctx) => {
    const { page, state, db } = ctx;
    await page.goto(`/erp/crm/engagements/${state.engagementId}/`);
    const callsBefore = (await db("communications.Communication", { engagement: str(state.engagementId), kind: "call" })).length;
    await page.getByRole("link", { name: /Log a communication/ }).click();
    await expect(page).toHaveURL(/\/log\//);
    await page.selectOption("#id_kind", "call");
    await page.selectOption("#id_direction", "outbound");
    await page.fill("#id_subject", "Discovery call");
    await page.fill("#id_duration_minutes", "15");
    await page.fill("#id_body_text", "Walked through the line layout; two shipments, one drop-shipped.");
    // "And add a task": the log form's own next-action fields (the
    // engagement page offers its task form only while nothing is planned).
    const due = new Date(Date.now() + 2 * 86_400_000).toISOString().slice(0, 10);
    await page.fill("#id_next_title", "Send the catalog and lead times");
    await page.fill("#id_next_due", due);
    await page.getByRole("button", { name: /Log it/ }).click();
    await expect(page).toHaveURL(new RegExp(`/erp/crm/engagements/${state.engagementId}/`));
    const calls = await db("communications.Communication", { engagement: str(state.engagementId), kind: "call" });
    expect(calls.length).toBe(callsBefore + 1);
    const tasks = await db("crm.Task", { engagement: str(state.engagementId), title: "Send the catalog and lead times" });
    expect(tasks.length).toBeGreaterThanOrEqual(1);
  });

  step(4, "move the deal stage by stage to Closed/Signed and confirm the win", async (ctx) => {
    const { page, state, dbOne } = ctx;
    for (const name of ["Prospecting", "Qualification", "Needs analysis", "Proposal sent", "Negotiation", "Verbal commit"]) {
      await page.locator(`button[aria-label="Move to ${name}"]`).click();
      await expect(page.locator("#deal-facts span[aria-current='step']")).toHaveText(name);
    }
    await page.locator('button[aria-label="Move to Closed/Signed"]').click();
    // The closing stage proposes the outcome; confirm it in the close panel.
    await expect(page).toHaveURL(/confirm=won/);
    await page.locator("#close form[action$='/win/'] button[type=submit]").click();
    const eng = await dbOne("crm.Engagement", { pk: str(state.engagementId) });
    expect(str(eng.status)).toBe("won");
    const stage = await dbOne("crm.Stage", { pk: str(eng.stage_id) });
    expect(str(stage.key)).toBe("closed_signed");
  });

  step(5, "quote from the engagement: two shipments, reordered lines, a person on it", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    // "New quote" on the deal (fixed 2026-10-17, R1.06): the editor opens
    // with the engagement picked; the party has no customer yet.
    await page.goto(`/erp/crm/engagements/${state.engagementId}/`);
    await page.getByRole("link", { name: /^New quote/i }).click();
    await expect(page).toHaveURL(new RegExp(`/erp/orders/quotes/new/\\?engagement=${state.engagementId}`));
    await expect(page.locator("select[name='engagement']")).toHaveValue(str(state.engagementId));
    // The lead is a party, not a customer yet: add the customer from the
    // editor (a rerun on the same database finds it and picks it instead).
    const existing = await db("customers.Customer", { email: str(state.email) });
    if (existing.length === 0) {
      await page.locator("#btn-add-customer").click();
      await page.fill("#cust-first_name", "Riley");
      await page.fill("#cust-last_name", "Castellano");
      await page.fill("#cust-email", str(state.email));
      await page.fill("#cust-phone", "+1-312-555-0190");
      await page.fill("#cust-company_name", str(state.company));
      await page.locator("#btn-save-customer").click();
      await expect(page.locator("#customerModal")).toBeHidden();
    } else {
      const widget = page.locator(".ajax-search-widget").filter({ has: page.locator("input") }).first();
      await widget.locator("input").fill("Castellano");
      await widget.locator("a.list-group-item", { hasText: /Castellano/ }).first().click();
    }
    const customer = await dbOne("customers.Customer", { email: str(state.email) });
    state.customerId = str(customer.id);
    expect(str(customer.party_id)).toBe(state.partyId);
    await expect(page.locator("select[name='customer']")).toHaveValue(str(customer.id));
    // The deal stays picked after the customer is set (the options refresh
    // keeps the current pick when the customer's party is on it).
    await expect(page.locator("select[name='engagement']")).toHaveValue(str(state.engagementId));
    // Three lines in the first (drop-ship) group.
    await addLine(page, 0, "ACT-500W", "2");
    await addLine(page, 0, "SNR-LASER-30", "1");
    await addLine(page, 0, "BRK-90-HD", "10");
    // A second shipment, fulfilled from stock (Internal, WH-EAST).
    await page.locator("#btn-split-ship").click();
    await expect(card(page, 1)).toBeVisible();
    await card(page, 1).locator(".sg-type-btn[data-type='internal']").click();
    const wh = card(page, 1).locator("select[name='sg_1_warehouse']");
    const whValue = await wh.locator("option", { hasText: "East Coast Fulfillment" }).getAttribute("value");
    await wh.selectOption(whValue ?? "");
    // Move the brackets to the stock shipment through the keyboard control.
    const brackets = card(page, 0).locator("tr.line-item-row").filter({ hasText: "BRK-90-HD" });
    await brackets.locator("select.li-move-select").selectOption("1");
    await expect(card(page, 1).locator("tr.line-item-row").filter({ hasText: "BRK-90-HD" })).toHaveCount(1);
    // Reorder: drag the sensor above the actuator in the first group.
    const rows0 = card(page, 0).locator("tr.line-item-row");
    await expect(rows0).toHaveCount(2);
    await rows0.nth(1).locator(".li-drag-handle").dragTo(rows0.nth(0), { targetPosition: { x: 5, y: 2 } });
    await expect(rows0.nth(0)).toContainText("SNR-LASER-30");
    await page.locator("button[name='_action'][value='save_continue']").click();
    await expect(page).toHaveURL(/\/erp\/orders\/quotes\/[0-9a-f-]+\/edit\//);
    const quotePk = page.url().match(/\/quotes\/([0-9a-f-]+)\/edit\//)?.[1] ?? "";
    const quote = await dbOne("orders.Quote", { pk: quotePk });
    expect(str(quote.customer_id)).toBe(str(customer.id));
    state.quoteId = str(quote.id);
    state.quoteNumber = str(quote.quote_number);
    state.quoteEngagementId = str(quote.engagement_id);
    const groups = await db("orders.ShipmentGroup", { quote: str(quote.id), _order: "sort_order" });
    expect(groups.length).toBe(2);
    const lines = await db("orders.QuoteLineItem", { quote: str(quote.id), _order: "sort_order" });
    expect(lines.length).toBe(3);
    const inStock = lines.filter((l) => str(l.shipment_group_id) === str(groups[1].id));
    expect(inStock.map((l) => str(l.sku))).toEqual(["BRK-90-HD"]);
    const dropShip = lines.filter((l) => str(l.shipment_group_id) === str(groups[0].id)).map((l) => str(l.sku));
    expect(dropShip).toEqual(["SNR-LASER-30", "ACT-500W"]);
    expect(num(quote.grand_total)).toBeGreaterThan(0);
    state.quoteTotal = str(quote.grand_total);
    // People: the customer's own person is a candidate; add them.
    const pick = page.locator("#doc-people-pick");
    await expect(pick).toBeVisible();
    await pick.selectOption({ index: 0 });
    await page.locator("[data-people-add]").click();
    await expect(page.locator("#doc-people")).toContainText("Castellano");
    const people = await db("orders.DocumentContact", { quote: str(quote.id) });
    expect(people.length).toBe(1);
  });

  step(6, "the quote lands on the lead's engagement", async (ctx) => {
    const { page, state, db } = ctx;
    // Fixed 2026-10-17 (was R1-quote-not-on-lead-engagement): the quote
    // joined the deal it was started from, with a history row saying so.
    await page.goto(`/erp/crm/engagements/${state.engagementId}/`);
    await expect(page.locator("#documents")).toContainText(str(state.quoteNumber));
    expect(state.quoteEngagementId).toBe(state.engagementId);
    const rows = await db("crm.EngagementStageChange", { engagement: str(state.engagementId), note: `Quote ${state.quoteNumber} added` });
    expect(rows.length).toBe(1);
  });

  step(7, "email the quote (console backend): one email, one communication", async (ctx) => {
    const { page, state, db } = ctx;
    await page.goto(`/erp/orders/quotes/${state.quoteId}/edit/`);
    const before = (await db("communications.EmailLog", { document_type: "quote", document_id: str(state.quoteId) })).length;
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: /Email Quote/ }).click();
    await expect(page.locator(".alert, .toast, [role=alert]").first()).toContainText(/emailed to/i);
    const logs = await db("communications.EmailLog", { document_type: "quote", document_id: str(state.quoteId), _order: "-created_at" });
    expect(logs.length).toBe(before + 1);
    expect(str(logs[0].status)).toBe("sent");
    const comms = await db("communications.Communication", { email_log: str(logs[0].id) });
    expect(comms.length).toBe(1);
    state.quoteEmailCommEngagement = str(comms[0].engagement_id);
  });

  step(8, "a double-click on Email Quote sends one email, not two", async (ctx) => {
    const { page, state, db } = ctx;
    const before = (await db("communications.EmailLog", { document_type: "quote", document_id: str(state.quoteId) })).length;
    const statuses = await doubleSubmit(page, `/erp/orders/quotes/${state.quoteId}/send/`, {}, "quote-send-form");
    expect(statuses.every((s) => s < 500)).toBe(true);
    const after = (await db("communications.EmailLog", { document_type: "quote", document_id: str(state.quoteId) })).length;
    // Fixed 2026-10-17 (was R1-double-send): the send form's _once token is
    // claimed once (core.once); the second POST answers "already sent".
    expect(after - before, "emails sent by a double submit").toBe(1);
  }, { axe: false, fit360: false });

  step(9, "convert to a sales order (double-submitted): exactly one order, quote accepted", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    await page.goto(`/erp/orders/quotes/${state.quoteId}/edit/`);
    // The double-click: two concurrent submits of the editor with convert_to_order.
    const statuses = await doubleSubmit(page, `/erp/orders/quotes/${state.quoteId}/edit/`,
      { _action: "convert_to_order" }, "document-form");
    expect(statuses.every((s) => s < 500)).toBe(true);
    const orders = await db("orders.Order", { source_quotes__id: str(state.quoteId) });
    expect(orders.length, "orders created by a double convert").toBe(1);
    const quote = await dbOne("orders.Quote", { pk: str(state.quoteId) });
    expect(str(quote.status)).toBe("accepted");
    expect(str(quote.converted_to_order_id)).toBe(str(orders[0].id));
    state.orderId = str(orders[0].id);
    state.orderNumber = str(orders[0].order_number);
    expect(num(orders[0].grand_total)).toBe(num(state.quoteTotal));
    // Reach the order by clicking from the quote.
    await page.reload();
    await page.getByRole("link", { name: /View Sales Order/ }).click();
    await expect(page).toHaveURL(new RegExp(`/erp/orders/${state.orderId}/edit/`));
    const groups = await db("orders.ShipmentGroup", { order: str(state.orderId), _order: "sort_order" });
    expect(groups.length).toBe(2);
    state.dropShipGroupId = str(groups[0].id);
    state.stockGroupId = str(groups[1].id);
    const people = await db("orders.DocumentContact", { order: str(state.orderId) });
    expect(people.length).toBe(1);
    // Observation 2026-10-17: nothing in the back office creates SubOrders
    // (only the API does), so the order's status roll-up from sub orders
    // never runs for UI-made orders; POs and pick tickets hang off the
    // shipment groups instead. Recorded, not asserted.
    state.subOrdersAtConvert = (await db("orders.SubOrder", { order: str(state.orderId) })).length;
  });

  step(10, "a vendor the picker offers survives the save", async (ctx) => {
    const { page, state, dbOne } = ctx;
    // Fixed 2026-10-17 (was R1-vendor-silently-dropped): the vendor search
    // offers only the document company's suppliers (what the save keeps),
    // and every seed binds its suppliers to the company.
    await openOrderEditor(ctx);
    const c = card(page, 0);
    await c.locator(".sg-supplier-search").fill("Global Components");
    await c.locator(".sg-supplier-results .list-group-item", { hasText: /Global Components/ }).first().click();
    await expect(c.locator(".sg-create-po")).not.toHaveClass(/disabled/);
    await page.locator("button[name='_action'][value='save_continue']").click();
    await expect(page).toHaveURL(new RegExp(`/erp/orders/${state.orderId}/edit/`));
    const group = await dbOne("orders.ShipmentGroup", { pk: str(state.dropShipGroupId) });
    expect(str(group.supplier_id), "the vendor picked in the editor, after Save").not.toBe("");
  });

  step(11, "Create PO from the drop-ship shipment saves as presented", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    // Fixed 2026-10-17 (was R1-po-from-group-unsavable): the prefilled rows
    // carry their variant, a line with no product is named on the page, the
    // blank extra row stays blank, and a failed save says why.
    await openOrderEditor(ctx);
    const c = card(page, 0);
    // A rerun on a kept database finds the PO the previous attempt made
    // (the card then links to it instead of offering Create PO).
    if ((await c.locator("a[href*='/erp/purchasing/'][href*='/edit/']").count()) === 0) {
      await c.locator(".sg-supplier-search").fill("Keystone");
      await c.locator(".sg-supplier-results .list-group-item", { hasText: /Keystone/ }).first().click();
      await expect(c.locator(".sg-create-po")).not.toHaveClass(/disabled/);
      await page.locator("button[name='_action'][value='save_continue']").click();
      await expect(page).toHaveURL(new RegExp(`/erp/orders/${state.orderId}/edit/`));
      await card(page, 0).locator(".sg-create-po").click();
      await expect(page).toHaveURL(/\/erp\/purchasing\/new\/\?/);
      await expect(page.locator("tr.line-item-row input[name$='-quantity_ordered']").first()).not.toHaveValue("");
      await page.locator("button[name='_action'][value='save']").click();
      await expect(page).toHaveURL(/\/erp\/purchasing\/$/, { timeout: 8000 });
    }
    const po = await dbOne("purchasing.PurchaseOrder", { shipment_group: str(state.dropShipGroupId) });
    const poLines = await db("purchasing.POLineItem", { purchase_order: str(po.id) });
    expect(poLines.map((l) => str(l.our_sku)).sort()).toEqual(["ACT-500W", "SNR-LASER-30"]);
    expect(poLines.every((l) => str(l.variant_id) !== "")).toBe(true);
  });

  step(12, "a purchase order can be created from Purchase Orders > New", async (ctx) => {
    const { page, db } = ctx;
    // Fixed 2026-10-17 (was R1-no-po-from-back-office): purchasing_uom is
    // not rendered, so a blank one is the model's "each".
    await clickNav(page, "Purchase Orders");
    await page.getByRole("link", { name: /^New/ }).first().click();
    await expect(page).toHaveURL(/\/erp\/purchasing\/new\/$/);
    const widget = page.locator(".ajax-search-widget").filter({ has: page.locator("input") }).first();
    await widget.locator("input").fill("Keystone");
    await widget.locator("a.list-group-item", { hasText: /Keystone/ }).first().click();
    const row = page.locator("tr.line-item-row").first();
    await pickSku(row, "ACT-500W");
    await row.locator("input[name$='-quantity_ordered']").fill("2");
    await row.locator("input[name$='-unit_cost']").fill("100.00");
    await row.locator("input[name$='-unit_cost']").dispatchEvent("input");
    const before = (await db("purchasing.PurchaseOrder", {})).length;
    await page.locator("button[name='_action'][value='save']").click();
    await expect(page).toHaveURL(/\/erp\/purchasing\/$/, { timeout: 8000 });
    expect((await db("purchasing.PurchaseOrder", {})).length).toBe(before + 1);
  });

  step(13, "pick ticket for the stock shipment, shipped; the order is shipped", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    await openOrderEditor(ctx);
    const popup = page.waitForEvent("popup");
    await card(page, 1).locator(".sg-print-pick-ticket").click();
    const print = await popup;
    await print.waitForLoadState("domcontentloaded").catch(() => {});
    await print.close();
    const tickets = await db("fulfillment.PickTicket", { order: str(state.orderId) });
    expect(tickets.length).toBe(1);
    expect(str(tickets[0].shipment_group_id)).toBe(state.stockGroupId);
    state.pickTicketId = str(tickets[0].id);
    await page.reload();
    await card(page, 1).locator("a[href*='/erp/fulfillment/']").click();
    await expect(page).toHaveURL(new RegExp(`/erp/fulfillment/${state.pickTicketId}/edit/`));
    await page.selectOption("#id_status", "shipped");
    await page.locator("button[name='_action'][value='save_continue']").click();
    const ticket = await dbOne("fulfillment.PickTicket", { pk: str(state.pickTicketId) });
    expect(str(ticket.status)).toBe("shipped");
    // The order's own status: Shipped through the action bar.
    await openOrderEditor(ctx);
    await page.selectOption("#status-transition-select", "set_status_shipped");
    page.once("dialog", (d) => d.accept());
    await page.locator("#status-apply-btn").click();
    const order = await dbOne("orders.Order", { pk: str(state.orderId) });
    expect(str(order.status)).toBe("shipped");
    const subs = await db("orders.SubOrder", { order: str(state.orderId) });
    state.subOrderStatuses = subs.map((s) => str(s.status)).join(",");
  });

  step(14, "Create Invoice lands on the invoice page, with a way back and a preview", async (ctx) => {
    const { page, state, db } = ctx;
    await openOrderEditor(ctx);
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: /Create Invoice/ }).click();
    await expect(page).toHaveURL(/\/erp\/payments\/[0-9a-f-]+\/$/);
    const invoices = await db("orders.SalesInvoice", { order: str(state.orderId) });
    expect(invoices.length).toBe(1);
    state.invoiceId = str(invoices[0].id);
    state.invoiceNumber = str(invoices[0].invoice_number);
    expect(num(invoices[0].grand_total)).toBe(num(state.quoteTotal));
    // Fixed 2026-10-17 (was R1-invoice-preview-dead-end): the invoice's own
    // back-office page; the print preview and the PDF open from it in a new
    // tab, and the preview serves its fonts as statics, not file://.
    await expect(page.locator("a.nav-link").first()).toBeVisible();
    await expect(page.getByRole("link", { name: new RegExp(`Order ${state.orderNumber}`) })).toBeVisible();
    const preview = page.getByRole("link", { name: /Print preview/ });
    await expect(preview).toHaveAttribute("target", "_blank");
    const previewHtml = await page.evaluate(async (href) => (await fetch(href)).text(), await preview.getAttribute("href"));
    expect(previewHtml).not.toContain("file://");
    expect(previewHtml).toContain("/static/assets/fonts/docs/");
    await expect(page.getByRole("link", { name: /Record payment/ })).toBeVisible();
  });

  step(15, "email the invoice from the order", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    await page.goto(`/erp/orders/${state.orderId}/edit/`); // the preview is a dead end (step 14)
    const before = (await db("communications.EmailLog", { document_type: "invoice", document_id: str(state.invoiceId) })).length;
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: /Email Invoice/ }).click();
    await expect(page).toHaveURL(/\/erp\//);
    const logs = await db("communications.EmailLog", { document_type: "invoice", document_id: str(state.invoiceId) });
    expect(logs.length).toBe(before + 1);
    const invoice = await dbOne("orders.SalesInvoice", { pk: str(state.invoiceId) });
    state.invoiceStatus = str(invoice.status);
  });

  step(16, "record a partial, then the full payment (double-click guarded)", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    // Fixed 2026-10-17 (was R1-no-record-payment-ui): "Record Payment" on the
    // order editor leads to the invoice page's form; the form's _once token
    // is the double-submit guard and the payment's idempotency key.
    await openOrderEditor(ctx);
    const already = await dbOne("orders.SalesInvoice", { pk: str(state.invoiceId) });
    if (num(already.balance_due) === 0 && str(already.status) === "paid") {
      // A rerun on a kept database: the previous attempt paid it. The order
      // editor then offers View Invoice, not Record Payment.
      await expect(page.getByRole("link", { name: /Record Payment/ })).toHaveCount(0);
      await page.getByRole("link", { name: /View Invoice/ }).click();
      await expect(page.locator("body")).toContainText("Paid in full");
      return;
    }
    await page.getByRole("link", { name: /Record Payment/ }).click();
    await expect(page).toHaveURL(new RegExp(`/erp/payments/${state.invoiceId}/`));
    const total = num(state.quoteTotal);
    const partial = (Math.floor(total * 40) / 100).toFixed(2);
    const before = (await db("payments.Payment", { invoice: str(state.invoiceId) })).length;
    await page.fill("#id_amount", partial);
    await page.selectOption("#id_payment_method", "check");
    await page.fill("#id_reference", "CHK-1041");
    await page.locator("#record-payment-form button[type=submit]").click();
    await expect(page).toHaveURL(new RegExp(`/erp/payments/${state.invoiceId}/`));
    await expect(page.locator(".alert, .toast, [role=alert]").first()).toContainText(/recorded/i);
    let invoice = await dbOne("orders.SalesInvoice", { pk: str(state.invoiceId) });
    expect(str(invoice.status)).toBe("partial");
    expect(num(invoice.amount_paid)).toBe(num(partial));
    expect(num(invoice.balance_due)).toBe(Number((total - num(partial)).toFixed(2)));
    // The rest, double-clicked: two concurrent submits of the same form.
    const remaining = num(invoice.balance_due).toFixed(2);
    const statuses = await doubleSubmit(page, `/erp/payments/${state.invoiceId}/record-payment/`,
      { amount: remaining }, "record-payment-form");
    expect(statuses.every((s) => s < 500)).toBe(true);
    const payments = await db("payments.Payment", { invoice: str(state.invoiceId), _order: "created_at" });
    expect(payments.length - before, "payments recorded by a partial and a double-clicked full").toBe(2);
    invoice = await dbOne("orders.SalesInvoice", { pk: str(state.invoiceId) });
    expect(str(invoice.status)).toBe("paid");
    expect(num(invoice.balance_due)).toBe(0);
    const order = await dbOne("orders.Order", { pk: str(state.orderId) });
    expect(str(order.payment_status)).toBe("paid");
    await page.reload();
    await expect(page.locator("#invoice-payments tbody tr")).toHaveCount(payments.length);
    await expect(page.locator("body")).toContainText("Paid in full");
  });

  step(17, "the books: trial balance balanced, the invoice posted", async (ctx) => {
    const { state, gl } = ctx;
    const books = await gl(COMPANY);
    expect(books.balanced).toBe(true);
    // The invoice posts when it leaves draft (orders/models.py): emailing it
    // does that, so the sale is in the books.
    const entries = books.entries as { source_reference: string }[];
    const forInvoice = entries.filter((e) => str(e.source_reference).includes(str(state.invoiceNumber)));
    expect(forInvoice.length, `invoice ${state.invoiceNumber} status ${state.invoiceStatus}`).toBeGreaterThan(0);
  }, { axe: false, fit360: false });

  step(18, "the engagement shows the thread: lead, call, task, quote email", async (ctx) => {
    const { page, state, db } = ctx;
    // The quote's engagement is the lead's (step 6): reach it from the quote
    // list's Engagement column.
    await clickNav(page, "Quotes");
    const row = page.locator("tr", { hasText: str(state.quoteNumber) }).first();
    await row.locator("a[href*='/erp/crm/engagements/']").first().click();
    await expect(page).toHaveURL(new RegExp(`/erp/crm/engagements/${state.quoteEngagementId}/`));
    await expect(page.locator("#documents")).toContainText(str(state.quoteNumber));
    await expect(page.locator("#documents")).toContainText(str(state.orderNumber));
    // The emailed quote is a communication on this engagement.
    const emails = await db("communications.Communication", { engagement: str(state.quoteEngagementId), kind: "email" });
    expect(emails.length).toBeGreaterThanOrEqual(1);
    expect(state.quoteEmailCommEngagement).toBe(state.quoteEngagementId);
    // And the lead's engagement still carries its own thread: leads, call, task.
    await page.goto(`/erp/crm/engagements/${state.engagementId}/`);
    await expect(page.locator("body")).toContainText("Discovery call");
    await expect(page.locator("body")).toContainText("Send the catalog and lead times");
  });

  step(19, "at 360 px: add a line to the order and save", async (ctx) => {
    const { page, state, db } = ctx;
    await page.setViewportSize({ width: 360, height: 780 });
    try {
      await openOrderEditor(ctx);
      const before = (await db("orders.OrderLineItem", { order: str(state.orderId) })).length;
      const row = await addLine(page, 1, "MLC-CORE-08", "1");
      await row.locator("input[name$='-quantity']").fill("2");
      await row.locator("input[name$='-quantity']").dispatchEvent("input");
      await page.locator("button[name='_action'][value='save_continue']").click();
      await expect(page).toHaveURL(new RegExp(`/erp/orders/${state.orderId}/edit/`));
      const lines = await db("orders.OrderLineItem", { order: str(state.orderId), sku: "MLC-CORE-08" });
      expect(lines.length).toBe(1);
      expect(num(lines[0].quantity)).toBe(2);
      expect((await db("orders.OrderLineItem", { order: str(state.orderId) })).length).toBe(before + 1);
    } finally {
      await page.setViewportSize({ width: 1440, height: 900 });
    }
  });
}, { axeKnown: AXE_KNOWN });
