import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createPortalClient,
  formatDate,
  formatMoney,
  humanizeStatus,
  PortalError,
  portalUrl,
  type FetchLike,
} from "../../src/lib/portal-client";
import { createAuthProvider } from "../../src/lib/auth";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const tokenAuth = (token: string | null) => ({
  getAccessToken: async () => token,
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("portal client token attachment", () => {
  it("sends the signed-in customer's bearer token to /api/v1/portal/customer/…", async () => {
    const fetchImpl = vi.fn<FetchLike>(async () => jsonResponse([]));
    await createPortalClient(tokenAuth("jwt-abc"), fetchImpl).orders();

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://localhost:8000/api/v1/portal/customer/orders/");
    expect((init?.headers as Record<string, string>).Authorization).toBe(
      "Bearer jwt-abc",
    );
  });

  it("uses the token from a real adapter session", async () => {
    const auth = createAuthProvider({
      provider: "mock",
      supabaseUrl: "",
      supabaseAnonKey: "",
      dev: true,
    });
    const session = await auth.signIn("pat@example.com", "pw-123456");
    const fetchImpl = vi.fn<FetchLike>(async () => jsonResponse({ orders: 1 }));
    await createPortalClient(auth, fetchImpl).moduleCounts();
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe(
      "http://localhost:8000/api/v1/portal/customer/module-counts/",
    );
    expect((init?.headers as Record<string, string>).Authorization).toBe(
      `Bearer ${session.accessToken}`,
    );
  });

  it("follows VITE_API_BASE_URL", () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://erp.example.com/api/");
    expect(portalUrl("orders/abc/")).toBe(
      "https://erp.example.com/api/v1/portal/customer/orders/abc/",
    );
  });

  it("makes no request at all when signed out", async () => {
    const fetchImpl = vi.fn<FetchLike>();
    await expect(
      createPortalClient(tokenAuth(null), fetchImpl).profile(),
    ).rejects.toMatchObject({
      kind: "unauthorized",
    });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("portal client errors (never fabricates)", () => {
  it.each([401, 403])("maps HTTP %i to unauthorized", async (status) => {
    const client = createPortalClient(tokenAuth("t"), async () =>
      jsonResponse({ detail: "no" }, status),
    );
    await expect(client.orders()).rejects.toMatchObject({
      kind: "unauthorized",
      status,
    });
  });

  it("maps 404 to not_found", async () => {
    const client = createPortalClient(tokenAuth("t"), async () =>
      jsonResponse({}, 404),
    );
    await expect(client.order("missing")).rejects.toMatchObject({
      kind: "not_found",
    });
  });

  it("surfaces server and network failures instead of returning data", async () => {
    const down = createPortalClient(tokenAuth("t"), async () =>
      jsonResponse({}, 500),
    );
    await expect(down.orders()).rejects.toBeInstanceOf(PortalError);
    const offline = createPortalClient(tokenAuth("t"), async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(offline.orders()).rejects.toMatchObject({
      kind: "unavailable",
      status: 0,
    });
  });

  it("accepts a paginated order list and rejects an unexpected shape", async () => {
    const paged = createPortalClient(tokenAuth("t"), async () =>
      jsonResponse({ results: [{ id: "1" }] }),
    );
    expect(await paged.orders()).toEqual([{ id: "1" }]);
    const odd = createPortalClient(tokenAuth("t"), async () =>
      jsonResponse({ nope: true }),
    );
    await expect(odd.orders()).rejects.toBeInstanceOf(PortalError);
  });

  it("encodes the order id into the path", async () => {
    const fetchImpl = vi.fn<FetchLike>(async () => jsonResponse({ id: "a/b" }));
    await createPortalClient(tokenAuth("t"), fetchImpl).order("a/b");
    expect(fetchImpl.mock.calls[0][0]).toBe(
      "http://localhost:8000/api/v1/portal/customer/orders/a%2Fb/",
    );
  });
});

describe("formatting helpers", () => {
  it("humanizes ERP status values", () => {
    expect(humanizeStatus("awaiting_payment")).toBe("Awaiting payment");
    expect(humanizeStatus("")).toBe("—");
    expect(humanizeStatus("cancelled")).toBe("Canceled");
  });

  it("formats bare dates in UTC so they never slip a day", () => {
    expect(formatDate("2026-10-01", "en-US")).toBe("Oct 1, 2026");
    expect(formatDate(null)).toBe("—");
    expect(formatDate("not a date")).toBe("not a date");
  });

  it("formats money with a known currency and without a guessed one", () => {
    expect(formatMoney("1234.5", "USD", "en-US")).toBe("$1,234.50");
    expect(formatMoney("1234.5", null, "en-US")).toBe("1,234.50");
    expect(formatMoney(null, "USD")).toBe("—");
  });
});
