// Shared back-office page checks: staff login, axe (serious + critical),
// colour-transition settling, the horizontal-scroll fit check and the
// off-palette scan. Used by erp-theme-sweep.mjs, erp-keyboard-walk.mjs and
// the release harness (scripts/erp-harness/). Extracted from the sweep so
// there is exactly one implementation of each (docs/release-testing.md).
import AxeBuilder from "@axe-core/playwright";

// GETs that change state or leave the back office (sweep crawl + harness).
// preview: the print document (its own paper palette, not a themed screen).
export const SKIP =
  /\/(logout|delete|toggle-active|send|send-email|send-shipping-email|update-tracking|pdf|csv|export|download|sso|password-reset|verify-email|duplicate|convert|void|post|approve|receive|cancel|preview)(\/|$)|\.(pdf|csv|xlsx)$/;

// One sample per URL shape: ids collapse to a placeholder.
export const shape = (u) =>
  new URL(u).pathname
    .replace(
      /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g,
      ":uuid",
    )
    .replace(/\/\d+(?=\/|$)/g, "/:id");

// Sign in on /erp/login/ and wait until the back office answers. The first
// request after `runserver` starts is the slow one (the dashboard behind the
// login compiles templates and warms caches): on a loaded runner it passed
// 15 s and three sweep shards died here on 2026-10-27, so the wait is long and
// a second attempt follows a timeout. The wait ends at domcontentloaded, like
// every other navigation in the sweep and the harness: base.html loads fonts
// and icons from Google and cdnjs, and the default "load" wait hung a shard
// for 2 × 60 s on 2026-10-27 while the login itself had nothing to do with
// it. A timeout reports where the page ended up and any form error, so a
// wrong password and a slow server read differently in the CI log.
export async function loginStaff(page, base, username, password, { timeout = 60000, attempts = 2 } = {}) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    await page.goto(`${base}/erp/login/`, { waitUntil: "domcontentloaded" });
    await page.fill("#id_username", username);
    await page.fill("#id_password", password);
    await page.click('button[type="submit"]');
    try {
      await page.waitForURL((u) => !u.pathname.startsWith("/erp/login"), {
        timeout,
        waitUntil: "domcontentloaded",
      });
      return;
    } catch (e) {
      lastError = e;
      const formError = await page
        .locator(".login-error, [role=alert], .errorlist")
        .first()
        .innerText({ timeout: 1000 })
        .catch(() => "");
      console.error(
        `loginStaff: attempt ${attempt}/${attempts} did not leave /erp/login/ within ${timeout} ms` +
          ` (at ${page.url()}${formError ? `; form says: ${formError.trim().slice(0, 200)}` : ""})`,
      );
    }
  }
  throw lastError;
}

// body and many links transition `color`, so text fades in from the unthemed
// fallback after the skin loads; axe must wait for every running transition
// (not every animation: some pages loop forever). CLAUDE.md, storefront gotchas.
export async function awaitTransitions(page) {
  const wait = () =>
    page.evaluate(() =>
      Promise.all(
        document
          .getAnimations()
          .filter((a) => a instanceof CSSTransition)
          .map((a) => a.finished.catch(() => {})),
      ),
    );
  try {
    await wait();
  } catch (err) {
    // A page that redirects itself (a meta refresh, a script navigation)
    // destroys the context mid-wait; settle on the new document and retry
    // once instead of aborting a 2000-check sweep (seen 2026-10-18).
    if (!String(err).includes("Execution context was destroyed")) throw err;
    await page.waitForLoadState("networkidle").catch(() => {});
    await wait();
  }
}

// axe violations of impact serious or critical: [{ id, nodes, sample }].
export async function axeSerious(page) {
  const axe = await new AxeBuilder({ page }).analyze();
  return axe.violations
    .filter((v) => ["serious", "critical"].includes(v.impact))
    .map((v) => ({
      id: v.id,
      nodes: v.nodes.length,
      sample: v.nodes[0]?.target.join(" "),
    }));
}

// No horizontal page scroll (phase4-fields.md 5.6): a list's own
// .table-responsive may scroll, the page may not.
export async function horizontalScroll(page) {
  const r = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  return { ...r, hscroll: r.scrollWidth > r.innerWidth };
}

// page.evaluate function: elements whose text or background colour is not
// one of the active theme's --brand-t-* tokens.
export const OFF_PALETTE = () => {
  const parse = (c) => {
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const [r, g, b, a = "1"] = m[1]
      .split(/[,\s/]+/)
      .filter(Boolean)
      .map(Number);
    return { r, g, b, a: Number(a) };
  };
  // Resolve each token to rgb through a probe element.
  const probe = document.createElement("div");
  document.body.append(probe);
  const root = getComputedStyle(document.documentElement);
  const tokens = [
    ...new Set(
      [...document.styleSheets]
        .flatMap((s) => {
          try {
            return [...s.cssRules].map((r) => r.cssText);
          } catch {
            return [];
          }
        })
        .join("\n")
        .match(/--brand-t-[a-z-]+/g) ?? [],
    ),
  ];
  const palette = [];
  for (const t of tokens) {
    if (!root.getPropertyValue(t).trim()) continue;
    probe.style.color = `var(${t})`;
    const c = parse(getComputedStyle(probe).color);
    if (c) palette.push({ t, ...c });
  }
  probe.remove();
  const near = (c) =>
    palette.some(
      (p) =>
        Math.abs(p.r - c.r) + Math.abs(p.g - c.g) + Math.abs(p.b - c.b) <= 24,
    );
  const found = new Map();
  for (const el of document.querySelectorAll("body *")) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none") continue;
    const hasText = [...el.childNodes].some(
      (n) => n.nodeType === 3 && n.textContent.trim(),
    );
    for (const [prop, val] of [
      ["bg", cs.backgroundColor],
      ["text", hasText ? cs.color : null],
    ]) {
      const c = val && parse(val);
      // Translucent fills are tints over tokens; judge only solid colours.
      if (!c || c.a < 0.95 || near(c)) continue;
      const key = `${prop} rgb(${c.r},${c.g},${c.b})`;
      const sel =
        el.tagName.toLowerCase() +
        (el.id ? `#${el.id}` : "") +
        [...el.classList]
          .slice(0, 3)
          .map((x) => `.${x}`)
          .join("");
      const e = found.get(key) ?? { count: 0, examples: new Set() };
      e.count++;
      if (e.examples.size < 3) e.examples.add(sel);
      found.set(key, e);
    }
  }
  return {
    paletteSize: palette.length,
    off: [...found].map(([k, v]) => ({
      colour: k,
      count: v.count,
      examples: [...v.examples],
    })),
  };
};
