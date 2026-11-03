import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  OrderSubmitError,
  __resetStorefrontTokenForTests,
  fetchCatalog,
  fetchProductById,
  getApiBaseUrl,
  getBackendOrigin,
  submitOrder,
  type CheckoutPayload,
} from "../../src/lib/api-client";

function payload(): CheckoutPayload {
  return {
    customer_name: "Test",
    company_name: "Test Co",
    email: "t@example.com",
    shipping_address: "1 Test Way",
    payment_method: "credit_card",
    items: [{ product_id: 1, quantity: 2, unit_price: "19.99" }],
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
