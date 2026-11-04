import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  OrderSubmitError,
  __resetStorefrontTokenForTests,
  checkoutKeyFor,
  checkoutLines,
  clearCheckoutKey,
  fetchCatalog,
  fetchProductById,
  getApiBaseUrl,
  getBackendOrigin,
  submitOrder,
  UNCONFIRMED_ORDER_MESSAGE,
  type CheckoutPayload,
  type Product,
} from "../../src/lib/api-client";

function payload(): CheckoutPayload {
  return {
    customer_name: "Test",
    company_name: "Test Co",
    email: "t@example.com",
    shipping_address: "1 Test Way",
    payment_method: "credit_card",
    lines: [{ variant: "v-1", quantity: 2 }],
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  __resetStorefrontTokenForTests();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("getApiBaseUrl", () => {
  it("defaults to the local Django API", () => {
    expect(getApiBaseUrl()).toBe("http://localhost:8000/api");
  });

  it("honors VITE_API_BASE_URL and strips a trailing slash", () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://erp.example.com/api/");
    expect(getApiBaseUrl()).toBe("https://erp.example.com/api");
  });
});

describe("getBackendOrigin", () => {
  it("defaults to the local backend origin", () => {
    expect(getBackendOrigin()).toBe("http://localhost:8000");
  });

  it("derives the origin from the configured API base URL", () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://erp.example.com/api");
    expect(getBackendOrigin()).toBe("https://erp.example.com");
  });
});

describe("fetchCatalog", () => {
  it("requests the catalog under the configured base URL", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://erp.example.com/api");
    const fetchMock = vi.fn(async () => jsonResponse({ results: [] }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchCatalog();
    expect(fetchMock).toHaveBeenCalledWith(
      "https://erp.example.com/api/products/products/?visible_on_website=true&is_active=true",
      expect.anything(),
    );
  });

  it("injects the bearer token when one is configured", async () => {
    vi.stubEnv("VITE_API_TOKEN", "tok-123");
    const fetchMock = vi.fn(async () => jsonResponse({ results: [] }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchCatalog();
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>)["Authorization"]).toBe(
      "Bearer tok-123",
    );
  });

  it("retries once on a 502 then returns the recovered catalog", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (fetchMock.mock.calls.length === 1) return jsonResponse({}, 502);
      void url;
      return jsonResponse({
        results: [
          { id: 9, name: "Recovered", base_price: "10.00", currency: "USD" },
        ],
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const catalog = await fetchCatalog();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(catalog[0].title).toBe("Recovered");
  });

  it("in development, gives up after the retry and falls back to the demo catalog", async () => {
    vi.stubEnv("DEV", true);
    const fetchMock = vi.fn(async () => jsonResponse({}, 502));
    vi.stubGlobal("fetch", fetchMock);
    const catalog = await fetchCatalog();
    expect(fetchMock).toHaveBeenCalledTimes(2); // 1 + 1 retry
    expect(catalog.length).toBeGreaterThan(0);
  });

  it("in a production build, an unreachable catalog is an error, never demo data", async () => {
    vi.stubEnv("DEV", false);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({}, 502)),
    );
    await expect(fetchCatalog()).rejects.toMatchObject({ name: "ApiError" });
  });

  it("VITE_DEMO_CATALOG=1 opts a production build into the demo catalog", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_DEMO_CATALOG", "1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({}, 502)),
    );
    expect((await fetchCatalog()).length).toBeGreaterThan(0);
  });

  it("an empty catalog stays empty", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ results: [] })),
    );
    expect(await fetchCatalog()).toEqual([]);
  });

  it("maps real ERP fields and invents nothing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          results: [
            {
              id: "4f1c9a1e-0000-4000-8000-000000000001",
              title: "Servo",
              part_number: "SV-1",
              brand_name: "Apex",
              price: "389.00",
              stock_quantity: 42,
              stock_status: "in_stock",
              summary: "Quiet torque.",
              category_name: "Motion",
              image_url: null,
            },
            {
              id: "4f1c9a1e-0000-4000-8000-000000000002",
              title: "Unpriced",
              price: null,
            },
          ],
        }),
      ),
    );
    const [servo, unpriced] = await fetchCatalog();
    expect(servo).toMatchObject({
      title: "Servo",
      sku: "SV-1",
      base_price: "389.00",
      stock_quantity: 42,
      description: "Quiet torque.",
      category_name: "Motion",
      image_url: null,
    });
    expect(unpriced.base_price).toBeNull();
    expect(unpriced.stock_quantity).toBe(0);
    expect(unpriced.stock_status).toBe("out_of_stock");
    expect(unpriced.brand_name).toBe("");
  });

  it("surfaces 401 as an ApiError (login redirect) before falling back", async () => {
    // A 401 must never silently degrade to demo data: the UI needs to
    // know the session is invalid. fetchCatalog rethrows; callers decide.
    const fetchMock = vi.fn(async () =>
      jsonResponse({ detail: "invalid" }, 401),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchCatalog({ strict: true })).rejects.toMatchObject({
      status: 401,
    });
  });
});

describe("fetchProductById", () => {
  it("fetches the product's own detail endpoint by UUID", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://erp.example.com/api");
    const id = "d76e7b1b-589a-4e0f-8e10-6b57a8294f50";
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        id,
        title: "Servo",
        price: "389.00",
        variants: [
          {
            id: "v1",
            sku: "SV-1-A",
            retail_price: "389.00",
            sale_price: null,
            stock: 4,
            is_active: true,
          },
          {
            id: "v2",
            sku: "SV-1-B",
            retail_price: null,
            sale_price: null,
            stock: 1,
            is_active: true,
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const product = await fetchProductById(id);
    expect(fetchMock).toHaveBeenCalledWith(
      `https://erp.example.com/api/products/products/${id}/`,
      expect.anything(),
    );
    expect(product?.base_price).toBe("389.00");
    expect(product?.variants.map((v) => v.sku)).toEqual(["SV-1-A"]); // unpriced variant dropped
  });

  it("returns undefined for a missing or hidden product", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "Not found." }, 404)),
    );
    expect(await fetchProductById("nope")).toBeUndefined();
  });
});

describe("submitOrder (visible failure, F-004)", () => {
  it("throws OrderSubmitError when the backend is unreachable", async () => {
    const fetchMock = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(submitOrder(payload())).rejects.toBeInstanceOf(
      OrderSubmitError,
    );
  });

  it("does not retry a failed POST (no double-submit)", async () => {
    vi.stubEnv("VITE_API_TOKEN", "tok-env");
    const fetchMock = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(submitOrder(payload())).rejects.toBeInstanceOf(
      OrderSubmitError,
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("throws OrderSubmitError on a 500 with the status attached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({}, 500)),
    );
    const err = await submitOrder(payload()).catch((e) => e);
    expect(err).toBeInstanceOf(OrderSubmitError);
    expect((err as ApiError).status).toBe(500);
  });

  it("throws OrderSubmitError on a 401 (no silent confirmation)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "auth" }, 401)),
    );
    await expect(submitOrder(payload())).rejects.toMatchObject({ status: 401 });
  });

  it("throws when the server confirms without an order number", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ total_amount: "39.98" }, 200)),
    );
    await expect(submitOrder(payload())).rejects.toBeInstanceOf(
      OrderSubmitError,
    );
  });

  it("maps a real server confirmation (order number comes from the ERP)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          order_number: "SO-12345",
          total_amount: "39.98",
          currency: "USD",
          status: "CONFIRMED",
          payment_status: "SETTLED",
        }),
      ),
    );
    const confirmation = await submitOrder(payload());
    expect(confirmation.order_number).toBe("SO-12345");
    expect(confirmation.total_amount).toBe("39.98");
  });

  it("posts the cart lines (variant + quantity) and no client prices or totals", async () => {
    vi.stubEnv("VITE_API_TOKEN", "tok-env");
    const fetchMock = vi.fn(async () =>
      jsonResponse({ order_number: "SO-1", total_amount: "1.00" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await submitOrder(payload());
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as RequestInit).body as string,
    );
    expect(body.lines).toEqual([{ variant: "v-1", quantity: 2 }]);
    expect(body).not.toHaveProperty("items");
    expect(JSON.stringify(body)).not.toMatch(/price|total/);
  });

  it("maps the server's priced lines and totals", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          {
            id: "o-1",
            order_number: "SO-00042",
            status: "pending",
            payment_status: "unpaid",
            currency: "USD",
            lines: [
              {
                variant: "v-1",
                sku: "SP-1",
                item_name: "Panel",
                quantity: 2,
                unit_price: "250.00",
                line_total: "500.00",
                line_tax: "0.00",
              },
            ],
            subtotal: "500.00",
            tax_total: "0.00",
            shipping_total: "0.00",
            discount_total: "0.00",
            grand_total: "500.00",
            total_amount: "500.00",
            created_at: "2026-11-03T12:00:00Z",
          },
          201,
        ),
      ),
    );
    const confirmation = await submitOrder(payload());
    expect(confirmation).toMatchObject({
      order_number: "SO-00042",
      total_amount: "500.00",
      subtotal: "500.00",
      tax_total: "0.00",
      status: "pending",
      payment_status: "unpaid",
    });
    expect(confirmation.lines).toEqual([
      {
        variant: "v-1",
        sku: "SP-1",
        item_name: "Panel",
        quantity: 2,
        unit_price: "250.00",
        line_total: "500.00",
      },
    ]);
  });

  it("surfaces per-line refusals by code with shopper copy", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          {
            code: "invalid_lines",
            detail: "Some lines cannot be ordered; nothing was placed.",
            lines: [
              {
                index: 0,
                code: "insufficient_stock",
                detail: "x",
                variant: "v-1",
                product: null,
                available: 3,
              },
              {
                index: 1,
                code: "out_of_stock",
                detail: "x",
                variant: "v-2",
                product: null,
                available: 0,
              },
              {
                index: 2,
                code: "variant_required",
                detail: "x",
                variant: null,
                product: "p-3",
              },
            ],
          },
          400,
        ),
      ),
    );
    const err = (await submitOrder(payload()).catch(
      (e) => e,
    )) as OrderSubmitError;
    expect(err).toBeInstanceOf(OrderSubmitError);
    expect(err.status).toBe(400);
    expect(err.code).toBe("invalid_lines");
    expect(err.message).toMatch(/no order was placed/i);
    expect(err.lineErrors.map((e) => [e.index, e.code])).toEqual([
      [0, "insufficient_stock"],
      [1, "out_of_stock"],
      [2, "variant_required"],
    ]);
    expect(err.lineErrors[0].available).toBe(3);
    expect(err.lineErrors[0].message).toMatch(/only 3 in stock/i);
    expect(err.lineErrors[1].message).toMatch(/out of stock/i);
    expect(err.lineErrors[2].message).toMatch(/choose one/i);
  });

  it("maps request-level refusal codes, not the ERP's message", async () => {
    for (const [code, status, pattern] of [
      ["lines_required", 400, /cart is empty/i],
      ["too_many_lines", 400, /at most 50 lines/i],
      ["guest_checkout_unavailable", 503, /not available right now/i],
    ] as const) {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () =>
          jsonResponse({ code, detail: "server words" }, status),
        ),
      );
      const err = (await submitOrder(payload()).catch(
        (e) => e,
      )) as OrderSubmitError;
      expect(err.code).toBe(code);
      expect(err.message).toMatch(pattern);
      expect(err.message).not.toContain("server words");
      expect(err.lineErrors).toEqual([]);
    }
  });

  it("never says 'no order was placed' when the POST may have gone through", async () => {
    const cases: Array<() => Promise<Response>> = [
      async () => {
        throw new DOMException("The operation timed out.", "TimeoutError");
      },
      async () => {
        throw new TypeError("fetch failed");
      },
      async () => jsonResponse({ detail: "boom" }, 500),
      async () => new Response("", { status: 502 }),
      async () => new Response("not json", { status: 201 }),
      async () => jsonResponse({ total_amount: "1.00" }, 201),
    ];
    for (const impl of cases) {
      vi.stubGlobal("fetch", vi.fn(impl));
      const err = (await submitOrder(payload()).catch(
        (e) => e,
      )) as OrderSubmitError;
      expect(err).toBeInstanceOf(OrderSubmitError);
      expect(err.unconfirmed).toBe(true);
      expect(err.message).toBe(UNCONFIRMED_ORDER_MESSAGE);
      expect(err.message).toMatch(
        /submit the same cart again from this browser/i,
      );
      expect(err.message).not.toMatch(/no order was placed/i);
    }
  });

  it("keeps the definite wording for real refusals (4xx with a body)", async () => {
    for (const [status, body, pattern] of [
      [400, { guest_email: ["required"] }, /enter your email/i],
      [
        400,
        { code: "order_too_large", detail: "x" },
        /too large to place online/i,
      ],
      [
        503,
        { code: "guest_checkout_unavailable", error: "x" },
        /not available right now/i,
      ],
    ] as const) {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => jsonResponse(body, status)),
      );
      const err = (await submitOrder(payload()).catch(
        (e) => e,
      )) as OrderSubmitError;
      expect(err.unconfirmed).toBe(false);
      expect(err.message).toMatch(pattern);
      expect(err.message).toMatch(/no order was placed/i);
    }
  });

  it("maps an order_too_large line", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          {
            code: "invalid_lines",
            detail: "x",
            lines: [
              {
                index: 0,
                code: "order_too_large",
                detail: "x",
                variant: "v",
                product: null,
              },
            ],
          },
          400,
        ),
      ),
    );
    const err = (await submitOrder(payload()).catch(
      (e) => e,
    )) as OrderSubmitError;
    expect(err.lineErrors[0].message).toMatch(/too large for an online order/i);
  });

  it("sends the coupon code when one is applied", async () => {
    vi.stubEnv("VITE_API_TOKEN", "tok-env");
    const fetchMock = vi.fn(async () =>
      jsonResponse({ order_number: "SO-1", total_amount: "1.00" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await submitOrder({ ...payload(), coupon_code: "SAVE10" });
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(init.body as string).coupon_code).toBe("SAVE10");
  });

  it("posts to the orders collection (not the DRF API root)", async () => {
    vi.stubEnv("VITE_API_TOKEN", "tok-env");
    const fetchMock = vi.fn(async () =>
      jsonResponse({ order_number: "SO-1", total_amount: "1.00" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await submitOrder(payload());
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://localhost:8000/api/orders/orders/",
    );
  });
});

describe("storefront guest token (F-037)", () => {
  function stubLocalStorage(): Map<string, string> {
    const store = new Map<string, string>();
    const localStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
    vi.stubGlobal("localStorage", localStorage);
    // getApiToken()/fetchStorefrontToken() read window.localStorage.
    vi.stubGlobal("window", { localStorage });
    return store;
  }

  function fetchMockWithToken(tokenUrl: string, tokenBody: unknown) {
    return vi.fn(async (url: string) => {
      if (url === tokenUrl) return jsonResponse(tokenBody);
      return jsonResponse({ order_number: "SO-9", total_amount: "1.00" });
    });
  }

  it("fetches a guest token, caches it, and sends it on the order POST", async () => {
    const store = stubLocalStorage();
    const tokenUrl = "http://localhost:8000/api/core/auth/storefront-token/";
    const fetchMock = fetchMockWithToken(tokenUrl, { token: "guest-tok" });
    vi.stubGlobal("fetch", fetchMock);
    const confirmation = await submitOrder(payload());
    expect(confirmation.order_number).toBe("SO-9");
    expect(fetchMock.mock.calls[0][0]).toBe(tokenUrl);
    expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");
    expect(store.get("golden_api_token")).toBe("guest-tok");
    const init = fetchMock.mock.calls[1][1] as RequestInit;
    expect((init.headers as Record<string, string>)["Authorization"]).toBe(
      "Bearer guest-tok",
    );
  });

  it("prefers an env token over fetching a guest token", async () => {
    stubLocalStorage();
    vi.stubEnv("VITE_API_TOKEN", "tok-env");
    const fetchMock = vi.fn(async () =>
      jsonResponse({ order_number: "SO-1", total_amount: "1.00" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await submitOrder(payload());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>)["Authorization"]).toBe(
      "Bearer tok-env",
    );
  });

  it("proceeds anonymously when the token endpoint fails", async () => {
    stubLocalStorage();
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("storefront-token"))
        return jsonResponse({ error: "nope" }, 500);
      return jsonResponse({ order_number: "SO-2", total_amount: "2.00" });
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirmation = await submitOrder(payload());
    expect(confirmation.order_number).toBe("SO-2");
    const init = fetchMock.mock.calls[1][1] as RequestInit;
    expect(
      (init.headers as Record<string, string>)["Authorization"],
    ).toBeUndefined();
  });
});

describe("checkoutLines", () => {
  const product = (variants: { id: string }[]): Product =>
    ({ id: "p-1", variants }) as unknown as Product;

  it("sends the chosen variant", () => {
    expect(
      checkoutLines([{ product: product([]), variantId: "v-9", quantity: 3 }]),
    ).toEqual([{ variant: "v-9", quantity: 3 }]);
  });

  it("falls back to the product's only variant", () => {
    expect(
      checkoutLines([
        {
          product: product([{ id: "v-1" }]),
          variantId: undefined,
          quantity: 1,
        },
      ]),
    ).toEqual([{ variant: "v-1", quantity: 1 }]);
  });

  it("sends the product when the variant is unknown (the ERP resolves or refuses)", () => {
    expect(
      checkoutLines([
        {
          product: product([{ id: "v-1" }, { id: "v-2" }]),
          variantId: undefined,
          quantity: 2,
        },
        { product: product([]), variantId: undefined, quantity: 1 },
      ]),
    ).toEqual([
      { product: "p-1", quantity: 2 },
      { product: "p-1", quantity: 1 },
    ]);
  });

  it("never sends a price", () => {
    const lines = checkoutLines([
      { product: product([{ id: "v-1" }]), variantId: "v-1", quantity: 2.7 },
    ]);
    expect(lines).toEqual([{ variant: "v-1", quantity: 2 }]);
  });
});

describe("checkout idempotency key (OP #3431)", () => {
  function stubSessionStorage() {
    clearCheckoutKey();
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
  }

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("sends the key as an Idempotency-Key header", async () => {
    vi.stubEnv("VITE_API_TOKEN", "tok-env");
    const fetchMock = vi.fn(async () =>
      jsonResponse({ order_number: "SO-1", total_amount: "1.00" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await submitOrder(payload(), "key-0123456789abcdef");
    const headers = (fetchMock.mock.calls[0][1] as RequestInit)
      .headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBe("key-0123456789abcdef");
  });

  it("reuses the key for a retry of the same cart and email", () => {
    stubSessionStorage();
    const first = checkoutKeyFor(payload());
    expect(first.length).toBeGreaterThanOrEqual(16);
    const retry = { ...payload(), email: "T@Example.com ", notes: "edited" };
    expect(checkoutKeyFor(retry)).toBe(first);
  });

  it("starts a new key when the cart changes or after a confirmed order", () => {
    stubSessionStorage();
    const first = checkoutKeyFor(payload());
    const bigger = { ...payload(), lines: [{ variant: "v-1", quantity: 3 }] };
    const second = checkoutKeyFor(bigger);
    expect(second).not.toBe(first);
    clearCheckoutKey();
    expect(checkoutKeyFor(bigger)).not.toBe(second);
  });

  it("matches the server's key format", () => {
    stubSessionStorage();
    expect(checkoutKeyFor(payload())).toMatch(/^[A-Za-z0-9._:-]{16,200}$/);
  });

  it("a 409 for a reused key never says no order was placed", async () => {
    vi.stubEnv("VITE_API_TOKEN", "tok-env");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({ code: "idempotency_key_reused", detail: "x" }, 409),
      ),
    );
    const err = (await submitOrder(payload(), "key-0123456789abcdef").catch(
      (e) => e,
    )) as OrderSubmitError;
    expect(err.code).toBe("idempotency_key_reused");
    expect(err.unconfirmed).toBe(false);
    expect(err.message).toMatch(/already placed/i);
    expect(err.message).not.toMatch(/no order was placed/i);
  });

  it("a throttled retry doesn't claim nothing was placed", async () => {
    vi.stubEnv("VITE_API_TOKEN", "tok-env");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "throttled" }, 429)),
    );
    const err = (await submitOrder(payload(), "key-0123456789abcdef").catch(
      (e) => e,
    )) as OrderSubmitError;
    expect(err.message).toMatch(/too many orders/i);
    expect(err.message).not.toMatch(/no order was placed/i);
  });

  it("a key past its day is replaced", () => {
    stubSessionStorage();
    const first = checkoutKeyFor(payload());
    const realNow = Date.now;
    vi.spyOn(Date, "now").mockImplementation(
      () => realNow() + 24 * 60 * 60 * 1000,
    );
    try {
      expect(checkoutKeyFor(payload())).not.toBe(first);
    } finally {
      vi.restoreAllMocks();
    }
  });

  it("without storage the key still holds for this page", () => {
    clearCheckoutKey();
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    });
    const first = checkoutKeyFor(payload());
    expect(first).not.toBe("");
    expect(checkoutKeyFor(payload())).toBe(first);
  });
});
