import { expect, test, type Page, type Route } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// testing-plan §6 E2E journeys. The Django API is mocked with route
// interception — no live backend required. Catalog pages render from the
// offline demo catalog; only the checkout POST crosses the (mocked) API.

// The origin the dev server's VITE_API_BASE_URL points at; the browser calls it
// directly, so the mocks must intercept that origin. CI runs the default.
const API_ORIGIN = process.env.E2E_API_ORIGIN ?? "http://localhost:8000";

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "access-control-allow-headers": "Content-Type, Authorization, Accept",
};

async function mockApi(
  page: Page,
  handler: (route: Route) => Promise<void>,
): Promise<void> {
  await page.route(`${API_ORIGIN}/**`, async (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") {
      await route.fulfill({ status: 200, headers: CORS, body: "" });
      return;
    }
    await handler(route);
  });
}

async function mockOrderSuccess(page: Page, orderNumber = "SO-12345"): Promise<void> {
  await mockApi(page, async (route) => {
    if (route.request().url().includes("/api/orders/") && route.request().method() === "POST") {
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        headers: CORS,
        body: JSON.stringify({
          order_number: orderNumber,
          total_amount: "849.00",
          currency: "USD",
          status: "CONFIRMED",
          payment_status: "SETTLED",
        }),
      });
      return;
    }
    await route.fulfill({ status: 404, contentType: "application/json", headers: CORS, body: "{}" });
  });
}

async function mockOrderFailure(page: Page, status = 500): Promise<void> {
  await mockApi(page, async (route) => {
    if (route.request().url().includes("/api/orders/") && route.request().method() === "POST") {
      await route.fulfill({
        status,
        contentType: "application/json",
        headers: CORS,
        body: JSON.stringify({ detail: "boom" }),
      });
      return;
    }
    await route.fulfill({ status: 404, contentType: "application/json", headers: CORS, body: "{}" });
  });
}

async function addProductOneToCart(page: Page): Promise<void> {
  await page.goto("/products/1");
  await expect(page.getByRole("heading", { name: "Quantum Edge Gateway X9" })).toBeVisible();
  await page.getByRole("button", { name: "+ Add to B2B Cart" }).click();
  await expect(page.getByRole("link", { name: /Proceed to Checkout/ })).toBeVisible();
  await page.getByRole("link", { name: /Proceed to Checkout/ }).click();
  await expect(page.getByRole("heading", { name: "Checkout & Order Submission" })).toBeVisible();
}

test.beforeEach(async () => {
  test.setTimeout(90000);
});

test("PDP → add-to-cart → checkout → confirmation (mocked ERP 201)", async ({ page }) => {
  await mockOrderSuccess(page);
  await addProductOneToCart(page);
  await expect(page.getByText("Quantum Edge Gateway X9").first()).toBeVisible();
  await page.getByRole("button", { name: /Submit B2B Order/ }).click();
  await expect(page.getByRole("heading", { name: "Order Successfully Placed" })).toBeVisible();
  await expect(page.getByText("SO-12345")).toBeVisible();
});

test("checkout failure shows an error, keeps the cart (F-004 visible failure)", async ({
  page,
}) => {
  await mockOrderFailure(page);
  await addProductOneToCart(page);
  await page.getByRole("button", { name: /Submit B2B Order/ }).click();
  const alert = page.getByRole("alert");
  await expect(alert.first()).toContainText(/could not be placed/i);
  await expect(alert.first()).toContainText(/no order was placed/i);
  await expect(page.getByRole("heading", { name: "Order Successfully Placed" })).toHaveCount(0);
  // Cart is retained: the line item and submit button are still there.
  await expect(page.getByText("Quantum Edge Gateway X9").first()).toBeVisible();
  await expect(page.getByRole("button", { name: /Submit B2B Order/ })).toBeVisible();
});

test("search with no matches shows the empty-result state", async ({ page }) => {
  await page.goto("/");
  await page.getByPlaceholder("Search products or SKUs...").fill("zzz-no-such-product-zzz");
  await expect(page.getByText("No products found matching your search.")).toBeVisible();
});

test("out-of-stock SKU is blocked on PDP and PLP", async ({ page }) => {
  await page.goto("/products/7");
  await expect(page.getByRole("heading", { name: "Helios Cryogenic Flow Regulator" })).toBeVisible();
  await expect(page.getByText("Out of Stock", { exact: true }).first()).toBeVisible();
  const addButton = page.getByRole("button", { name: /Out of Stock/ });
  await expect(addButton).toBeDisabled();
  // No cart drawer opens: the checkout link never appears.
  await expect(page.getByRole("link", { name: /Proceed to Checkout/ })).toHaveCount(0);

  await page.goto("/");
  const cardButton = page.getByRole("button", { name: /Helios.*out of stock/i });
  await expect(cardButton).toBeDisabled();
});

test("invalid coupon shows a message and leaves the total unchanged", async ({ page }) => {
  await mockOrderSuccess(page);
  await addProductOneToCart(page);
  await expect(page.getByText("$849.00 USD").first()).toBeVisible();

  await page.locator("#co-coupon").fill("NOPE");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByRole("alert")).toContainText(/not recognized/i);
  await expect(page.getByText("$849.00 USD").first()).toBeVisible();
});

test("valid coupon applies a discount line and lowers the total", async ({ page }) => {
  await mockOrderSuccess(page);
  await addProductOneToCart(page);

  await page.locator("#co-coupon").fill("SAVE10");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByText("Coupon SAVE10")).toBeVisible();
  await expect(page.getByText("−$84.90 USD")).toBeVisible();
  await expect(page.getByText("$764.10 USD").first()).toBeVisible();

  await page.getByRole("button", { name: /Remove coupon SAVE10/ }).click();
  await expect(page.getByText("$849.00 USD").first()).toBeVisible();
});

test("PDP has no serious accessibility violations", async ({ page }) => {
  await page.goto("/products/1");
  const results = await new AxeBuilder({ page }).analyze();
  const serious = results.violations.filter((v) =>
    ["critical", "serious"].includes(v.impact || ""),
  );
  expect(serious).toEqual([]);
});

test("cart has no serious accessibility violations", async ({ page }) => {
  await mockOrderSuccess(page);
  await addProductOneToCart(page);
  const results = await new AxeBuilder({ page }).analyze();
  const serious = results.violations.filter((v) =>
    ["critical", "serious"].includes(v.impact || ""),
  );
  expect(serious).toEqual([]);
});
