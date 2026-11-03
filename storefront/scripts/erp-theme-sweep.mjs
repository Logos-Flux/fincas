// ERP theme sweep: every back-office page × every registered theme.
//
// For each page and theme it saves a full-page screenshot, runs axe (serious +
// critical only), and flags "off-palette" colours: elements whose text or
// background colour is not one of the active theme's --brand-t-* tokens.
// Those are the stock-framework or hard-coded colours that make a theme look
// half-applied.
//
// Run from storefront/ against a running Django dev server:
//   SWEEP_USER=… SWEEP_PASS=… node scripts/erp-theme-sweep.mjs
// Env: ERP_URL (default http://127.0.0.1:8000), SWEEP_OUT (default
// test-results/erp-sweep), SWEEP_THEMES (comma list; default: all registered),
// SWEEP_ONLY (regex on the URL shape, e.g. "accounting/import"), SWEEP_SEEDS
// (comma list of extra start paths, query strings kept: a seed such as
// "/erp/parties/?kind=organisation" samples a list with an active filter, so
// its "Save this view" form renders). Pages with list tools are checked
// twice: as loaded and with the chooser / save-filter panels open ("#open").
// SWEEP_WIDTHS (comma list of viewport widths, default "1440,360"): every
// page is checked at each width; below 1440 the page key gets "@<width>".
// Each check also asserts no horizontal page scroll
// (document.documentElement.scrollWidth <= innerWidth, phase4-fields.md 5.6);
// a list's own .table-responsive may scroll, the page may not. The summary
// line counts axe serious, off-palette elements and horizontal-scroll pages.
import { chromium } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import {
  OFF_PALETTE,
  SKIP,
  awaitTransitions,
  axeSerious,
  horizontalScroll,
  loginStaff,
  shape,
} from "./lib/erp-checks.mjs";

const BASE = process.env.ERP_URL ?? "http://127.0.0.1:8000";
const OUT = process.env.SWEEP_OUT ?? "test-results/erp-sweep";
const { SWEEP_USER, SWEEP_PASS } = process.env;
if (!SWEEP_USER || !SWEEP_PASS)
  throw new Error("set SWEEP_USER and SWEEP_PASS");

fs.mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
});
const page = await context.newPage();

await loginStaff(page, BASE, SWEEP_USER, SWEEP_PASS);

const themes = process.env.SWEEP_THEMES
  ? process.env.SWEEP_THEMES.split(",")
  : await page.evaluate(() =>
      JSON.parse(document.getElementById("erp-themes-data").textContent).map(
        (t) => t.name,
      ),
    );

// ── Crawl: breadth-first from the dashboard, one sample per URL shape ──
const seeds = (process.env.SWEEP_SEEDS ?? "").split(",").filter(Boolean);
const queue = [`${BASE}/erp/`, ...seeds.map((s) => BASE + s)];
const pages = new Map(); // shape -> url
while (queue.length && pages.size < 200) {
  const url = queue.shift();
  const key = shape(url);
  if (pages.has(key) || SKIP.test(new URL(url).pathname)) continue;
  const res = await page
    .goto(url, { waitUntil: "domcontentloaded" })
    .catch(() => null);
  if (!res || res.status() >= 400 || !page.url().startsWith(`${BASE}/erp`))
    continue;
  pages.set(key, url);
  const links = await page.$$eval("a[href]", (as) => as.map((a) => a.href));
  for (const l of links) {
    if (!l.startsWith(`${BASE}/erp/`)) continue;
    const clean = l.split("#")[0].split("?")[0];
    if (!pages.has(shape(clean))) queue.push(clean);
  }
}
// SWEEP_ONLY: regex on the URL shape, to re-check a few pages quickly.
if (process.env.SWEEP_ONLY) {
  const only = new RegExp(process.env.SWEEP_ONLY);
  for (const k of [...pages.keys()]) if (!only.test(k)) pages.delete(k);
}
console.log(`crawled ${pages.size} page shapes; themes: ${themes.join(", ")}`);

fs.writeFileSync(
  path.join(OUT, "pages.json"),
  JSON.stringify([...pages.values()], null, 2),
);

// ── Per page × theme checks ──
const WIDTHS = (process.env.SWEEP_WIDTHS ?? "1440,360")
  .split(",")
  .map(Number)
  .filter(Boolean);
const report = [];
for (const theme of themes) {
  await page.evaluate((t) => localStorage.setItem("erp-theme", t), theme);
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: width < 768 ? 780 : 900 });
    for (const [shapeKey, url] of pages) {
      const key = width === 1440 ? shapeKey : `${shapeKey}@${width}`;
      await page.goto(url, { waitUntil: "networkidle" }).catch(() => {});
      await awaitTransitions(page);
      const file = `${theme}__${
        key
          .replace(/^\/erp\/?/, "")
          .replace(/[/:]+/g, "_")
          .replace(/_$/, "")
          .replace(/@/, "__w") || "dashboard"
      }.png`;
      await page.screenshot({ path: path.join(OUT, file), fullPage: true });
      const serious = await axeSerious(page);
      const palette = await page.evaluate(OFF_PALETTE);
      const { hscroll, ...scroll } = await horizontalScroll(page);
      report.push({
        theme,
        page: key,
        file,
        axe: serious,
        hscroll,
        ...scroll,
        ...palette,
      });
      // List tools (phase4-fields.md 5.3, 5.4): the column chooser and the
      // save-filter form live in closed <details>, which axe and the palette
      // check skip. Open them and check the page again as "<shape>#open".
      const opened = await page.evaluate(() => {
        const panels = document.querySelectorAll(
          "details.erp-chooser, details.erp-save-filter",
        );
        panels.forEach((d) => (d.open = true));
        return panels.length;
      });
      if (opened) {
        const openFile = file.replace(/\.png$/, "__open.png");
        await page.screenshot({
          path: path.join(OUT, openFile),
          fullPage: true,
        });
        const openAxe = await axeSerious(page);
        const openPalette = await page.evaluate(OFF_PALETTE);
        const { hscroll: openHscroll, ...openScroll } =
          await horizontalScroll(page);
        report.push({
          theme,
          page: `${key}#open`,
          file: openFile,
          axe: openAxe,
          hscroll: openHscroll,
          ...openScroll,
          ...openPalette,
        });
      }
      process.stdout.write(".");
    }
  }
}
await browser.close();

fs.writeFileSync(
  path.join(OUT, "report.json"),
  JSON.stringify(report, null, 2),
);
const offTotal = (r) => r.off.reduce((n, o) => n + o.count, 0);
const lines = [
  "| theme | page | axe serious | off-palette elements | h-scroll | worst colours |",
  "|---|---|---|---|---|---|",
];
for (const r of report)
  lines.push(
    `| ${r.theme} | ${r.page} | ${r.axe.map((a) => `${a.id}×${a.nodes}`).join(", ") || "—"} | ${offTotal(r)} | ${r.hscroll ? `**${r.scrollWidth}>${r.innerWidth}**` : "no"} | ${r.off
      .sort((a, b) => b.count - a.count)
      .slice(0, 3)
      .map((o) => `${o.colour} ×${o.count}`)
      .join("; ")} |`,
  );
fs.writeFileSync(path.join(OUT, "report.md"), lines.join("\n") + "\n");
const totals = {
  axe: report.reduce((n, r) => n + r.axe.reduce((m, a) => m + a.nodes, 0), 0),
  contrast: report.reduce(
    (n, r) =>
      n +
      r.axe
        .filter((a) => a.id === "color-contrast")
        .reduce((m, a) => m + a.nodes, 0),
    0,
  ),
  off: report.reduce((n, r) => n + offTotal(r), 0),
  hscroll: report.filter((r) => r.hscroll).length,
};
const summary =
  `${report.length} checks: ${totals.off} off-palette, ${totals.contrast} contrast, ` +
  `${totals.axe} serious axe, ${totals.hscroll} with horizontal page scroll`;
fs.appendFileSync(path.join(OUT, "report.md"), `\n${summary}\n`);
console.log(
  `\nwrote ${report.length} results to ${OUT}/report.{json,md}\n${summary}`,
);
