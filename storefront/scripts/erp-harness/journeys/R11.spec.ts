// R11 "Returning user" (docs/release-hardening-plan.md section 4, R11 row and
// the design block's returning-user bullet): on a scrubbed-copy clone, from a
// legacy quote (one whose engagement was reconstructed by the backfill, not
// opened by a person), reach its engagement, history and tasks by clicking
// only; the reconstructed history is disclosed (the engagement's history
// marker, the stage report's footnote).
//
// Runs on `make harness-synth` (a clone of golden_synth with
// seed_release_test --overlay); on a fresh seed every engagement was opened
// through the UI or the seed, so the journey defines no steps there.
// Evidence from the clone is ids and shapes only. Login: staff.
import { journey, expect } from "../lib/harness";
import { clickNav, searchList, str } from "../lib/erp-ui";

// Synth mode: the runner names a clone golden_harness_a_synth_<stamp>
// (scripts/harness/run.sh --synth); HARNESS_SYNTH=1 forces it for a reused
// database. On a fresh seed the journey defines no steps.
const SYNTH =
  process.env.HARNESS_SYNTH === "1" || /synth/i.test(process.env.HARNESS_DB ?? "");

journey("R11", "Returning user", (step) => {
  if (!SYNTH) {
    console.log("R11 runs on a scrubbed-copy clone only (make harness-synth JOURNEY=R11): no steps on this database.");
    return;
  }
  step(1, "find a legacy quote: its engagement's history was reconstructed", async (ctx) => {
    const { page, state, db, dbOne, login } = ctx;
    await login("staff");
    await page.goto("/erp/");
    // The backfill writes source=backfill rows (crm.backfill). The clone's
    // reconstructed rows may instead carry source=system with no actor and a
    // "Quote <number>" note (the ensure_for_quote path); record which.
    let changes = await db("crm.EngagementStageChange", { source: "backfill", _order: "changed_at", _limit: 50 });
    state.historySource = "backfill";
    if (changes.length === 0) {
      changes = (
        await db("crm.EngagementStageChange", {
          source: "system", note__startswith: "Quote ", _order: "changed_at", _limit: 200,
        })
      ).filter((c) => c.changed_by_id == null);
      state.historySource = "system (no actor, 'Quote <number>' note)";
    }
    expect(changes.length, "reconstructed history rows on this database").toBeGreaterThan(0);
    let quote: Record<string, unknown> | undefined;
    for (const change of changes) {
      const quotes = await db("orders.Quote", { engagement: str(change.engagement_id), _order: "created_at" });
      if (quotes.length) {
        quote = quotes[0];
        state.engagementId = str(change.engagement_id);
        break;
      }
    }
    expect(quote, "a quote on a reconstructed engagement").toBeTruthy();
    state.quoteId = str(quote!.id);
    state.quoteNumber = str(quote!.quote_number);
    const eng = await dbOne("crm.Engagement", { pk: str(state.engagementId) });
    state.engagementNumber = str(eng.engagement_number ?? eng.number ?? "");
    state.historyRows = (await db("crm.EngagementStageChange", { engagement: str(state.engagementId) })).length;
    state.reconstructedRows = (
      await db("crm.EngagementStageChange", { engagement: str(state.engagementId) })
    ).filter((c) => c.changed_by_id == null).length;
    state.taskRows = (await db("crm.Task", { engagement: str(state.engagementId) })).length;
  });
  // Found 2026-10-17 on the clone (fixed the same day): the dashboard's
  // warning alerts failed axe colour contrast (an Argon-era text-white on
  // Tabler's tinted alert); a fresh seed shows no alert there.

  step(2, "Quotes list, the quote, then its engagement by clicking", async (ctx) => {
    const { page, state } = ctx;
    await clickNav(page, "Quotes");
    await searchList(page, str(state.quoteNumber));
    const row = page.locator("tr", { hasText: str(state.quoteNumber) }).first();
    await expect(row).toBeVisible();
    await row.locator(`a[href*='/erp/orders/quotes/${state.quoteId}/']`).first().click();
    await expect(page).toHaveURL(new RegExp(`/erp/orders/quotes/${state.quoteId}/`));
    // The editor links its engagement (the deal panel); else the list's column does.
    const fromEditor = page.locator(`a[href*='/erp/crm/engagements/${state.engagementId}/']`).first();
    if (await fromEditor.isVisible().catch(() => false)) {
      await fromEditor.click();
    } else {
      await clickNav(page, "Quotes");
      await searchList(page, str(state.quoteNumber));
      await page.locator("tr", { hasText: str(state.quoteNumber) }).first()
        .locator(`a[href*='/erp/crm/engagements/${state.engagementId}/']`).first().click();
    }
    await expect(page).toHaveURL(new RegExp(`/erp/crm/engagements/${state.engagementId}/`));
    await expect(page.locator("#documents")).toContainText(str(state.quoteNumber));
  });

  step(3, "history: every change listed, the reconstructed rows marked as such", async (ctx) => {
    const { page, state } = ctx;
    const history = page.locator("section[aria-labelledby='history-title']");
    await expect(history).toBeVisible();
    await history.locator("summary").click();
    await expect(history.locator("summary")).toContainText(new RegExp(`${state.historyRows} change`));
    await expect(history.locator("li")).toHaveCount(Number(state.historyRows));
    // Found 2026-10-17 (fixed the same day, crm.tests.test_reconstructed_history):
    // the list never showed a row's source; backfill and import rows now
    // carry a "reconstructed" badge.
    await expect(history.locator("li", { hasText: /backfill|reconstructed|import/i }).first()).toBeVisible({ timeout: 2000 });
  });

  step(4, "tasks: the engagement's next action, then the Tasks list", async (ctx) => {
    const { page, state } = ctx;
    const next = page.locator("#next-action");
    await expect(next).toBeVisible();
    if (Number(state.taskRows) > 0) {
      await expect(next).not.toContainText(/No open tasks/);
    }
    await clickNav(page, "Tasks");
    await expect(page).toHaveURL(/\/erp\/crm\/tasks\/$/);
    await expect(page.locator("h1").first()).toBeVisible();
  });

  step(5, "the stage report discloses reconstructed history", async (ctx) => {
    const { page, state } = ctx;
    await clickNav(page, "Stage Duration");
    await expect(page).toHaveURL(/\/erp\/reports\/crm\/stages\//);
    // The report footnotes engagements left out (no observed history) and
    // visits that began or ended at a backfill or import row. Found
    // 2026-10-17 on the clone (fixed the same day): its reconstructed rows
    // were source=system, which the report does not count. A lazy ensure now
    // writes backfill, and the runner's reconcile_engagements --fix after
    // migrate relabels the clone's old rows (step 1 records the source).
    const footnote = page.locator("[data-testid='no-history-footnote']");
    await expect(footnote, `history source on this database: ${state.historySource}`).toBeVisible({ timeout: 2000 });
    await expect(footnote).toContainText(/left out|not timed/);
  });
});
