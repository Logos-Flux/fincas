// Shared helpers for the people and setup journeys (R7, R8, R10, R11).
// A separate file on purpose: lib/harness.ts is the step runner, and R1 keeps
// private copies of its editor helpers; journeys share only what they import.
import { execFileSync } from "node:child_process";
import path from "node:path";
import { expect, type Page } from "@playwright/test";

/** The repository root: Playwright runs from storefront/ (scripts/harness/run.sh). */
export const ROOT = process.env.HARNESS_ROOT ?? path.resolve(process.cwd(), "..");
export const PYTHON = process.env.PYTHON || path.join(ROOT, ".venv", "bin", "python");
export const BASE = process.env.HARNESS_BASE_URL ?? "http://127.0.0.1:8771";
export const RUN_DIR = path.resolve(process.env.HARNESS_RUN_DIR ?? "test-results/erp-harness");

export const str = (v: unknown) => (v == null ? "" : String(v));

/**
 * Run `manage.py <args>` on the harness database: the runner exported
 * DJANGO_SETTINGS_MODULE, DATABASE_URL and the GOLDEN_HARNESS_* variables.
 * The request log is unset so a command's own log lines never land in the
 * server-error capture window. Returns stdout; throws with stderr's tail.
 */
export function manage(...args: string[]): string {
  const env = { ...process.env };
  delete env.GOLDEN_HARNESS_LOG;
  try {
    return execFileSync(PYTHON, ["manage.py", ...args], {
      cwd: ROOT,
      env,
      encoding: "utf-8",
      stdio: ["ignore", "pipe", "pipe"],
      maxBuffer: 16 * 1024 * 1024,
    });
  } catch (e) {
    const err = e as { stderr?: string; message: string };
    throw new Error(`manage.py ${args.join(" ")} failed: ${(err.stderr ?? err.message).split("\n").slice(-12).join("\n")}`);
  }
}

// ── navigation ────────────────────────────────────────────────────────────────

/** A sidebar link by its title (the icon glyph is part of the accessible name). */
export const nav = (page: Page, title: string) =>
  page.locator("a.nav-link .nav-link-title", { hasText: new RegExp(`^${title}$`) }).first();

/** Click a sidebar link, opening the collapsed sidebar first on narrow viewports. */
export async function clickNav(page: Page, title: string) {
  // An error page (a 500, Django's bare 403/404) has no sidebar: the only
  // way back is the dashboard URL, as a person would type it.
  if ((await page.locator("a.nav-link").count()) === 0) await page.goto(`${BASE}/erp/`);
  const link = nav(page, title);
  // Open the sidebar only when the toggler is showing (narrow viewport). A
  // link that is merely not rendered yet is not a reason to: it used to send
  // the desktop run to the hidden toggler and a 15 s timeout.
  const toggler = page.locator('button[aria-label="Toggle navigation"]').first();
  if (!(await link.isVisible()) && (await toggler.isVisible())) {
    await toggler.click();
    await expect(link).toBeVisible();
  }
  // Wait for the page the link opens. Without this, a second clickNav right
  // after (Accounting, then Trial Balance) looked at the page mid-load (R2.08,
  // R5.04 on GitHub-hosted runners, 2026-11-04/05).
  const href = await link.locator("xpath=ancestor::a[1]").getAttribute("href");
  await link.click();
  if (href) await page.waitForURL((u) => u.pathname === new URL(href, u).pathname);
}

/**
 * Type into a list page's search box and apply it (the search is part of
 * the screen). Observation 2026-10-17: the Engagements list opens on the
 * "Unassigned" (or "Mine") chip and its search form carries that chip as a
 * hidden input, so a number typed from the default view finds nothing when
 * the engagement is owned or closed, with no hint. The carried chip is
 * dropped here so the search means "this text, any view".
 */
export async function searchList(page: Page, text: string) {
  await page.evaluate(() => {
    for (const el of document.querySelectorAll("form input[type=hidden][name=chip]")) el.remove();
  });
  const box = page.locator("input[type=search][name=q]").first();
  await box.fill(text);
  await box.press("Enter");
  await page.waitForLoadState("domcontentloaded");
}

// ── the quote/order editor (same controls R1 drives) ──────────────────────────

/** The shipment group card at index i. */
export const card = (page: Page, i: number) =>
  page.locator(`.shipment-group-card[data-sg-index="${i}"]`);

/** Pick a variant in a line row by SKU through the row's search box. */
export async function pickSku(row: ReturnType<Page["locator"]>, sku: string) {
  const search = row.locator(".variant-search-input");
  await search.fill(sku);
  const hit = row.locator(".variant-search-results a", { hasText: sku }).first();
  await hit.waitFor({ state: "visible", timeout: 10_000 });
  await hit.click();
  await expect(row.locator("[name$='-item_name']")).not.toHaveValue("");
}

/** Add a line to group i: reuse its blank row if one exists, else Add Row. */
export async function addLine(page: Page, i: number, sku: string, qty: string) {
  const c = card(page, i);
  const rows = c.locator("tr.line-item-row");
  let row = rows.filter({ has: page.locator(".variant-search-input") }).last();
  const blank =
    (await rows.count()) > 0 && (await row.locator(".variant-search-input").inputValue()) === "";
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

// ── reading the page and the database ─────────────────────────────────────────

/** The page's visible text (what a person reads), whitespace collapsed. */
export async function bodyText(page: Page): Promise<string> {
  return (await page.evaluate(() => document.body.innerText)).replace(/\s+/g, " ");
}

/** Row count of a model through /harness/state/ (rows are capped; the count is not). */
export async function count(page: Page, label: string, filter: Record<string, string | number> = {}) {
  const qs = new URLSearchParams({ _limit: "1" });
  for (const [k, v] of Object.entries(filter)) qs.set(k, String(v));
  const res = await page.request.get(`${BASE}/harness/state/${label}/?${qs}`);
  if (!res.ok()) throw new Error(`harness state ${label}: HTTP ${res.status()} ${(await res.text()).slice(0, 200)}`);
  return ((await res.json()) as { count: number }).count;
}

/** The needles (names, addresses, digits) still present in a JSON dump of rows. */
export function leaks(rows: unknown, needles: string[]): string[] {
  const text = JSON.stringify(rows).toLowerCase();
  return needles.filter((n) => text.includes(n.toLowerCase()));
}

/** A JSON API call made from the page's session (CSRF header from the cookie). */
export async function apiJson(
  page: Page,
  csrf: string,
  method: string,
  url: string,
  body?: unknown,
): Promise<{ status: number; json: Record<string, unknown> | null; text: string }> {
  return page.evaluate(
    async ({ method, url, body, csrf }) => {
      const r = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrf },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await r.text();
      let json: Record<string, unknown> | null = null;
      try {
        json = JSON.parse(text);
      } catch {
        json = null;
      }
      return { status: r.status, json, text: text.slice(0, 500) };
    },
    { method, url, body, csrf },
  );
}

/** The local webhook receiver's listing (config/harness_views.py). */
export async function receiver(page: Page) {
  const res = await page.request.get(`${BASE}/harness/webhooks/`);
  return (await res.json()) as {
    count: number;
    repeats: Record<string, number>;
    deliveries: { delivery_id: string; event: string; received_at: string; payload: unknown }[];
  };
}
