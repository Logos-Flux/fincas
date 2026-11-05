// R7 "Import and undo" (docs/release-hardening-plan.md section 4, R7 row;
// decision 8: through the UI).
//
// The HubSpot fixture export (golden_erp/addons/hubspot_export/fixtures/crawl-small) ->
// review the proposed rows in the CRM import screens -> apply -> spot check
// (an imported person and an imported engagement exist and are reachable)
// -> undo the whole job leaves no trace (every count back to its baseline).
//
// Staging is the one step the UI cannot do: the CRM imports screen has no
// upload button on purpose (crm_import/templates job_list: "an operator
// stages an export on the server"), so step 1 stages through the CLI the
// screen names (`crm_export_adapt`, `check_canonical_export`, `crm_import
// stage`) and records the gap as a known finding. Review, apply and undo are
// the screens' own forms; `manage.py crm_import run` is never called here
// (the apply button runs crm_import.runner.run_job below WEB_RUN_LIMIT).
//
// Runs on the fresh seed only (the fixture is a second tenant's worth of
// people; docs/release-testing.md). Login: crm-admin (crm_import.apply_import).
import { journey, expect, type Ctx } from "../lib/harness";
import { ROOT, RUN_DIR, clickNav, count, manage, searchList, str } from "../lib/erp-ui";
import path from "node:path";

const FIXTURE = path.join(ROOT, "golden_erp", "addons", "hubspot_export", "fixtures", "crawl-small");
const SOURCE = "hubspot:1234567"; // the fixture's invented portal (mapping.toml)
const COMPANY = process.env.HARNESS_COMPANY ?? "0c0ffee0-4e1e-4a5e-9a11-0000000d0001";

// The outcomes the scripted reviewer picks for closed stages the pipelines
// left unhinted (crm_import/tests/test_runner.py STAGE_PICKS), and for a deal
// whose closed stage needs one.
const STAGE_PICKS: Record<string, string> = { hs_12345680: "lost", active: "won", ended: "lost" };
const DEAL_OUTCOME = "lost";

// What "no trace" is measured on: business tables an applied job writes.
const TRACE: [string, Record<string, string>][] = [
  ["parties.Party", {}],
  ["parties.PartyEmail", {}],
  ["parties.PartyPhone", {}],
  ["parties.PartyRelationship", {}],
  ["parties.PartyRole", {}],
  ["parties.ConsentRecord", {}],
  ["crm.Pipeline", {}],
  ["crm.Stage", {}],
  ["crm.Engagement", {}],
  ["crm.EngagementStageChange", {}],
  ["crm.Task", {}],
  ["communications.Communication", {}],
  ["core.UserProfile", {}],
  ["core.Team", {}],
  ["crm.EngagementContact", {}],
  ["parties.Suppression", {}],
  ["core.ExternalRef", { source: SOURCE }],
];

async function snapshot(ctx: Ctx): Promise<Record<string, number>> {
  const out: Record<string, number> = {};
  for (const [label, filter] of TRACE) out[label] = await count(ctx.page, label, filter);
  return out;
}

async function openJob(ctx: Ctx) {
  const { page, state } = ctx;
  // Step 1 is a known finding, so any error in it (staging included) is
  // recorded as that finding; without a job id every later step would only
  // time out on a link to /erp/imports/crm/undefined/.
  if (!state.jobId) throw new Error("no staged import job: R7.01 failed to stage the fixture (see its verdict)");
  await clickNav(page, "CRM Imports");
  await page.locator(`a[href='/erp/imports/crm/${state.jobId}/']`).first().click();
  await expect(page).toHaveURL(new RegExp(`/erp/imports/crm/${state.jobId}/$`));
}

journey("R7", "Import and undo", (step) => {
  step(1, "stage the HubSpot fixture export from the CRM imports screen", async (ctx) => {
    const { page, state, db, login } = ctx;
    await login("crm-admin");
    await page.goto("/erp/");
    await clickNav(page, "CRM Imports");
    await expect(page).toHaveURL(/\/erp\/imports\/crm\/$/);
    // A rerun on the same database finds the staged job instead of staging twice.
    let jobs = await db("crm_import.ImportJob", { source: SOURCE, company: COMPANY, _order: "-created_at" });
    if (jobs.length === 0) {
      const canonical = path.join(RUN_DIR, "fixtures", "hubspot-canonical");
      manage("crm_export_adapt", "--provider", "hubspot", "--input", FIXTURE, "--output", canonical,
        "--mapping", path.join(FIXTURE, "mapping.toml"));
      manage("check_canonical_export", canonical);
      manage("crm_import", "stage", "--company", COMPANY, "--dir", canonical,
        "--mapping", path.join(FIXTURE, "mapping.toml"));
      jobs = await db("crm_import.ImportJob", { source: SOURCE, company: COMPANY, _order: "-created_at" });
    }
    expect(jobs.length).toBeGreaterThanOrEqual(1);
    state.jobId = str(jobs[0].id);
    expect(str(jobs[0].status)).toBe("reviewing");
    await page.reload();
    await expect(page.locator(`a[href='/erp/imports/crm/${state.jobId}/']`).first()).toBeVisible();
    // Found 2026-10-17: the screen has no upload (by design: exports hold
    // personal data and are staged on the server). The plan asks for the
    // whole journey through the UI; recorded as a known finding so the gap
    // stays visible rather than silently scripted around.
    const main = page.locator("main.main-content, main, .container-fluid").first();
    await expect(
      main
        .getByRole("link", { name: /upload|new import|stage an export/i })
        .or(main.getByRole("button", { name: /upload|new import/i }))
        .or(main.locator("input[type=file]"))
        .first(),
    ).toBeVisible({ timeout: 2000 });
  }, { knownFinding: "R7-no-upload-ui" });

  step(2, "review: decide every proposed row from the job's queues", async (ctx) => {
    const { page, state } = ctx;
    await openJob(ctx);
    // The baseline "no trace" is measured against: taken before anything is
    // applied (a rerun after an apply keeps the baseline it took first).
    if (!state.baseline || (await count(page, "core.ExternalRef", { source: SOURCE })) === 0) {
      state.baseline = JSON.stringify(await snapshot(ctx));
    }
    for (let round = 0; round < 12; round++) {
      const review = page.getByRole("link", { name: /^Review \d+/ }).first();
      if (!(await review.isVisible().catch(() => false))) break;
      await review.click();
      await expect(page).toHaveURL(new RegExp(`/erp/imports/crm/${state.jobId}/[a-z_]+/`));
      for (let row = 0; row < 40; row++) {
        // Each undecided row is a collapsed <details>; open the first one.
        const item = page.locator("li.crm-review-row[data-decidable='1'][data-state='proposed']").first();
        if ((await item.count()) === 0) break;
        const details = item.locator("details.crm-row-details").first();
        if (!(await details.evaluate((d) => (d as HTMLDetailsElement).open))) {
          await details.locator("summary.crm-row-summary").click();
        }
        const form = item.locator("form.crm-decide-form").first();
        if ((await form.count()) === 0) break; // listed, but not decidable here (gated)
        await expect(form).toBeVisible();
        // A pipeline row: outcome per closed stage; a deal row: its outcome.
        const stageSelects = form.locator("select[name^='stage_outcome:']");
        for (let i = 0; i < (await stageSelects.count()); i++) {
          const sel = stageSelects.nth(i);
          const key = (await sel.getAttribute("name"))!.split(":")[1];
          await sel.selectOption(STAGE_PICKS[key] ?? "lost");
        }
        const outcome = form.locator("select[name=outcome]");
        if (await outcome.count()) await outcome.selectOption(DEAL_OUTCOME);
        // The reviewer's rule (crm_import/tests/test_runner.py review()):
        // approve the proposal; a row with no proposal becomes a new record;
        // an ambiguous row takes its first candidate.
        const decide = form.locator("button[value=approve], button[value=create], button[name=pick]").first();
        await expect(decide).toBeVisible();
        const posted = page.waitForResponse((r) => r.request().method() === "POST" && r.url().includes("/decide/"));
        await decide.click();
        await posted;
        await page.waitForLoadState("domcontentloaded");
      }
      await page.getByRole("link", { name: /Job overview/ }).first().click();
      await expect(page).toHaveURL(new RegExp(`/erp/imports/crm/${state.jobId}/$`));
    }
    await expect(page.getByRole("link", { name: /^Review \d+/ })).toHaveCount(0);
    expect(await count(page, "unify.StagedRow", { batch__company: COMPANY, mapping_state: "proposed" })).toBe(0);
  });

  step(3, "apply every step from the overview, in step order", async (ctx) => {
    const { page, state, dbOne } = ctx;
    await openJob(ctx);
    // A step's "Apply N" appears once the steps it waits for are applied;
    // click the first one offered until none is left.
    const applied: string[] = [];
    for (let round = 0; round < 12; round++) {
      const form = page.locator("form").filter({ has: page.locator("button[value=apply]") }).first();
      if ((await form.count()) === 0) break;
      const otype = await form.locator("input[name=object_type]").inputValue();
      await form.locator("button[value=apply]").click();
      await expect(page).toHaveURL(new RegExp(`/erp/imports/crm/${state.jobId}/$`));
      await expect(page.locator(".alert").first()).toContainText(/Applied \d+ row/);
      applied.push(otype);
    }
    state.appliedSteps = applied.join(",");
    const job = await dbOne("crm_import.ImportJob", { pk: str(state.jobId) });
    // A rerun on the same database finds the job applied already.
    if (str(job.status) !== "applied") expect(applied.length, "steps applied from the overview").toBeGreaterThan(0);
    expect(str(job.status), `job status after applying ${applied.join(", ")}`).toBe("applied");
    expect(await count(page, "core.ExternalRef", { source: SOURCE })).toBeGreaterThan(0);
  });

  step(4, "spot check: an imported person and an imported engagement are reachable", async (ctx) => {
    const { page, db, dbOne } = ctx;
    // Refs of type "contact" also cover consent and suppression rows; take
    // the first that resolves to a Party. "deal" refs are engagements.
    const refs = await db("core.ExternalRef", { source: SOURCE, _limit: 200 });
    let party: Record<string, unknown> | undefined;
    for (const ref of refs.filter((r) => str(r.object_type) === "contact")) {
      const rows = await db("parties.Party", { pk: str(ref.object_id) });
      if (rows.length) {
        party = rows[0];
        break;
      }
    }
    expect(party, "an imported person").toBeTruthy();
    const deal = refs.find((r) => str(r.object_type) === "deal");
    expect(deal, "an ExternalRef for a deal").toBeTruthy();
    const engagement = await dbOne("crm.Engagement", { pk: str(deal!.object_id) });
    await clickNav(page, "Parties");
    await searchList(page, str(party!.display_name));
    await page.locator(`a[href='/erp/parties/${party!.id}/']`).first().click();
    await expect(page).toHaveURL(new RegExp(`/erp/parties/${party!.id}/`));
    await expect(page.locator("#identifiers-title ~ *, section:has(#identifiers-title)").first()).toContainText(SOURCE.split(":")[0]);
    await clickNav(page, "Engagements");
    await searchList(page, str(engagement.engagement_number ?? engagement.number ?? ""));
    await page.locator(`a[href='/erp/crm/engagements/${engagement.id}/']`).first().click();
    await expect(page).toHaveURL(new RegExp(`/erp/crm/engagements/${engagement.id}/`));
  });

  step(5, "undo the whole job: every count back to its baseline", async (ctx) => {
    const { page, state, dbOne } = ctx;
    await openJob(ctx);
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: /Undo the whole job/ }).click();
    await expect(page).toHaveURL(new RegExp(`/erp/imports/crm/${state.jobId}/$`));
    await expect(page.locator(".alert").first()).toContainText(/Undid \d+ row/);
    const job = await dbOne("crm_import.ImportJob", { pk: str(state.jobId) });
    expect(str(job.status), "job status after the undo").toBe("undone");
    const base = JSON.parse(str(state.baseline)) as Record<string, number>;
    const after = await snapshot(ctx);
    const drift = Object.entries(after).filter(([k, v]) => k in base && v !== base[k]).map(([k, v]) => `${k}: ${base[k]} -> ${v}`);
    expect(drift, "rows left behind by the undo").toEqual([]);
  });
});
