// Back-office helpers shared by the journeys (docs/release-testing.md,
// "Adding a journey"): the sidebar, the document editor's line rows and
// shipment cards, a double submit, and the money/date shapes of
// docs/formatting.md. R1.spec.ts carries its own copies (it predates this
// file); new journeys import from here.
import type { Locator, Page } from "@playwright/test";
import { expect } from "./harness";

export const str = (v: unknown) => (v == null ? "" : String(v));
export const num = (v: unknown) => Number(v ?? 0);

// ── formatting (docs/formatting.md 1.1 and 2.2) ───────────────────────────────

/** `$1,234.50`, `($1,234.50)`, `CAD 1,234.50`, or the em dash. */
export const MONEY_RE =
  /^(—|\(?\$\d{1,3}(,\d{3})*\.\d{2}\)?|[A-Z]{3} \d{1,3}(,\d{3})*\.\d{2})$/;
/** `Oct 7, 2026` (`M j, Y`, the de facto default). */
export const DATE_MJY_RE = /^[A-Z][a-z]{2} \d{1,2}, \d{4}$/;
/** `2026-10-07` (`Y-m-d`, accounting screens). */
export const DATE_ISO_RE = /^\d{4}-\d{2}-\d{2}$/;

export function expectMoney(text: string, what = "amount") {
  expect(text.trim(), `${what} is formatted per docs/formatting.md 1.1`).toMatch(MONEY_RE);
}

/** Parse `$1,234.50` / `($1,234.50)` to a number. */
export function moneyValue(text: string): number {
  const t = text.trim();
  const negative = t.startsWith("(");
  const n = Number(t.replace(/[^\d.]/g, ""));
  return negative ? -n : n;
}

// ── sidebar ───────────────────────────────────────────────────────────────────

/** A sidebar link by its title (the icon glyph is part of the accessible name). */
export const nav = (page: Page, title: string) =>
  page.locator("a.nav-link .nav-link-title", { hasText: new RegExp(`^${title}$`) }).first();

/** Click a sidebar link, opening the collapsed sidebar first on narrow viewports. */
export async function clickNav(page: Page, title: string) {
  const link = nav(page, title);
  if (!(await link.isVisible())) {
    await page.locator('button[aria-label="Toggle navigation"]').first().click();
    await expect(link).toBeVisible();
  }
  await link.click();
}

// ── document editor ───────────────────────────────────────────────────────────

/** Pick a variant in a line row by SKU through the row's search box. */
export async function pickSku(row: Locator, sku: string) {
  const search = row.locator(".variant-search-input");
  await search.fill(sku);
  const hit = row.locator(".variant-search-results a", { hasText: sku }).first();
  await hit.waitFor({ state: "visible", timeout: 10_000 });
  await hit.click();
  await expect(row.locator("[name$='-item_name']")).not.toHaveValue("");
}

/** The shipment group card at index i. */
export const card = (page: Page, i: number) =>
  page.locator(`.shipment-group-card[data-sg-index="${i}"]`);

/** Add a line to group i: reuse its blank row if one exists, else Add Row. */
export async function addLine(page: Page, i: number, sku: string, qty: string) {
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

/** Pick an existing customer in the editor's customer search widget. */
export async function pickCustomer(page: Page, text: string | RegExp) {
  const widget = page.locator(".ajax-search-widget").filter({ has: page.locator("input") }).first();
  await widget.locator("input").fill(typeof text === "string" ? text : text.source);
  await widget.locator("a.list-group-item", { hasText: text }).first().click();
}

/** Two concurrent POSTs from the page: what a double-click submits. */
export async function doubleSubmit(
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

/**
 * One POST with the browser's session and CSRF cookie, not following
 * redirects: what typing the action's URL (or replaying a hidden form) gets.
 * Returns the status, the Location header and the start of the body.
 */
export async function postFromPage(page: Page, url: string, fields: Record<string, string> = {}) {
  const cookies = await page.context().cookies();
  const csrf = cookies.find((c) => c.name === "csrftoken")?.value ?? "";
  const r = await page.request.post(url, {
    form: { ...fields, csrfmiddlewaretoken: csrf },
    headers: { "X-CSRFToken": csrf, Referer: page.url() },
    maxRedirects: 0,
  });
  return {
    status: r.status(),
    location: r.headers()["location"] ?? "",
    body: (await r.text()).slice(0, 4000),
  };
}

/** The visible alert / toast text on the page (empty when none). */
export async function alerts(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const sel = "[role=alert], .alert, .invalid-feedback, .errorlist, .toast-body, .messages li";
    const out: string[] = [];
    for (const el of document.querySelectorAll(sel)) {
      const h = el as HTMLElement;
      if (!h.offsetParent && getComputedStyle(h).position !== "fixed") continue;
      const text = (h.innerText || "").replace(/\s+/g, " ").trim();
      if (text) out.push(text);
    }
    return out;
  });
}
