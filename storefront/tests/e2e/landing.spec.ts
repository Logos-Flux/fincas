import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { LANDING_TEMPLATES } from "../../src/lib/landing-templates";

// Landing-page templates: /landing gallery + one standalone page per template.

test("gallery lists every landing template", async ({ page }) => {
  await page.goto("/landing/");
  for (const t of LANDING_TEMPLATES) {
    await expect(page.getByRole("heading", { name: t.name })).toBeVisible();
  }
});

// Dark, light, white-accent and black-accent skins. Each one's own text and
// muted colours pass AA, so a failure here is the page's fault, not the
// palette's (many skins' muted greys fail AA on their own — see TODO.md).
const THEMES = [
  "issue-tracker",
  "workflow-database",
  "frontier-ai-lab",
  "design-tool",
];

for (const t of LANDING_TEMPLATES) {
  test(`${t.slug}: standalone page renders without store chrome`, async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(`/landing/${t.slug}/`);
    await expect(page.locator("h1")).toHaveCount(1);
    await expect(page.getByText(t.brand).first()).toBeVisible();
    // The store's cart button belongs to the store layout only.
    await expect(page.getByRole("button", { name: /cart/i })).toHaveCount(0);
    expect(errors).toEqual([]);
  });

  for (const theme of THEMES) {
    test(`${t.slug}: no serious a11y violations in ${theme}`, async ({
      page,
    }) => {
      await page.addInitScript(
        (id) => localStorage.setItem("golden_theme", id),
        theme,
      );
      await page.goto(`/landing/${t.slug}/`);
      await expect(page.locator("#theme-css")).toHaveAttribute(
        "href",
        `/themes/${theme}.css`,
      );
      // The href flips before the sheet loads, and text then fades from the
      // unthemed fallback colour (body/link colour transitions), so wait for
      // the sheet and then for those transitions — not the looping ambient
      // animations, which never finish — or axe can catch a mid-fade colour.
      await page.waitForFunction(
        (id) =>
          [...document.styleSheets].some(
            (sheet) =>
              sheet.href?.endsWith(`/themes/${id}.css`) &&
              sheet.cssRules.length > 0,
          ) &&
          getComputedStyle(document.documentElement)
            .getPropertyValue("--color-t-text")
            .trim() !== "",
        theme,
      );
      await page.evaluate(() =>
        Promise.all(
          document
            .getAnimations()
            .filter((a) => a instanceof CSSTransition)
            .map((a) => a.finished.catch(() => undefined)),
        ),
      );
      const results = await new AxeBuilder({ page }).analyze();
      const serious = results.violations.filter((v) =>
        ["critical", "serious"].includes(v.impact || ""),
      );
      expect(serious).toEqual([]);
    });
  }
}

test("no landing page scrolls horizontally on a phone", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  for (const t of LANDING_TEMPLATES) {
    await page.goto(`/landing/${t.slug}/`);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    );
    expect(overflow, t.slug).toBeLessThanOrEqual(0);
  }
});
