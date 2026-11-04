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
  "access-control-allow-headers": "Content-Type, Authorization, Accept, Idempotency-Key",
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

/** The ERP's guest-checkout confirmation: server-priced lines and totals (OP #3426). */
function confirmationBody(orderNumber: string) {
  return {
    id: "8a6f0b1e-0000-4000-8000-000000000001",
    order_number: orderNumber,
    status: "pending",
    payment_status: "unpaid",
    currency: "USD",
    guest_email: "procurement@industrial-dynamics.com",
    lines: [
      {
        variant: "101",
        sku: "QEG-X900-32G",
        item_name: "Standard (32GB ECC)",
        quantity: 1,
        unit_price: "849.00",
        line_total: "849.00",
        line_tax: "0.00",
      },
    ],
    subtotal: "849.00",
    tax_total: "0.00",
    shipping_total: "0.00",
    discount_total: "0.00",
    grand_total: "849.00",
    total_amount: "849.00",
    created_at: "2026-11-03T12:00:00Z",
  };
}

async function mockOrderSuccess(
  page: Page,
  orderNumber = "SO-12345",
  posted: unknown[] = [],
): Promise<void> {
  await mockApi(page, async (route) => {
    if (route.request().url().includes("/api/orders/") && route.request().method() === "POST") {
      posted.push(route.request().postDataJSON());
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        headers: CORS,
        body: JSON.stringify(confirmationBody(orderNumber)),
      });
      return;
    }
    await route.fulfill({ status: 404, contentType: "application/json", headers: CORS, body: "{}" });
  });
}

async function mockOrderRefusal(page: Page, body: unknown): Promise<void> {
  await mockApi(page, async (route) => {
    if (route.request().url().includes("/api/orders/") && route.request().method() === "POST") {
      await route.fulfill({
        status: 400,
        contentType: "application/json",
        headers: CORS,
        body: JSON.stringify(body),
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
  const posted: any[] = [];
  await mockOrderSuccess(page, "SO-12345", posted);
  await page.goto("/products/1");
  await expect(page.getByRole("heading", { name: "Quantum Edge Gateway X9" })).toBeVisible();
  const variant = page.getByRole("button", { name: /Standard \(32GB ECC\)/ });
  await variant.click();
  // The click must have landed (resumed) before the add reads the selection.
  await expect(variant).toHaveClass(/ring-1/);
  await page.getByRole("button", { name: "+ Add to B2B Cart" }).click();
  await page.getByRole("link", { name: /Proceed to Checkout/ }).click();
  await expect(page.getByRole("heading", { name: "Checkout & Order Submission" })).toBeVisible();
  await expect(page.getByText("Quantum Edge Gateway X9").first()).toBeVisible();
  await page.getByRole("button", { name: /Submit B2B Order/ }).click();
  await expect(page.getByRole("heading", { name: "Order Successfully Placed" })).toBeVisible();
  await expect(page.getByText("SO-12345")).toBeVisible();

  // The cart went out as variant + quantity only; the ERP prices it.
  expect(posted).toHaveLength(1);
  expect(posted[0].lines).toEqual([{ variant: 101, quantity: 1 }]);
  expect(posted[0]).not.toHaveProperty("items");
  expect(JSON.stringify(posted[0].lines)).not.toMatch(/price|total/);

  // The confirmation shows the server's priced lines and totals.
  const items = page.getByRole("table", { name: "Items on your order" });
  await expect(items.getByText("Standard (32GB ECC)")).toBeVisible();
  await expect(items.getByText("QEG-X900-32G")).toBeVisible();
  await expect(page.getByText("$849.00 USD").first()).toBeVisible();
  await expect(page.getByText("Pending", { exact: true })).toBeVisible();
  // The placed order's cart is gone, also after a reload.
  await expect(page.getByRole("button", { name: /^Cart 0$|^Cart$/ })).toBeVisible();
  await page.goto("/cart");
  await expect(page.getByText("Your cart is empty.")).toBeVisible();
});

test("an out-of-stock refusal marks the line and lets the shopper remove it", async ({ page }) => {
  await mockOrderRefusal(page, {
    code: "invalid_lines",
    detail: "Some lines cannot be ordered; nothing was placed.",
    lines: [
      { index: 0, code: "out_of_stock", detail: "x", variant: null, product: "1", available: 0 },
    ],
  });
  await addProductOneToCart(page);
  await page.getByRole("button", { name: /Submit B2B Order/ }).click();

  const alert = page.getByRole("alert");
  await expect(alert.first()).toContainText(/could not be placed/i);
  await expect(alert.first()).toContainText(/no order was placed/i);
  await expect(page.getByRole("heading", { name: "Order Successfully Placed" })).toHaveCount(0);
  const lineError = page.locator('[data-line-error="out_of_stock"]');
  await expect(lineError).toContainText(/out of stock/i);

  await lineError.getByRole("button", { name: /Remove Quantum Edge Gateway X9/ }).click();
  await expect(page.getByText("Your cart is empty.")).toBeVisible();
  // The removed line stays removed (the saved cart is not restored over it).
  await page.reload();
  await expect(page.getByText("Your cart is empty.")).toBeVisible();
});

test("a short-stock refusal says how many are left", async ({ page }) => {
  await mockOrderRefusal(page, {
    code: "invalid_lines",
    detail: "x",
    lines: [{ index: 0, code: "insufficient_stock", detail: "x", variant: "101", product: null, available: 3 }],
  });
  await addProductOneToCart(page);
  await page.getByRole("button", { name: /Submit B2B Order/ }).click();
  await expect(page.locator('[data-line-error="insufficient_stock"]')).toContainText(
    /only 3 in stock/i,
  );
  await expect(page.getByRole("button", { name: /Edit quantities in your cart/ })).toBeVisible();
});

test("checkout failure shows an error, keeps the cart (F-004 visible failure)", async ({
  page,
}) => {
  await mockOrderFailure(page);
  const keys: Array<string | undefined> = [];
  page.on("request", (req) => {
    if (req.method() === "POST" && req.url().includes("/orders/orders/")) {
      keys.push(req.headers()["idempotency-key"]);
    }
  });
  await addProductOneToCart(page);
  await page.getByRole("button", { name: /Submit B2B Order/ }).click();
  const alert = page.getByRole("alert");
  // A 5xx after the POST went out: the order may exist, so never "no order".
  await expect(alert.first()).toContainText(/couldn't confirm your order/i);
  await expect(alert.first()).toContainText(/submit the same cart again from this browser/i);
  await expect(alert.first()).not.toContainText(/no order was placed/i);
  await expect(page.getByRole("heading", { name: "Order Successfully Placed" })).toHaveCount(0);
  // Cart is retained: the line item and submit button are still there.
  await expect(page.getByText("Quantum Edge Gateway X9").first()).toBeVisible();
  await expect(page.getByRole("button", { name: /Submit B2B Order/ })).toBeVisible();

  // OP #3431: the retry of the same cart carries the same Idempotency-Key, so
  // an order that did commit comes back instead of being placed twice.
  await page.getByRole("button", { name: /Submit B2B Order/ }).click();
  await expect.poll(() => keys.length).toBe(2);
  expect(keys[0]).toMatch(/^[A-Za-z0-9._:-]{16,200}$/);
  expect(keys[1]).toBe(keys[0]);
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
  const cardButton = page.getByRole("button", { name: /Sold out: Helios/i });
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
