// R2 "Losing and reopening" (docs/release-hardening-plan.md section 4, R2 row).
//
// quote -> its engagement lost with a reason -> reopen without permission
// (the action is hidden; the direct URL is refused with the styled 403) and
// with permission (sales-manager holds crm.reopen_engagement) -> revision
// -> won; the funnel report counts the deal once.
//
// Every screen is reached by clicking (the dashboard after sign-in is the
// only typed URL), the database is asserted through /harness/state/, and
// the runner adds the page, axe, 360 px and server-log checks
// (lib/harness.ts). Product gaps carry knownFinding (docs/release-testing.md).
import { journey, expect, type Ctx } from "../lib/harness";
import {
  alerts,
  addLine,
  clickNav,
  expectMoney,
  pickCustomer,
  postFromPage,
  str,
} from "../lib/erp";

const COMPANY =
  process.env.HARNESS_COMPANY ?? "0c0ffee0-4e1e-4a5e-9a11-0000000d0001";

/** Quotes list -> the quote's Engagement column -> the engagement page. */
async function openEngagementFromQuotes(ctx: Ctx) {
  const { page, state } = ctx;
  await clickNav(page, "Quotes");
  const row = page.locator("tr", { hasText: str(state.quoteNumber) }).first();
  await row.locator("a[href*='/erp/crm/engagements/']").first().click();
  await expect(page).toHaveURL(new RegExp(`/erp/crm/engagements/${state.engagementId}/`));
}

async function openQuoteEditor(ctx: Ctx) {
  const { page, state } = ctx;
  await clickNav(page, "Quotes");
  // exact: the Engagement column's link names the quote too ("Quote QT-n ...").
  await page.getByRole("link", { name: str(state.quoteNumber), exact: true }).first().click();
  await expect(page).toHaveURL(new RegExp(`/erp/orders/quotes/${state.quoteId}/edit/`));
}

journey("R2", "Losing and reopening", (step) => {
  step(1, "a quote for an existing customer opens its own engagement", async (ctx) => {
    const { page, state, db, dbOne, login } = ctx;
    await login("staff");
    await page.goto("/erp/");
    await clickNav(page, "Quotes");
    await page.getByRole("link", { name: /^New/ }).first().click();
    await expect(page).toHaveURL(/\/erp\/orders\/quotes\/new\//);
    await pickCustomer(page, /Acme/);
    await addLine(page, 0, "ACT-500W", "3");
    await page.locator("button[name='_action'][value='save_continue']").click();
    await expect(page).toHaveURL(/\/erp\/orders\/quotes\/[0-9a-f-]+\/edit\//);
    const quotePk = page.url().match(/\/quotes\/([0-9a-f-]+)\/edit\//)?.[1] ?? "";
    const quote = await dbOne("orders.Quote", { pk: quotePk });
    state.quoteId = str(quote.id);
    state.quoteNumber = str(quote.quote_number);
    state.quoteTotal = str(quote.grand_total);
    state.engagementId = str(quote.engagement_id);
    expect(state.engagementId).not.toBe("");
    const eng = await dbOne("crm.Engagement", { pk: state.engagementId });
    expect(str(eng.status)).toBe("open");
    expect(str(eng.origin)).toBe("quote");
    state.stageBeforeLoss = str(eng.stage_id);
    const lines = await db("orders.QuoteLineItem", { quote: state.quoteId });
    expect(lines.length).toBe(1);
  });

  step(2, "lose the deal from its engagement, with a reason", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    await openEngagementFromQuotes(ctx);
    // The documents card lists the quote with money per docs/formatting.md.
    const doc = page.locator("#documents li", { hasText: str(state.quoteNumber) });
    await expect(doc).toBeVisible();
    expectMoney((await doc.innerText()).split("·").pop() ?? "", "quote total on the engagement");
    const before = await dbOne("crm.Engagement", { pk: str(state.engagementId) });
    if (str(before.status) === "open") {
      // A rerun on a kept database finds it lost already (previous attempt).
      await page.getByRole("link", { name: /^Lose$/ }).click();
      await expect(page).toHaveURL(/confirm=lost/);
      const close = page.locator("#close");
      await expect(close).toHaveAttribute("open", "");
      await close.locator("select[name='loss_reason']").selectOption("price");
      await close.locator("input[name='loss_competitor']").fill("Rival Actuation Co");
      await close.locator("[name='loss_notes']").fill("Came in 12% under on the actuators.");
      await close.getByRole("button", { name: /Mark as lost/ }).click();
      await expect(page.locator(".alert, [role=alert]").first()).toContainText(/Marked as lost/);
    }
    const eng = await dbOne("crm.Engagement", { pk: str(state.engagementId) });
    expect(str(eng.status)).toBe("lost");
    expect(str(eng.loss_reason)).toBe("price");
    expect(str(eng.loss_competitor)).toBe("Rival Actuation Co");
    expect(str(eng.closed_at)).not.toBe("");
    const lost = await db("crm.EngagementStageChange", { engagement: str(state.engagementId), to_status: "lost" });
    expect(lost.length).toBe(1);
    await expect(page.locator(".badge", { hasText: /Lost: Price/ })).toBeVisible();
    // Observation 2026-10-17: losing the engagement leaves the quote's own
    // status as it was (draft); nothing marks the quote lost. Recorded.
    const quote = await dbOne("orders.Quote", { pk: str(state.quoteId) });
    state.quoteStatusAfterLoss = str(quote.status);
  });

  step(3, "sales (no reopen permission) sees no Reopen action", async (ctx) => {
    const { page, state, login } = ctx;
    await login("sales");
    await page.goto("/erp/");
    await openEngagementFromQuotes(ctx);
    await expect(page.locator(".badge", { hasText: /Lost: Price/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Reopen$/ })).toHaveCount(0);
    await expect(page.locator(`form[action$='/engagements/${state.engagementId}/reopen/']`)).toHaveCount(0);
  });

  step(4, "a direct reopen by sales gets the styled 403 and changes nothing", async (ctx) => {
    const { page, state, dbOne } = ctx;
    // Found 2026-10-17 (fixed the same day): _EngagementAction caught
    // PermissionDenied, flashed and redirected (302). An authorization
    // refusal is now the styled 403 page (decision 10; PR #131's page).
    const res = await postFromPage(page, `/erp/crm/engagements/${state.engagementId}/reopen/`, { stage: "" });
    const eng = await dbOne("crm.Engagement", { pk: str(state.engagementId) });
    expect(str(eng.status), "the engagement after a refused reopen").toBe("lost");
    state.directReopenStatus = res.status;
    // The flash a redirect carries shows on the next page render.
    await page.reload();
    const flash = (await alerts(page)).join(" | ");
    expect(res.status, `direct reopen answered ${res.status} -> ${res.location} (flash: ${flash})`).toBe(403);
    expect(res.body, "a styled 403 (not Django's bare Forbidden) with sign-out and back links").toMatch(/sign out|log out/i);
    expect(res.body).toMatch(/back/i);
    expect(res.body).not.toMatch(/<h1>403 Forbidden<\/h1>/);
  }, { axe: false, fit360: false });

  step(5, "sales-manager reopens the deal where it was", async (ctx) => {
    const { page, state, db, dbOne, login } = ctx;
    await login("sales-manager");
    await page.goto("/erp/");
    await openEngagementFromQuotes(ctx);
    const form = page.locator(`form[action$='/engagements/${state.engagementId}/reopen/']`);
    await expect(form).toBeVisible();
    await form.locator("select[name='stage']").selectOption("");
    await form.getByRole("button", { name: /^Reopen$/ }).click();
    await expect(page.locator(".alert, [role=alert]").first()).toContainText(/Reopened/);
    const eng = await dbOne("crm.Engagement", { pk: str(state.engagementId) });
    expect(str(eng.status)).toBe("open");
    expect(str(eng.loss_reason)).toBe("");
    expect(str(eng.loss_competitor)).toBe("");
    expect(str(eng.closed_at)).toBe("");
    expect(str(eng.stage_id), "reopened at the stage it was lost from").toBe(state.stageBeforeLoss);
    const reopened = await db("crm.EngagementStageChange", {
      engagement: str(state.engagementId), from_status: "lost", to_status: "open",
    });
    expect(reopened.length).toBe(1);
  });

  step(6, "a revision of the quote stays on the deal", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    // Found 2026-10-17 (fixed the same day): the quote editor had no
    // "New revision" action although the model and API carried revisions.
    // The editor now posts orders.revisions.revise_quote (a confirm first).
    await openQuoteEditor(ctx);
    const control = page.getByRole("button", { name: /^New revision/i });
    await expect(control.first()).toBeVisible({ timeout: 3000 });
    page.once("dialog", (d) => d.accept());
    await control.first().click();
    await expect(page).toHaveURL(/\/erp\/orders\/quotes\/[0-9a-f-]+\/edit\//);
    const revisionPk = page.url().match(/\/quotes\/([0-9a-f-]+)\/edit\//)?.[1] ?? "";
    expect(revisionPk).not.toBe(state.quoteId);
    const revision = await dbOne("orders.Quote", { pk: revisionPk });
    expect(str(revision.original_quote_id)).toBe(state.quoteId);
    expect(str(revision.engagement_id)).toBe(state.engagementId);
    state.revisionId = str(revision.id);
    const original = await dbOne("orders.Quote", { pk: str(state.quoteId) });
    expect(str(original.superseded_by_id)).toBe(revisionPk);
    expect((await db("orders.Quote", { engagement: str(state.engagementId) })).length).toBe(2);
    expect(str(original.status), "the source quote is superseded").toBe("superseded");
  });

  step(7, "win the deal; the documents card keeps the quote with its status", async (ctx) => {
    const { page, state, db, dbOne } = ctx;
    await openEngagementFromQuotes(ctx);
    await page.locator("form[action$='/win/']").first().getByRole("button", { name: /^Win$/ }).click();
    await expect(page.locator(".alert, [role=alert]").first()).toContainText(/Marked as won/);
    const eng = await dbOne("crm.Engagement", { pk: str(state.engagementId) });
    expect(str(eng.status)).toBe("won");
    expect(str(eng.loss_reason)).toBe("");
    const won = await db("crm.EngagementStageChange", { engagement: str(state.engagementId), to_status: "won" });
    expect(won.length).toBe(1);
    const head = state.revisionId ? await dbOne("orders.Quote", { pk: str(state.revisionId) }) : await dbOne("orders.Quote", { pk: str(state.quoteId) });
    const doc = page.locator("#documents li", { hasText: str(head.quote_number) });
    await expect(doc).toBeVisible();
    // The quote's own status is shown, not the deal's.
    const statusLabel = { draft: "Draft", sent: "Sent", accepted: "Accepted", lost: "Lost" }[str(head.status)] ?? str(head.status);
    await expect(doc).toContainText(statusLabel);
  });

  step(8, "the funnel report counts the deal once", async (ctx) => {
    const { page, state, db } = ctx;
    await clickNav(page, "Reports");
    await clickNav(page, "Funnel by Source");
    await expect(page).toHaveURL(/\/erp\/reports\/crm\/funnel\//);
    // Narrow the cohort to engagements created today (this run's), through
    // the page's own period control.
    await page.locator("select[name='period']").selectOption("today");
    await expect(page).toHaveURL(/period=today/);
    const from = await page.locator("input[name='date_from']").inputValue();
    const to = await page.locator("input[name='date_to']").inputValue();
    const table = page.locator("[data-testid=report-table]");
    const headers = (await table.locator("thead th").allInnerTexts()).map((h) => h.trim().toLowerCase());
    const col = (re: RegExp) => {
      const i = headers.findIndex((h) => re.test(h));
      expect(i, `column ${re} in ${headers.join(" | ")}`).toBeGreaterThanOrEqual(0);
      return i;
    };
    const total = table.locator("tfoot tr").first();
    await expect(total).toBeVisible();
    const cells = (await total.locator("td").allInnerTexts()).map((c) => c.trim());
    const n = (re: RegExp) => Number(cells[col(re)].replace(/[^\d]/g, "") || 0);
    const created = n(/created/), won = n(/^won$/), lost = n(/^lost$/), open = n(/^open$/);
    const notCounted = n(/not counted|disqualified|apart/);
    // Each engagement sits in exactly one outcome column.
    expect(created, `created ${created} = won ${won} + lost ${lost} + not counted ${notCounted} + open ${open}`)
      .toBe(won + lost + notCounted + open);
    // And the totals are the database's: one row per engagement created in the period.
    const cohort = await db("crm.Engagement", {
      company: COMPANY, created_at__date__gte: from, created_at__date__lte: to,
    });
    const by = (s: string) => cohort.filter((e) => str(e.status) === s).length;
    expect(created).toBe(cohort.length);
    expect(won).toBe(by("won"));
    expect(lost + notCounted).toBe(by("lost"));
    expect(open).toBe(by("open"));
    expect(cohort.map((e) => str(e.id))).toContain(state.engagementId);
    // The deal that was lost, reopened and won is won once and lost nowhere.
    const history = await db("crm.EngagementStageChange", { engagement: str(state.engagementId) });
    expect(history.filter((h) => str(h.to_status) === "lost").length).toBe(1);
    expect(history.filter((h) => str(h.to_status) === "won").length).toBe(1);
  });
});
