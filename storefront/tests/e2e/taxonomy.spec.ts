import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// Category and brand pages (docs/storefront-seo.md). Like the journeys, these
// render from the offline demo catalog: run with no backend on the API origin.

async function seriousViolations(page: import("@playwright/test").Page) {
  const results = await new AxeBuilder({ page }).analyze();
  return results.violations.filter((v) => ["critical", "serious"].includes(v.impact || ""));
}

test("category page lists its products with breadcrumbs, canonical and JSON-LD", async ({
  page,
}) => {
  await page.goto("/c/sensors-and-optics/");
  await expect(page.getByRole("heading", { level: 1, name: "Sensors & Optics" })).toBeVisible();
  const crumbs = page.getByRole("navigation", { name: "Breadcrumb" });
  await expect(crumbs.getByRole("link", { name: "Catalog" })).toHaveAttribute("href", "/");
  await expect(page.getByTestId("listing-count")).toHaveText("2 products");
  await expect(page.locator('link[rel="canonical"]')).toHaveCount(1);
  await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
    "href",
    /\/c\/sensors-and-optics\/$/,
  );
  const types = await page
    .locator('script[type="application/ld+json"]')
    .evaluateAll((els) => els.map((e) => JSON.parse(e.textContent || "{}")["@type"]));
  expect(types).toEqual(["BreadcrumbList", "CollectionPage"]);
  expect(await seriousViolations(page)).toEqual([]);
});

test("unknown category is a 404 page that is not indexed", async ({ page }) => {
  const response = await page.goto("/c/no-such-category/");
  expect(response?.status()).toBe(404);
  await expect(page.getByRole("heading", { name: "Category not found" })).toBeVisible();
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute("content", "noindex");
});

test("brand index links to brand pages that list the brand's products", async ({ page }) => {
  await page.goto("/brands/");
  await expect(page.getByRole("heading", { level: 1, name: "Brands" })).toBeVisible();
  await page.getByRole("link", { name: "Apex Instruments" }).click();
  await expect(page).toHaveURL(/\/brands\/apex-instruments\/$/);
  await expect(page.getByRole("heading", { level: 1, name: "Apex Instruments" })).toBeVisible();
  await expect(page.getByTestId("listing-count")).toHaveText("2 products");
  expect(await seriousViolations(page)).toEqual([]);
});

test("product page links its category and uses one canonical", async ({ page }) => {
  await page.goto("/products/2/");
  await expect(page.locator('link[rel="canonical"]')).toHaveCount(1);
  await expect(page.locator('link[rel="canonical"]')).toHaveAttribute("href", /\/products\/2\/$/);
  await page.getByRole("link", { name: "Sensors & Optics" }).first().click();
  await expect(page).toHaveURL(/\/c\/sensors-and-optics\/$/);
});
