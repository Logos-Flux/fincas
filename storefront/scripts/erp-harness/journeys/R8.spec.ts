// R8 "People data" (docs/release-hardening-plan.md section 4, R8 row and the
// design block: an erased customer shows a proper label in lists, on old
// orders and in engagement titles, never the raw `erased+<uuid>@invalid`).
//
// seed_release_test holds two Dana Whitfield customers (same name and phone,
// different emails and organisation spellings). Through the UI: a lead from
// the second Dana (her engagement and intake task), a quote for the first with
// her on it -> the duplicate scan -> merge (keep the first) -> the engagement,
// its task, the person on the quote and the employment relationship follow
// the survivor -> unmerge restores every row -> merge again and erase the
// survivor -> nothing personal left in the database -> every screen that
// shows her uses a label, not the tombstone address.
//
// Login: crm-admin (parties.merge_party, parties.erase_party, export_pii).
import { journey, expect, type Ctx } from "../lib/harness";
import { addLine, bodyText, clickNav, leaks, searchList, str } from "../lib/erp-ui";

const DANA1 = "dana.whitfield@example.com"; // seed: Whitfield Fabrication
const DANA2 = "d.whitfield@example.org"; // seed: Whitfield Fabrication Inc.
const PHONE = "+1-312-555-0177";
// Personal values of the erased person; the organisation names stay (business accounts).
const NEEDLES = ["Dana", DANA1, DANA2, "555-0177", "3125550177"];
const TOMBSTONE = /erased\+[0-9a-f-]{36}@invalid/i;

async function mergeReview(ctx: Ctx) {
  // From the Parties list's Duplicates button (R8.03; fixed 2026-10-17).
  await clickNav(ctx.page, "Parties");
  await ctx.page.locator("#party-merge-review-link").click();
  await expect(ctx.page).toHaveURL(/\/erp\/parties\/merge-review\//);
  await expect(ctx.page.locator("h1")).toContainText(/Duplicate parties/);
}

async function approveAndApply(ctx: Ctx, survivor: string, loser: string) {
  const { page, db } = ctx;
  const form = page.locator("form", { has: page.locator(`input[name=survivor][value='${survivor}']`) }).first();
  if (!(await form.isVisible().catch(() => false))) {
    await page.getByRole("button", { name: /Scan now/ }).click();
    await expect(page.locator(".alert").first()).toContainText(/Scan:/);
  }
  await expect(form).toBeVisible();
  await expect(form.locator(`input[name=survivor][value='${loser}']`)).toHaveCount(1);
  await form.locator(`input[name=survivor][value='${survivor}']`).check();
  await form.getByRole("button", { name: /^(Merge|Update approval)$/ }).click();
  await expect(page.locator(".alert").first()).toContainText(/Merge approved/);
  await page.getByRole("button", { name: /^Apply approved/ }).click();
  await page.waitForLoadState("domcontentloaded");
  const logs = (
    await db("parties.PartyMergeLog", { survivor: survivor, loser: loser, _order: "-created_at" })
  ).filter((l) => l.undone_at == null);
  expect(logs.length, "an undoable merge log for the pair").toBe(1);
  return logs[0];
}

journey("R8", "People data", (step) => {
  step(1, "a lead from the second Dana lands on her engagement; the lead form after an API lead", async (ctx) => {
    const { page, state, db, dbOne, login, apiKey } = ctx;
    await login("crm-admin");
    await page.goto("/erp/");
    const c1 = await dbOne("customers.Customer", { email: DANA1 });
    const c2 = await dbOne("customers.Customer", { email: DANA2 });
    state.c1 = str(c1.id);
    state.c2 = str(c2.id);
    // A business customer's Customer.party is its organisation; the person
    // is the party holding the customer's email (the party sync's mirror).
    state.p1 = str((await dbOne("parties.PartyEmail", { email: DANA1 })).party_id);
    state.p2 = str((await dbOne("parties.PartyEmail", { email: DANA2 })).party_id);
    state.org1 = str(c1.party_id);
    state.org2 = str(c2.party_id);
    expect(state.p1).not.toBe(state.p2);
    const formLead = async (email: string, company: string, message: string) => {
      await clickNav(page, "Leads");
      await page.getByRole("link", { name: /^New/ }).first().click();
      await expect(page).toHaveURL(/\/erp\/crm\/leads\/new\//);
      await page.fill("#id_first_name", "Dana");
      await page.fill("#id_last_name", "Whitfield");
      await page.fill("#id_email", email);
      await page.fill("#id_phone", PHONE);
      await page.fill("#id_company_name", company);
      await page.selectOption("#id_source", "phone");
      await page.selectOption("#id_request_type", "quote");
      await page.fill("#id_message", message);
      await page.getByRole("button", { name: /^(Add|Save|Create)/ }).first().click();
      await page.waitForLoadState("domcontentloaded");
      return /\/erp\/crm\/engagements\/[0-9a-f-]+\//.test(page.url());
    };
    const apiLead = async (email: string, company: string, number: string) =>
      page.evaluate(
        async ({ key, body }) => {
          const r = await fetch("/api/v1/crm/leads/", {
            method: "POST",
            headers: { Authorization: `Api-Key ${key}`, "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          return r.status;
        },
        { key: apiKey, body: { lead_number: number, first_name: "Dana", last_name: "Whitfield", email, phone: PHONE,
                               company_name: company, source: "website_form", request_type: "quote",
                               message: "Web form: asking for a quote." } },
      );
    // The second Dana writes in: her lead lands on an engagement for her
    // person party. Through the form; through API intake when the form
    // cannot number it (the finding reproduced below).
    let leads = await db("crm.Lead", { email: DANA2 });
    if (leads.length === 0) {
      const ok = await formLead(DANA2, "Whitfield Fabrication Inc.", "Asking for a quote on brackets for the north line.");
      state.leadChannel = ok ? "form" : "api";
      if (!ok) {
        expect(apiKey, "HARNESS_API_KEY for the API intake fallback").toBeTruthy();
        expect(await apiLead(DANA2, "Whitfield Fabrication Inc.", `L-R8-${Date.now().toString(36).toUpperCase()}`)).toBe(201);
      }
      leads = await db("crm.Lead", { email: DANA2 });
    }
    expect(leads.length).toBe(1);
    expect(str(leads[0].party_id), "the lead matched the second Dana's person party").toBe(state.p2);
    state.engagementId = str(leads[0].engagement_id);
    const eng = await dbOne("crm.Engagement", { pk: state.engagementId });
    expect(str(eng.party_id)).toBe(state.p2);
    const tasks = await db("crm.Task", { engagement: state.engagementId });
    expect(tasks.length).toBeGreaterThanOrEqual(1);
    state.taskId = str(tasks[0].id);
    // Found 2026-10-17 (fixed the same day, crm.tests.test_lead_numbers):
    // Lead.save numbered a new lead from the most recently created lead, so
    // after an API lead with its own number the form minted LD-00001 again
    // and answered 500. Kept as the regression: an API lead, then a form lead.
    expect(apiKey, "HARNESS_API_KEY").toBeTruthy();
    const stamp = Date.now().toString(36);
    expect(await apiLead(`r8.api.${stamp}@example.com`, "Whitfield Fabrication Inc.", `L-R8-${stamp.toUpperCase()}`)).toBe(201);
    expect(await db("crm.Lead", { lead_number: "LD-00001" }), "an LD-00001 lead exists").toHaveLength(1);
    const numbered = await formLead(`r8.form.${stamp}@example.com`, "Whitfield Fabrication Inc.", "A second inquiry from the office.");
    expect(numbered, "the lead form numbered the lead after an API lead").toBe(true);
  });

  step(2, "a quote for the first Dana with her as the person on it", async (ctx) => {
    const { page, state, db } = ctx;
    await clickNav(page, "Quotes");
    // Always make the quote here. When every journey runs on one database
    // (2026-10-18), R1-R3 have already left quotes for this customer with
    // nobody on them, and a guard that reused the newest one failed the
    // people assertion below. The newest quote must be this step's.
    let quotes;
    {
      await page.getByRole("link", { name: /^New/ }).first().click();
      await expect(page).toHaveURL(/\/erp\/orders\/quotes\/new\//);
      const widget = page.locator(".ajax-search-widget").filter({ has: page.locator("input") }).first();
      await widget.locator("input").fill("Whitfield");
      const hits = widget.locator("a.list-group-item", { hasText: /Whitfield/ });
      await hits.first().waitFor({ state: "visible" });
      const byEmail = hits.filter({ hasText: DANA1 });
      if (await byEmail.count()) await byEmail.first().click();
      else await hits.first().click();
      if ((await page.locator("select[name='customer']").inputValue()) !== state.c1) {
        await widget.locator("input").fill("Whitfield");
        await hits.nth(1).click();
      }
      await expect(page.locator("select[name='customer']")).toHaveValue(str(state.c1));
      await addLine(page, 0, "BRK-90-HD", "4");
      await page.locator("button[name='_action'][value='save_continue']").click();
      await expect(page).toHaveURL(/\/erp\/orders\/quotes\/[0-9a-f-]+\/edit\//);
      const pick = page.locator("#doc-people-pick");
      await expect(pick).toBeVisible();
      await pick.selectOption({ index: 0 });
      await page.locator("[data-people-add]").click();
      await expect(page.locator("#doc-people")).toContainText("Whitfield");
      quotes = await db("orders.Quote", { customer: str(state.c1), _order: "-created_at" });
    }
    expect(quotes.length).toBeGreaterThanOrEqual(1);
    state.quoteId = str(quotes[0].id);
    state.quoteNumber = str(quotes[0].quote_number);
    const people = await db("orders.DocumentContact", { quote: str(state.quoteId) });
    expect(people.length).toBe(1);
    expect(str(people[0].party_id)).toBe(state.p1);
    // The employment relationships the party sync wrote (person -> organisation).
    expect((await db("parties.PartyRelationship", { from_party: str(state.p2) })).length).toBeGreaterThanOrEqual(1);
    state.rel1 = (await db("parties.PartyRelationship", { from_party: str(state.p1) })).length;
    state.rel2 = (await db("parties.PartyRelationship", { from_party: str(state.p2) })).length;
  });

  step(3, "the duplicate review is reachable from Parties", async (ctx) => {
    const { page } = ctx;
    // Found 2026-10-17 (fixed the same day): the merge review was linked
    // only from a CRM import's overview. The Parties list now carries a
    // Duplicates button for users holding parties.merge_party.
    await clickNav(page, "Parties");
    await expect(page.locator("a[href*='/erp/parties/merge-review/']").first()).toBeVisible({ timeout: 2000 });
    await mergeReview(ctx);
  });

  step(4, "scan, keep the first Dana, merge, apply", async (ctx) => {
    const { state, dbOne } = ctx;
    await mergeReview(ctx);
    const log = await approveAndApply(ctx, str(state.p1), str(state.p2));
    state.mergeId = str(log.merge_id);
    const loser = await dbOne("parties.Party", { pk: str(state.p2) });
    expect(str(loser.merged_into_id)).toBe(state.p1);
  });

  step(5, "the engagement, its task, the person on the quote and the relationship follow the survivor", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    const eng = await dbOne("crm.Engagement", { pk: str(state.engagementId) });
    expect(str(eng.party_id), "engagement party after the merge").toBe(state.p1);
    const task = await dbOne("crm.Task", { pk: str(state.taskId) });
    expect(str(task.engagement_id)).toBe(state.engagementId);
    const people = await db("orders.DocumentContact", { quote: str(state.quoteId) });
    expect(people.map((p) => str(p.party_id))).toEqual([state.p1]);
    expect((await db("parties.PartyRelationship", { from_party: str(state.p2) })).length, "loser keeps no relationship").toBe(0);
    expect((await db("parties.PartyRelationship", { from_party: str(state.p1) })).length).toBeGreaterThanOrEqual(
      Number(state.rel1) + Number(state.rel2) - 1,
    );
    // The loser's page says so, and the survivor's page shows the engagement.
    await clickNav(page, "Parties");
    await page.getByRole("link", { name: /Show merged and erased/ }).click();
    await page.locator(`a[href='/erp/parties/${state.p2}/']`).first().click();
    await expect(page.locator(".alert").first()).toContainText(/Merged into/);
    await page.locator(`a[href='/erp/parties/${state.p1}/']`).first().click();
    await expect(page.locator("#engagements ~ *, section:has(#engagements)").first()).not.toContainText(/No engagements/);
  });

  step(6, "undo the merge: both Danas are back, every link restored", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    await mergeReview(ctx);
    const form = page.locator("form", { has: page.locator(`input[name=merge_id][value='${state.mergeId}']`) }).first();
    await expect(form).toBeVisible();
    await form.locator("button[value=undo-merge]").click();
    await expect(page.locator(".alert").first()).toContainText(/Merge undone/);
    const loser = await dbOne("parties.Party", { pk: str(state.p2) });
    expect(str(loser.merged_into_id)).toBe("");
    const eng = await dbOne("crm.Engagement", { pk: str(state.engagementId) });
    expect(str(eng.party_id), "engagement party after the unmerge").toBe(state.p2);
    const people = await db("orders.DocumentContact", { quote: str(state.quoteId) });
    expect(people.map((p) => str(p.party_id))).toEqual([state.p1]);
    expect((await db("parties.PartyRelationship", { from_party: str(state.p2) })).length).toBe(Number(state.rel2));
    expect((await db("parties.PartyRelationship", { from_party: str(state.p1) })).length).toBe(Number(state.rel1));
  });

  step(7, "merge again, then erase the survivor: nothing personal left", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    let party = await dbOne("parties.Party", { pk: str(state.p1) });
    if (party.erased_at == null) {
      await mergeReview(ctx);
      await approveAndApply(ctx, str(state.p1), str(state.p2));
      await page.goto(`/erp/parties/${state.p1}/`);
      await page.locator("#danger-zone-title ~ * summary, section:has(#danger-zone-title) summary").first().click();
      await page.fill("#erase-confirm", str(party.display_name));
      await page.fill("#erase-reason", "Harness R8: erasure request.");
      await page.getByRole("button", { name: /Erase permanently/ }).click();
      await expect(page.locator(".alert").first()).toContainText(/Erased\./);
      party = await dbOne("parties.Party", { pk: str(state.p1) });
    }
    const rows: Record<string, unknown> = {};
    const parties = [str(state.p1), str(state.p2)];
    const customers = [str(state.c1), str(state.c2)];
    const queries: [string, Record<string, string>][] = [
      ...parties.map((pk): [string, Record<string, string>] => ["parties.Party", { pk }]),
      ...parties.map((party): [string, Record<string, string>] => ["parties.PartyEmail", { party }]),
      ...parties.map((party): [string, Record<string, string>] => ["parties.PartyPhone", { party }]),
      ...parties.map((party): [string, Record<string, string>] => ["parties.PartyAddress", { party }]),
      ...customers.map((pk): [string, Record<string, string>] => ["customers.Customer", { pk }]),
      ...parties.map((party): [string, Record<string, string>] => ["crm.Lead", { party }]),
      ["crm.Engagement", { pk: str(state.engagementId) }],
      ["crm.Task", { engagement: str(state.engagementId) }],
      ["communications.Communication", { engagement: str(state.engagementId) }],
      ["orders.Quote", { pk: str(state.quoteId) }],
      ["orders.DocumentContact", { quote: str(state.quoteId) }],
    ];
    for (const [label, filter] of queries) {
      const key = `${label} ${JSON.stringify(filter)}`;
      rows[key] = await db(label, filter);
    }
    const found = Object.entries(rows).flatMap(([label, r]) => leaks(r, NEEDLES).map((n) => `${label}: ${n}`));
    expect(found, "personal values still stored after the erasure").toEqual([]);
    const erased = await dbOne("parties.Party", { pk: str(state.p1) });
    expect(str(erased.erased_at)).not.toBe("");
    const customer = await dbOne("customers.Customer", { pk: str(state.c1) });
    expect(str(customer.email)).toMatch(TOMBSTONE);
  });

  step(8, "an erased customer shows a label in lists, on old orders and in engagement titles", async (ctx) => {
    const { page, state, dbOne } = ctx;
    const eng = await dbOne("crm.Engagement", { pk: str(state.engagementId) });
    const leaking: string[] = [];
    const check = async (what: string) => {
      const text = await bodyText(page);
      const raw = text.match(TOMBSTONE);
      const uuid = text.includes(str(state.c1)) || text.includes(str(state.c2));
      if (raw || uuid) leaking.push(`${what} (${page.url()}): ${raw ? raw[0] : "customer uuid"}`);
    };
    await clickNav(page, "Customers");
    await searchList(page, "Erased");
    await check("Customers list");
    await page.locator(`a[href='/erp/customers/${state.c1}/']`).first().click();
    await check("Customer page");
    await clickNav(page, "Quotes");
    await searchList(page, str(state.quoteNumber));
    await check("Quotes list");
    await page.locator(`a[href*='/erp/orders/quotes/${state.quoteId}/']`).first().click();
    await check("Quote editor");
    await clickNav(page, "Engagements");
    await searchList(page, str(eng.engagement_number ?? eng.number ?? ""));
    await check("Engagements list");
    await page.locator(`a[href='/erp/crm/engagements/${state.engagementId}/']`).first().click();
    await check("Engagement page");
    await clickNav(page, "Parties");
    await page.getByRole("link", { name: /Show merged and erased/ }).click();
    await check("Parties list");
    // Found 2026-10-17 (fixed the same day, customers.test_erased_display):
    // the Customers list's Email column and the customer page printed the
    // tombstone address; every screen now shows "Erased customer" / "Erased".
    expect(leaking, "screens showing the raw tombstone").toEqual([]);
  });
});
