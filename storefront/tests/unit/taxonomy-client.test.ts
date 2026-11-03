import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  fetchBrandBySlug,
  fetchCategories,
  fetchCategoryBySlug,
  fetchProductPage,
} from "../../src/lib/api-client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const CATEGORY_ROW = {
  id: "c1",
  slug: "lithium",
  title: "Lithium",
  parent_slug: "batteries",
  description: "Packs.",
  short_description: "",
  body_copy: "## About\n\nCopy.",
  image_url: null,
  seo_title: "Lithium batteries",
  seo_meta_description: "",
  ancestors: [
    { id: "r", slug: "power", title: "Power" },
    { id: "b", slug: "batteries", title: "Batteries" },
  ],
  children: [],
};

describe("category and brand reads", () => {
  it("fetches one category by slug and maps tree + SEO fields", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://erp.example.com/api");
    const fetchMock = vi.fn(async () =>
      jsonResponse({ count: 1, next: null, results: [CATEGORY_ROW] }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const cat = await fetchCategoryBySlug("lithium");
    expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toBe(
      "https://erp.example.com/api/products/categories/?slug=lithium&page=1",
    );
    expect(cat?.ancestors.map((a) => a.slug)).toEqual(["power", "batteries"]);
    expect(cat?.seo_title).toBe("Lithium batteries");
    expect(cat?.seo_meta_description).toBeNull(); // "" means unset, so the head falls back
    expect(cat?.body_copy).toBe("## About\n\nCopy.");
  });

  it("returns undefined for an unknown slug (the route answers 404)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ count: 0, next: null, results: [] })),
    );
    expect(await fetchBrandBySlug("nope")).toBeUndefined();
  });

  it("follows pages for the full category list", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ count: 2, next: "x", results: [CATEGORY_ROW] }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          count: 2,
          next: null,
          results: [{ ...CATEGORY_ROW, id: "c2", slug: "lead" }],
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    expect((await fetchCategories()).map((c) => c.slug)).toEqual([
      "lithium",
      "lead",
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("never serves demo categories in a production build", async () => {
    vi.stubEnv("DEV", false);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("offline");
      }),
    );
    await expect(fetchCategoryBySlug("power-systems")).rejects.toBeInstanceOf(
      ApiError,
    );
  });

  it("serves demo categories derived from the demo catalog on the dev server", async () => {
    vi.stubEnv("DEV", true);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("offline");
      }),
    );
    const cat = await fetchCategoryBySlug("power-systems");
    expect(cat?.title).toBe("Power Systems");
    const page = await fetchProductPage({ categorySlug: "power-systems" });
    expect(page.products.length).toBeGreaterThan(0);
    expect(
      page.products.every((p) => p.category_slug === "power-systems"),
    ).toBe(true);
  });
});

describe("fetchProductPage", () => {
  it("asks for the public catalog filtered by category subtree and page", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://erp.example.com/api");
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        count: 30,
        next: "https://erp.example.com/api/products/products/?page=3",
        results: [
          {
            id: "p1",
            title: "Cell",
            category_slug: "lithium",
            seo_title: "Cell, 100 Ah",
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const page = await fetchProductPage({ categorySlug: "lithium", page: 2 });
    const url = new URL(String((fetchMock.mock.calls[0] as unknown[])[0]));
    expect(url.searchParams.get("category_slug")).toBe("lithium");
    expect(url.searchParams.get("page")).toBe("2");
    expect(url.searchParams.get("visible_on_website")).toBe("true");
    expect(page).toMatchObject({ count: 30, page: 2, hasNext: true });
    expect(page.products[0].seo_title).toBe("Cell, 100 Ah");
  });

  it("treats a page past the end as empty, not an outage", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "Invalid page." }, 404)),
    );
    expect(
      await fetchProductPage({ brandSlug: "acme", page: 9 }),
    ).toMatchObject({
      products: [],
      hasNext: false,
    });
  });
});

describe("mapApiProduct long_description", () => {
  it("falls through an empty description instead of keeping it", async () => {
    const { mapApiProduct } = await import("../../src/lib/api-client");
    const p = mapApiProduct({
      id: 1,
      title: "X",
      description: "",
      short_description: "Short one.",
    });
    expect(p.long_description).toBe("Short one.");
  });

  it("an empty description plus a truncated API summary yields no mid-sentence meta", async () => {
    const { mapApiProduct } = await import("../../src/lib/api-client");
    const { buildSeoHead } = await import("../../src/lib/seo-head");
    const p = mapApiProduct({
      id: 1,
      title: "X",
      description: "",
      short_description: "",
      summary: "A long excerpt the ERP cut at three hundred characters and…",
    });
    const head = buildSeoHead({
      name: p.title,
      description: p.long_description,
      canonical: "/products/1/",
      site: { name: "", url: "" },
    });
    expect(head.meta?.find((m) => m.name === "description")).toBeUndefined();
  });
});
