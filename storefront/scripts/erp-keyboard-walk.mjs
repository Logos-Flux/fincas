// ERP keyboard walk-through: Tab through back-office pages and record what
// a keyboard-only user reaches (docs/crm/phase4-fields.md 5.7).
//
// For each page it presses Tab until focus wraps (or a cap), and records the
// focused element's role, accessible name and whether a focus indicator is
// visible (outline or box-shadow differs from the unfocused state). It then
// lists interactive elements that are visible but never received focus
// ("unreachable"), and focus stops with no visible indicator. Markdown goes to
// stdout and SWEEP_OUT/keyboard-walk.md; exit 1 when anything is unreachable
// or unindicated.
//
// Run from storefront/ against a running Django dev server:
//   SWEEP_USER=… SWEEP_PASS=… WALK_PAGES=/erp/parties/,/erp/crm/tasks/ \
//     node scripts/erp-keyboard-walk.mjs
// Env: ERP_URL (default http://127.0.0.1:8000), WALK_PAGES (comma list of
// paths, required), WALK_WIDTH (default 1440), WALK_MAX_TABS (default 400).
import { chromium } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { loginStaff } from "./lib/erp-checks.mjs";

const BASE = process.env.ERP_URL ?? "http://127.0.0.1:8000";
const OUT = process.env.SWEEP_OUT ?? "test-results/erp-sweep";
const { SWEEP_USER, SWEEP_PASS } = process.env;
const PAGES = (process.env.WALK_PAGES ?? "").split(",").filter(Boolean);
const WIDTH = Number(process.env.WALK_WIDTH ?? 1440);
const MAX_TABS = Number(process.env.WALK_MAX_TABS ?? 400);
if (!SWEEP_USER || !SWEEP_PASS)
  throw new Error("set SWEEP_USER and SWEEP_PASS");
if (!PAGES.length) throw new Error("set WALK_PAGES");

fs.mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: WIDTH, height: 900 },
});
const page = await context.newPage();
await loginStaff(page, BASE, SWEEP_USER, SWEEP_PASS);

// Mark every visible, enabled, tabbable control with a walk id.
const MARK = () => {
  const sel = "a[href], button, input, select, textarea, summary, [tabindex]";
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const hidden =
      cs.visibility === "hidden" ||
      cs.display === "none" ||
      (!r.width && !r.height);
    const skip =
      el.disabled ||
      el.type === "hidden" ||
      el.tabIndex < 0 ||
      hidden ||
      el.closest("[inert], [hidden], details:not([open]) > :not(summary)");
    if (!skip) el.dataset.walk = String(++n);
  }
  return n;
};
const DESCRIBE = () => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const name = (
    el.getAttribute("aria-label") ||
    el.labels?.[0]?.innerText ||
    el.innerText ||
    el.value ||
    el.getAttribute("title") ||
    el.name ||
    ""
  )
    .trim()
    .replace(/\s+/g, " ");
  const role =
    el.getAttribute("role") ||
    el.tagName.toLowerCase() +
      (el.type && el.tagName === "INPUT" ? `[${el.type}]` : "");
  const focused = getComputedStyle(el);
  const ring =
    (focused.outlineStyle !== "none" && parseFloat(focused.outlineWidth) > 0) ||
    (focused.boxShadow && focused.boxShadow !== "none");
  return {
    id: el.dataset.walk ?? "",
    role,
    name: name.slice(0, 60),
    ring: !!ring,
    where:
      (el.id ? "#" + el.id : "") +
      [...el.classList]
        .slice(0, 2)
        .map((c) => "." + c)
        .join(""),
    inMain: !!el.closest("main, .main-content, #content, [role=main]"),
  };
};

const report = [];
let failures = 0;
for (const p of PAGES) {
  await page.goto(BASE + p, { waitUntil: "networkidle" });
  const total = await page.evaluate(MARK);
  await page.evaluate(() => document.activeElement?.blur());
  const seen = new Set();
  const stops = [];
  for (let i = 0; i < MAX_TABS; i++) {
    await page.keyboard.press("Tab");
    const d = await page.evaluate(DESCRIBE);
    if (!d) continue;
    // Back at the first stop: focus wrapped around the page. A repeat of any
    // other control is Tab moving inside it (a date input's day / month /
    // year segments), not a new stop.
    if (d.id && stops.length > 1 && d.id === stops[0].id) break;
    if (d.id && seen.has(d.id)) continue;
    if (d.id) seen.add(d.id);
    stops.push(d);
  }
  const unreachable = await page.evaluate(
    (ids) =>
      [...document.querySelectorAll("[data-walk]")]
        .filter((el) => !ids.includes(el.dataset.walk))
        // A radio group is one tab stop: reached when any radio of it was.
        .filter(
          (el) =>
            !(
              el.type === "radio" &&
              [
                ...document.querySelectorAll(
                  `input[type=radio][name="${el.name}"]`,
                ),
              ].some((r) => ids.includes(r.dataset.walk))
            ),
        )
        .map(
          (el) =>
            `${el.tagName.toLowerCase()} “${(el.getAttribute("aria-label") || el.innerText || el.name || "").trim().slice(0, 40)}”`,
        ),
    [...seen],
  );
  const noRing = stops.filter((s) => !s.ring);
  failures += unreachable.length + noRing.length;
  report.push({ page: p, total, stops, unreachable, noRing });
}
await browser.close();

const lines = [`# Keyboard walk-through (${WIDTH} px)`, ""];
for (const r of report) {
  lines.push(
    `## ${r.page}`,
    "",
    `${r.stops.length} tab stops of ${r.total} focusable controls; ` +
      `${r.unreachable.length} unreachable; ${r.noRing.length} without a visible focus indicator.`,
    "",
  );
  lines.push(
    "| # | control | name | element | focus visible |",
    "|---|---|---|---|---|",
  );
  r.stops.forEach((s, i) =>
    lines.push(
      `| ${i + 1} | ${s.role} | ${s.name.replace(/\|/g, "/")} | ${s.where} | ${s.ring ? "yes" : "**no**"} |`,
    ),
  );
  if (r.unreachable.length)
    lines.push("", "Unreachable: " + r.unreachable.join(", "));
  lines.push("");
}
const md = lines.join("\n");
fs.writeFileSync(path.join(OUT, `keyboard-walk-${WIDTH}.md`), md);
console.log(md);
process.exit(failures ? 1 : 0);
