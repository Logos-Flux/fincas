import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// testing-plan §8.1 smoke + §8.4 a11y baseline. Run: bun run test:e2e
test("home loads with no console errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto("/");
  await expect(page).toHaveTitle(/.+/);
  expect(errors).toEqual([]);
});

test("home has no serious accessibility violations", async ({ page }) => {
  await page.goto("/");
  const results = await new AxeBuilder({ page }).analyze();
  const serious = results.violations.filter((v) =>
    ["critical", "serious"].includes(v.impact || ""),
  );
  expect(serious).toEqual([]);
});

test("design-spec demo bar is gone, theme dropdown remains", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Design Spec Demo")).toHaveCount(0);
  await expect(
    page.getByTitle("Switch Design Template"),
  ).toBeVisible();
});

test("a pre-rename theme id in localStorage migrates to its new file", async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem("golden_theme", "spacex"));
  await page.goto("/");
  await expect(page.locator("#theme-css")).toHaveAttribute(
    "href",
    "/themes/space-company.css",
  );
  expect(await page.evaluate(() => localStorage.getItem("golden_theme"))).toBe(
    "space-company",
  );
});
