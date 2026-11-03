// R4 "Procure to stock" (docs/release-hardening-plan.md section 4, R4 row).
//
// supplier -> purchase order -> partial receipt -> stock on hand -> landed
// cost -> the rest received -> PO closed; an order line on a draft PO is
// locked to its shipment. Built on 2026-10-17 while the PO editor could
// not save (R1.12), as steps `blocked(...)` behind that finding; every
// finding it carried is fixed, so it now runs end to end (lib/harness.ts).
// Reached by clicking, asserted through /harness/state/.
import { blocked, journey, expect, type Ctx } from "../lib/harness";
import {
  addLine,
  card,
  clickNav,
  expectMoney,
  num,
  pickCustomer,
  pickSku,
  str,
} from "../lib/erp";

const COMPANY =
  process.env.HARNESS_COMPANY ?? "0c0ffee0-4e1e-4a5e-9a11-0000000d0001";

async function openPurchaseOrder(ctx: Ctx, poNumber: string, poId: string) {
  const { page } = ctx;
  await clickNav(page, "Purchase Orders");
  await page.getByRole("link", { name: poNumber, exact: true }).first().click();
  await expect(page).toHaveURL(new RegExp(`/erp/purchasing/${poId}/edit/`));
}

journey("R4", "Procure to stock", (step) => {
  step(1, "a new supplier through Suppliers > New", async (ctx) => {
    const { page, state, db, dbOne, login } = ctx;
    await login("purchasing");
    await page.goto("/erp/");
    const stamp = Date.now().toString(36).toUpperCase();
    state.supplierCode = `SUP-R4-${stamp}`;
    state.supplierName = `Northwind Fasteners ${stamp}`;
    await clickNav(page, "Suppliers");
    await page.getByRole("link", { name: /^New/ }).first().click();
    await expect(page).toHaveURL(/\/erp\/purchasing\/suppliers\/new\//);
    await page.fill("#id_supplier_code", str(state.supplierCode));
    await page.fill("#id_company_name", str(state.supplierName));
    await page.fill("#id_display_name", "Northwind");
    await page.selectOption("#id_supplier_type", "manufacturer");
    await page.locator("#id_is_active").check();
    await page.locator("#id_is_approved").check();
    await page.fill("#id_primary_contact_name", "Priya Natarajan");
    await page.fill("#id_primary_contact_email", "orders@northwind-fasteners.example.com");
    await page.selectOption("#id_company", COMPANY);
    await page.locator("button[name='_action'][value='save']").click();
    await expect(page).toHaveURL(/\/erp\/purchasing\/suppliers\/$/);
    const supplier = await dbOne("purchasing.Supplier", { supplier_code: str(state.supplierCode) });
    state.supplierId = str(supplier.id);
    expect(str(supplier.company_id)).toBe(COMPANY);
    expect(supplier.is_active).toBe(true);
    await expect(page.locator("tr", { hasText: str(state.supplierCode) })).toBeVisible();
    void db;
  });

  step(2, "a purchase order for the supplier with two lines, saved", async (ctx) => {
    const { page, state, db } = ctx;
    // Found 2026-10-17 (R1.12, fixed on hardening/r1-fixes): POLineItemForm
    // required purchasing_uom, which the editor never renders.
    await clickNav(page, "Purchase Orders");
    await page.getByRole("link", { name: /^New/ }).first().click();
    await expect(page).toHaveURL(/\/erp\/purchasing\/new\/$/);
    await pickCustomer(page, /Northwind/); // the supplier search widget has the same shape
    const rows = page.locator("tr.line-item-row");
    const first = rows.first();
    await pickSku(first, "ACT-500W");
    await first.locator("input[name$='-quantity_ordered']").fill("10");
    await first.locator("input[name$='-unit_cost']").fill("90.00");
    await first.locator("input[name$='-unit_cost']").dispatchEvent("input");
    await page.locator("#btn-add-row").click();
    const second = rows.nth(1);
    await pickSku(second, "BRK-90-HD");
    await second.locator("input[name$='-quantity_ordered']").fill("50");
    await second.locator("input[name$='-unit_cost']").fill("2.50");
    await second.locator("input[name$='-unit_cost']").dispatchEvent("input");
    await page.locator("button[name='_action'][value='save']").click();
    await expect(page).toHaveURL(/\/erp\/purchasing\/$/, { timeout: 8000 });
    const pos = await db("purchasing.PurchaseOrder", { supplier: str(state.supplierId), _order: "-created_at" });
    expect(pos.length).toBeGreaterThanOrEqual(1);
    state.poId = str(pos[0].id);
    state.poNumber = str(pos[0].po_number);
    const lines = await db("purchasing.POLineItem", { purchase_order: state.poId, _order: "created_at" });
    expect(lines.length).toBe(2);
    expect(num(pos[0].subtotal)).toBe(10 * 90 + 50 * 2.5);
    const row = page.locator("tr", { hasText: str(state.poNumber) }).first();
    expectMoney(await row.locator("td").last().innerText(), "PO total in the list");
  });

  step(3, "a partial receipt against the purchase order", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    if (!state.poId) blocked("R1-no-po-from-back-office", "no purchase order was saved in step 2");
    // Found 2026-10-17 (fixed the same day): receiving existed only as
    // plain CRUD in the API and the PO editor offered no Receive action.
    // "Receive" on the editor opens the Receive screen, which posts
    // purchasing.services.receive_purchase_order (the API's path too).
    if (state.stockBefore == null) {
      const stock = await db("inventory.WarehouseStock", { variant__sku: "ACT-500W" });
      state.stockBefore = stock.reduce((s, r) => s + num(r.quantity), 0);
    }
    await openPurchaseOrder(ctx, str(state.poNumber), str(state.poId));
    const control = page.getByRole("link", { name: /^Receive$/ });
    await expect(control.first(), "a Receive action on the purchase order").toBeVisible({ timeout: 3000 });
    await control.first().click();
    await expect(page).toHaveURL(new RegExp(`/erp/purchasing/${state.poId}/receive/`));
    // 4 of the 10 actuators arrive; no brackets yet (the field defaults to
    // what is still to come).
    const line = page.locator("tr", { hasText: "ACT-500W" }).first();
    await line.locator("input[name$='quantity_received']").fill("4");
    await page.locator("tr", { hasText: "BRK-90-HD" }).first().locator("input[name$='quantity_received']").fill("0");
    await page.getByRole("button", { name: /^Receive$/ }).click();
    await expect(page).toHaveURL(new RegExp(`/erp/purchasing/${state.poId}/edit/`));
    await expect(page.locator(".alert, [role=alert]").first()).toContainText(/Receipt REC-/);
    const receipts = await db("purchasing.POReceipt", { purchase_order: str(state.poId) });
    expect(receipts.length).toBe(1);
    state.receiptId = str(receipts[0].id);
    const poLines = await db("purchasing.POLineItem", { purchase_order: str(state.poId), our_sku: "ACT-500W" });
    expect(num(poLines[0].quantity_received)).toBe(4);
    expect(str(poLines[0].receiving_status)).toBe("partial");
    const po = await dbOne("purchasing.PurchaseOrder", { pk: str(state.poId) });
    expect(str(po.receiving_status)).toBe("partial");
    expect(str(po.status), "a short delivery leaves the order receiving").toBe("receiving");
    expect(str(receipts[0].status)).toBe("posted");
  });

  step(4, "stock on hand shows the received quantity", async (ctx) => {
    const { page, state, db } = ctx;
    if (!state.receiptId) blocked("R4-no-receiving-ui", "nothing was received in step 3");
    const before = num(state.stockBefore ?? 0);
    await clickNav(page, "Products");
    await page.locator("input[name='q']").first().fill("ACT-500W");
    await page.locator("input[name='q']").first().press("Enter");
    await page.locator("tr", { hasText: "ACT-500W" }).first().locator("a").first().click();
    await expect(page.locator("body")).toContainText(/stock/i);
    const stock = await db("inventory.WarehouseStock", { variant__sku: "ACT-500W" });
    const onHand = stock.reduce((s, r) => s + num(r.quantity), 0);
    expect(onHand, "stock on hand after the receipt").toBe(before + 4);
  });

  step(5, "freight and other charges allocate as landed cost on the lines", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    if (!state.poId) blocked("R1-no-po-from-back-office", "no purchase order was saved in step 2");
    await openPurchaseOrder(ctx, str(state.poNumber), str(state.poId));
    await page.fill("#id_shipping_cost", "100.00");
    await page.fill("#id_other_charges", "20.00");
    await page.locator("button[name='_action'][value='save_continue']").click();
    await expect(page).toHaveURL(new RegExp(`/erp/purchasing/${state.poId}/edit/`));
    const po = await dbOne("purchasing.PurchaseOrder", { pk: str(state.poId) });
    expect(num(po.shipping_cost)).toBe(100);
    expect(num(po.other_charges)).toBe(20);
    const lines = await db("purchasing.POLineItem", { purchase_order: str(state.poId) });
    let landed = 0;
    for (const line of lines) {
      expect(num(line.landed_cost_per_unit), `landed cost on ${line.our_sku}`).toBeGreaterThan(num(line.unit_cost));
      landed += num(line.landed_cost_per_unit) * num(line.quantity_ordered);
    }
    // Every dollar of freight and charges lands on a line (4-dp rounding).
    expect(Math.abs(landed - (num(po.subtotal) + 120))).toBeLessThan(0.05);
  });

  step(6, "the rest arrives; the purchase order is received, then closed", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    if (!state.poId) blocked("R1-no-po-from-back-office", "no purchase order was saved in step 2");
    // Observed 2026-10-17 and fixed the same day: the editor's Status field
    // wrote any status directly. The action bar's status control now offers
    // only PurchaseOrder.VALID_TRANSITIONS, so the order is received first
    // (the remaining 6 actuators and 50 brackets, the screen's defaults).
    let po = await dbOne("purchasing.PurchaseOrder", { pk: str(state.poId) });
    if (str(po.status) === "receiving") {
      await openPurchaseOrder(ctx, str(state.poNumber), str(state.poId));
      await page.getByRole("link", { name: /^Receive$/ }).click();
      await expect(page).toHaveURL(new RegExp(`/erp/purchasing/${state.poId}/receive/`));
      await page.getByRole("button", { name: /^Receive$/ }).click();
      await expect(page).toHaveURL(new RegExp(`/erp/purchasing/${state.poId}/edit/`));
      po = await dbOne("purchasing.PurchaseOrder", { pk: str(state.poId) });
      expect(str(po.status)).toBe("received");
      expect(str(po.receiving_status)).toBe("received");
      const lines = await db("purchasing.POLineItem", { purchase_order: str(state.poId) });
      for (const line of lines) expect(num(line.quantity_received), `received on ${line.our_sku}`).toBe(num(line.quantity_ordered));
      expect((await db("purchasing.POReceipt", { purchase_order: str(state.poId) })).length).toBe(2);
    }
    if (str(po.status) === "received") {
      await openPurchaseOrder(ctx, str(state.poNumber), str(state.poId));
      const select = page.locator("#status-transition-select");
      const offered = await select.locator("option").evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value).filter(Boolean));
      expect(offered, "only the legal moves from received").toEqual(["set_status_closed"]);
      await select.selectOption("set_status_closed");
      page.once("dialog", (d) => d.accept());
      await page.locator("#status-apply-btn").click();
      await expect(page).toHaveURL(new RegExp(`/erp/purchasing/${state.poId}/edit/`));
      await expect(page.locator(".alert, [role=alert]").first()).toContainText(/is now closed/);
    }
    po = await dbOne("purchasing.PurchaseOrder", { pk: str(state.poId) });
    expect(str(po.status)).toBe("closed");
    // Closed is terminal: no status control, no Receive, no Cancel.
    await openPurchaseOrder(ctx, str(state.poNumber), str(state.poId));
    await expect(page.locator("#status-transition-select")).toHaveCount(0);
    await expect(page.getByRole("link", { name: /^Receive$/ })).toHaveCount(0);
    await expect(page.locator("button[value='cancel_order']")).toHaveCount(0);
  });

  step(7, "an order's drop-ship shipment gets a purchase order", async (ctx) => {
    const { page, state, db } = ctx;
    // Found 2026-10-17 (R1.11, fixed on hardening/r1-fixes): "Create PO"
    // from a shipment group prefilled rows the PO page could not save.
    await clickNav(page, "Sales Orders");
    await page.getByRole("link", { name: /^New/ }).first().click();
    await expect(page).toHaveURL(/\/erp\/orders\/new\//);
    await pickCustomer(page, /Harbor Lights/);
    await addLine(page, 0, "SNR-LASER-30", "1");
    const c = card(page, 0);
    await c.locator(".sg-supplier-search").fill("Keystone");
    await c.locator(".sg-supplier-results .list-group-item", { hasText: /Keystone/ }).first().click();
    await page.locator("button[name='_action'][value='save_continue']").click();
    await expect(page).toHaveURL(/\/erp\/orders\/[0-9a-f-]+\/edit\//);
    state.orderId = page.url().match(/\/orders\/([0-9a-f-]+)\/edit\//)?.[1] ?? "";
    const order = (await db("orders.Order", { pk: str(state.orderId) }))[0];
    state.orderNumber = str(order.order_number);
    const groups = await db("orders.ShipmentGroup", { order: str(state.orderId) });
    expect(groups.length).toBe(1);
    expect(str(groups[0].supplier_id), "the vendor kept on the shipment").not.toBe("");
    await card(page, 0).locator(".sg-create-po").click();
    await expect(page).toHaveURL(/\/erp\/purchasing\/new\/\?/);
    await page.locator("button[name='_action'][value='save']").click();
    await expect(page).toHaveURL(/\/erp\/purchasing\/$/, { timeout: 8000 });
    const pos = await db("purchasing.PurchaseOrder", { order: str(state.orderId) });
    expect(pos.length).toBe(1);
    state.lockPoId = str(pos[0].id);
    state.lockPoNumber = str(pos[0].po_number);
    const poLines = await db("purchasing.POLineItem", { purchase_order: state.lockPoId });
    expect(poLines.map((l) => str(l.order_line_item_id)).filter(Boolean).length, "PO lines linked to the order lines").toBe(1);
  });

  step(8, "the order line on the draft PO is locked to its shipment", async (ctx) => {
    const { page, state, db } = ctx;
    if (!state.lockPoId) blocked("R1-po-from-group-unsavable", "no purchase order was created from the shipment in step 7");
    await clickNav(page, "Sales Orders");
    await page.getByRole("link", { name: str(state.orderNumber) }).first().click();
    await expect(page).toHaveURL(new RegExp(`/erp/orders/${state.orderId}/edit/`));
    const row = page.locator("tr.line-item-row", { hasText: "SNR-LASER-30" }).first();
    await expect(row).toHaveAttribute("data-move-lock", new RegExp(str(state.lockPoNumber)));
    await expect(row.locator(".li-move-select")).toHaveCount(0);
    await expect(row.locator(".li-drag-handle")).toHaveCount(0);
    const po = (await db("purchasing.PurchaseOrder", { pk: str(state.lockPoId) }))[0];
    expect(str(po.status)).toBe("draft");
  });
});
