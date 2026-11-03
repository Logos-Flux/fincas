import { expect, test, type Page, type Request } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// Customer /account area (identity-unification spec §5, step 7). The dev
// server has no Supabase settings, so the auth adapter runs its mock
// provider; the ERP portal API is mocked at the dead origin the dev server's
// VITE_API_BASE_URL points at (see CLAUDE.md: no backend on :8000).
const API_ORIGIN = process.env.E2E_API_ORIGIN ?? "http://localhost:8000";
const PORTAL = `${API_ORIGIN}/api/v1/portal/customer`;

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, POST, PUT, PATCH, OPTIONS",
  "access-control-allow-headers": "Content-Type, Authorization, Accept",
};

const ORDER_A = "6f1c2b8e-0d3a-4c55-9a7e-1b2c3d4e5f60";
const ORDER_B = "0a9b8c7d-6e5f-4a3b-8c2d-1e0f9a8b7c6d";

const PROFILE = {
  id: "c0ffee00-0000-4000-8000-000000000001",
  first_name: "Dana",
  last_name: "Rivera",
  full_name: "Dana Rivera",
  email: "dana@example.com",
  phone: "+1 555 0100",
  company_name: "Rivera Fabrication",
  customer_type: "business",
  is_tax_exempt: false,
  accepts_marketing: false,
  default_currency: "USD",
  created_at: "2026-01-05T10:00:00Z",
  updated_at: "2026-09-01T10:00:00Z",
};

const COUNTS = { orders: 2, quotes: 1, invoices: 3, shared_docs: 0, product_docs: 4, proposals: 0 };

const ORDERS = [
  {
    id: ORDER_A,
    order_number: "SO-10042",
    status: "partially_shipped",
    payment_status: "paid",
    fulfillment_status: "partial",
    grand_total: "1983.40",
    created_at: "2026-09-28T15:30:00Z",
    item_count: 2,
  },
  {
    id: ORDER_B,
    order_number: "SO-10017",
    status: "completed",
    payment_status: "paid",
    fulfillment_status: "fulfilled",
    grand_total: "310.00",
    created_at: "2026-08-02T09:00:00Z",
    item_count: 1,
  },
];

const ORDER_A_DETAIL = {
  ...ORDERS[0],
  order_date: "2026-09-28",
  subtotal_ex_tax: "1720.00",
  subtotal_inc_tax: "1858.40",
  tax_total: "138.40",
  shipping_cost_inc_tax: "125.00",
  customer_message: "Deliver to dock 4.",
  updated_at: "2026-10-02T12:00:00Z",
  line_items: [
    {
      id: "l1",
      variant: 11,
      sku: "QEG-X900-PRO",
      item_name: "Quantum Edge Gateway X9",
      quantity: 2,
      unit_price_ex_tax: "800.00",
      unit_price_inc_tax: "864.00",
      line_total_ex_tax: "1600.00",
      line_total_inc_tax: "1728.00",
      fulfillment_status: "fulfilled",
    },
    {
      id: "l2",
      variant: 12,
      sku: "MNT-RAIL-35",
      item_name: "DIN Rail Mount Kit",
      quantity: 1,
      unit_price_ex_tax: "120.00",
      unit_price_inc_tax: "130.40",
      line_total_ex_tax: "120.00",
      line_total_inc_tax: "130.40",
      fulfillment_status: "unfulfilled",
    },
  ],
  sub_orders: [
    {
      id: "s1",
      sub_order_number: "SO-10042-1",
      status: "shipped",
      tracking_number: "1Z999AA10123456784",
      tracking_url: "https://tracking.example.com/1Z999AA10123456784",
      estimated_ship_date: "2026-09-30",
      actual_ship_date: "2026-09-30",
      estimated_delivery_date: "2026-10-04",
      actual_delivery_date: null,
    },
  ],
};

interface PortalMock {
  /** Every portal request the page made, for header assertions. */
  requests: Request[];
}

/** Mock the portal API. `rejectToken` makes every portal call answer 403. */
async function mockPortal(page: Page, opts: { rejectToken?: boolean } = {}): Promise<PortalMock> {
  const mock: PortalMock = { requests: [] };
  await page.route(`${API_ORIGIN}/**`, async (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") {
      await route.fulfill({ status: 200, headers: CORS, body: "" });
      return;
    }
    const url = new URL(req.url());
    const json = (status: number, body: unknown) =>
      route.fulfill({ status, contentType: "application/json", headers: CORS, body: JSON.stringify(body) });

    if (!url.href.startsWith(PORTAL)) return json(404, {});
    mock.requests.push(req);
    const auth = req.headers()["authorization"] ?? "";
    if (opts.rejectToken || !auth.startsWith("Bearer mock-access-token.")) {
      return json(403, { detail: "Authentication credentials were not provided." });
    }
    const path = url.pathname.slice(new URL(PORTAL).pathname.length);
    if (path === "/profile/") return json(200, PROFILE);
    if (path === "/module-counts/") return json(200, COUNTS);
    if (path === "/orders/") return json(200, ORDERS);
    if (path === `/orders/${ORDER_A}/`) return json(200, ORDER_A_DETAIL);
    return json(404, { detail: "Not found." });
  });
  return mock;
}

/** axe after the skin has loaded and colour transitions have settled (as landing.spec.ts). */
async function expectNoSeriousA11yViolations(page: Page, exclude?: string): Promise<void> {
  await page.waitForFunction(
    () =>
      [...document.styleSheets].some((s) => s.href?.includes("/themes/") && s.cssRules.length > 0) &&
      getComputedStyle(document.documentElement).getPropertyValue("--color-t-text").trim() !== "",
  );
  await page.evaluate(() =>
    Promise.all(
      document
        .getAnimations()
        .filter((a) => a instanceof CSSTransition)
        .map((a) => a.finished.catch(() => undefined)),
    ),
  );
  // The Qwik dev server's click-to-source popup is a dev-only overlay.
  const builder = new AxeBuilder({ page }).exclude("#qwik-inspector-info-popup");
  if (exclude) builder.exclude(exclude);
  const results = await builder.analyze();
  const serious = results.violations.filter((v) => ["critical", "serious"].includes(v.impact || ""));
  expect(serious).toEqual([]);
}

async function signIn(page: Page, email = "dana@example.com", password = "correct-horse"): Promise<void> {
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test.beforeEach(async () => {
  test.setTimeout(90000);
});

test("logged-out visitors are sent to sign in, then back to the page they asked for", async ({ page }) => {
  const mock = await mockPortal(page);
  await page.goto(`/account/orders/${ORDER_A}/`);
  await expect(page).toHaveURL(
    `/account/login/?next=${encodeURIComponent(`/account/orders/${ORDER_A}/`)}`,
  );
  await expect(page.getByRole("heading", { name: "Sign in to your account" })).toBeVisible();
  await expect(page.getByRole("status")).toHaveText("Sign in to view your account.");
  // Nothing was requested from the portal without a session.
  expect(mock.requests).toEqual([]);

  await signIn(page);
  await expect(page).toHaveURL(`/account/orders/${ORDER_A}/`);
  await expect(page.getByRole("heading", { name: "Order SO-10042" })).toBeVisible();
});

test("every account route redirects when logged out", async ({ page }) => {
  await mockPortal(page);
  for (const path of ["/account/", "/account/orders/"]) {
    await page.goto(path);
    await expect(page).toHaveURL(`/account/login/?next=${encodeURIComponent(path)}`);
  }
});

test("journey: sign in → orders → order detail → sign out, axe-clean on each page", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const mock = await mockPortal(page);

  await page.goto("/");
  await page.getByRole("link", { name: "Account", exact: true }).click();
  await expect(page).toHaveURL(/\/account\/login\/\?next=/);
  await expect(page.getByRole("heading", { name: "Sign in to your account" })).toBeVisible();
  await expectNoSeriousA11yViolations(page);

  // Error states: empty form, then a rejected password.
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toHaveText("Enter your email address and password.");
  await signIn(page, "dana@example.com", "wrong-password");
  await expect(page.getByRole("alert")).toHaveText("Invalid email or password.");
  await expect(page.getByLabel("Email")).toHaveAttribute("aria-invalid", "true");
  await expectNoSeriousA11yViolations(page);

  await signIn(page);
  await expect(page).toHaveURL("/account/");
  await expect(page.getByRole("heading", { name: "Welcome back, Dana" })).toBeVisible();
  await expect(page.getByText("Rivera Fabrication")).toBeVisible();
  await expect(page.getByText("Signed in as dana@example.com")).toBeVisible();
  const totals = page.getByRole("list", { name: "Account totals" });
  await expect(totals.getByRole("listitem")).toHaveCount(4);
  await expect(totals.getByRole("link", { name: /2\s*Orders/ })).toBeVisible();
  await expectNoSeriousA11yViolations(page);

  await page.getByRole("navigation", { name: "Account sections" }).getByRole("link", { name: "Orders" }).click();
  await expect(page).toHaveURL("/account/orders/");
  await expect(page.getByRole("heading", { name: "Orders", level: 1 })).toBeVisible();
  const rows = page.locator("tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("SO-10042");
  await expect(rows.nth(0)).toContainText("Partially shipped");
  await expect(rows.nth(0)).toContainText("$1,983.40");
  await expect(rows.nth(1)).toContainText("SO-10017");
  await expect(rows.nth(1)).toContainText("Completed");
  await expectNoSeriousA11yViolations(page);

  await page.getByRole("link", { name: "SO-10042" }).click();
  await expect(page).toHaveURL(`/account/orders/${ORDER_A}/`);
  await expect(page.getByRole("heading", { name: "Order SO-10042" })).toBeVisible();
  const items = page.getByRole("table", { name: /Items in order SO-10042/ });
  await expect(items.getByRole("row")).toHaveCount(3);
  await expect(items).toContainText("Quantum Edge Gateway X9");
  await expect(items).toContainText("$1,728.00");
  await expect(page.getByText("Order total")).toBeVisible();
  await expect(page.getByText("$1,983.40")).toBeVisible();
  const tracking = page.getByRole("link", { name: /1Z999AA10123456784/ });
  await expect(tracking).toHaveAttribute("href", "https://tracking.example.com/1Z999AA10123456784");
  await expect(page.getByText("Deliver to dock 4.")).toBeVisible();
  await expectNoSeriousA11yViolations(page);

  // Every portal call carried the session's bearer token.
  expect(mock.requests.length).toBeGreaterThanOrEqual(4);
  for (const req of mock.requests) {
    expect(req.headers()["authorization"]).toBe(
      `Bearer mock-access-token.${encodeURIComponent("dana@example.com")}`,
    );
  }

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL("/account/login/?signed_out=1");
  await expect(page.getByRole("status")).toHaveText("You have been signed out.");

  // The session is really gone: the account link leads back to sign-in.
  await page.getByRole("link", { name: "Account", exact: true }).click();
  await expect(page).toHaveURL(/\/account\/login\/\?next=/);
  expect(errors).toEqual([]);
});

test("a token the ERP rejects ends the session and asks to sign in again", async ({ page }) => {
  await mockPortal(page, { rejectToken: true });
  await page.goto("/account/login/");
  await signIn(page);
  await expect(page).toHaveURL(/\/account\/login\/\?next=%2Faccount%2F&reason=expired$/);
  await expect(page.getByRole("status")).toHaveText("Your session has ended. Sign in again to continue.");
});

test("an unknown order shows a not-found state, not a fake order", async ({ page }) => {
  await mockPortal(page);
  await page.goto("/account/login/?next=%2Faccount%2Forders%2F11111111-2222-4333-8444-555555555555%2F");
  await signIn(page);
  await expect(page.getByRole("heading", { name: "Order not found" })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveText("We could not find that order in your account.");
});

test("the account area fits a phone without horizontal scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await mockPortal(page);
  await page.goto(`/account/login/?next=${encodeURIComponent(`/account/orders/${ORDER_A}/`)}`);
  let overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await signIn(page);
  await expect(page.getByRole("heading", { name: "Order SO-10042" })).toBeVisible();
  overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

// The journey above runs in the default dark skin; repeat axe in a light skin
// and a black-accent one (same picks as landing.spec.ts). The store footer is
// excluded here: its hard-coded emerald "Headless Engine Online" pill and
// 60%-opacity text fail contrast in light skins on every store page — an
// existing footer issue, not part of the account area.
for (const theme of ["workflow-database", "design-tool"]) {
  test(`account pages have no serious a11y violations in ${theme}`, async ({ page }) => {
    await page.addInitScript((id) => localStorage.setItem("golden_theme", id), theme);
    await mockPortal(page);
    await page.goto("/account/login/?next=%2Faccount%2F");
    await expect(page.locator("#theme-css")).toHaveAttribute("href", `/themes/${theme}.css`);
    await expectNoSeriousA11yViolations(page, "footer");

    await signIn(page);
    await expect(page.getByRole("heading", { name: "Welcome back, Dana" })).toBeVisible();
    await expect(page.getByRole("list", { name: "Account totals" })).toBeVisible();
    await expectNoSeriousA11yViolations(page, "footer");

    await page.getByRole("link", { name: "View your orders →" }).click();
    await expect(page.locator("tbody tr")).toHaveCount(2);
    await expectNoSeriousA11yViolations(page, "footer");

    await page.getByRole("link", { name: "SO-10042" }).click();
    await expect(page.getByRole("heading", { name: "Order SO-10042" })).toBeVisible();
    await expectNoSeriousA11yViolations(page, "footer");
  });
}
