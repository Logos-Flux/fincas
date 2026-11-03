import { describe, expect, it } from "vitest";
import {
  addOrMergeItem,
  cartSubtotalCents,
  centsToAmount,
  discountCentsForCoupon,
  DEMO_COUPONS,
  isOrderable,
  lineTotalCents,
  removeItemAt,
  resolveTierPrice,
  toCents,
  updateItemQuantity,
  validateCoupon,
  type CouponDef,
} from "../../src/lib/cart-math";
import type { CartItem, Product } from "../../src/lib/api-client";

function product(overrides: Partial<Product> = {}): Product {
  return {
    id: 1,
    title: "Test Widget",
    slug: "test-widget",
    sku: "TST-001",
    description: "d",
    category_name: "Test",
    brand_name: "TestBrand",
    base_price: "100.00",
    currency: "USD",
    stock_status: "in_stock",
    stock_quantity: 10,
    image_url: "",
    specs: {},
    variants: [],
    tiered_pricing: [
      { min_quantity: 1, unit_price: "100.00" },
      { min_quantity: 5, unit_price: "90.00", discount_percentage: "10%" },
      { min_quantity: 20, unit_price: "75.00", discount_percentage: "25%" },
    ],
    ...overrides,
  };
}

function item(p: Product, quantity: number, selectedPrice?: string): CartItem {
  return { product: p, quantity, selectedPrice: selectedPrice ?? p.base_price };
}

describe("resolveTierPrice", () => {
  it("returns the base price below the first break", () => {
    expect(resolveTierPrice("100.00", product().tiered_pricing, 3)).toBe(
      "100.00",
    );
  });

  it("resolves the correct break at exact boundaries", () => {
    expect(resolveTierPrice("100.00", product().tiered_pricing, 5)).toBe(
      "90.00",
    );
    expect(resolveTierPrice("100.00", product().tiered_pricing, 20)).toBe(
      "75.00",
    );
  });

  it("holds the deepest break above the top tier", () => {
    expect(resolveTierPrice("100.00", product().tiered_pricing, 500)).toBe(
      "75.00",
    );
  });

  it("handles unsorted tiers and an empty tier list", () => {
    const unsorted = [
      { min_quantity: 20, unit_price: "75.00" },
      { min_quantity: 1, unit_price: "100.00" },
      { min_quantity: 5, unit_price: "90.00" },
    ];
    expect(resolveTierPrice("100.00", unsorted, 7)).toBe("90.00");
    expect(resolveTierPrice("100.00", [], 7)).toBe("100.00");
  });
});

describe("money helpers", () => {
  it("converts decimal strings to exact integer cents", () => {
    expect(toCents("19.99")).toBe(1999);
    expect(toCents("0.07")).toBe(7);
    expect(centsToAmount(5498)).toBe("54.98");
  });

  it("sums line totals without float drift", () => {
    // 2 x 19.99 + 3 x 5.00 = 54.98 (parseFloat math would risk 54.98000000001)
    const p = product({ base_price: "19.99", tiered_pricing: [] });
    const q = product({ base_price: "5.00", tiered_pricing: [] });
    expect(lineTotalCents(item(p, 2, "19.99"))).toBe(3998);
    expect(cartSubtotalCents([item(p, 2, "19.99"), item(q, 3, "5.00")])).toBe(
      5498,
    );
  });
});

describe("quantity edits", () => {
  it("incrementing into a tier recomputes the unit price", () => {
    const p = product();
    const after = updateItemQuantity([item(p, 4, "100.00")], 0, 1);
    expect(after[0].quantity).toBe(5);
    expect(after[0].selectedPrice).toBe("90.00");
  });

  it("decrementing out of a tier restores the higher unit price", () => {
    const p = product();
    const after = updateItemQuantity([item(p, 5, "90.00")], 0, -1);
    expect(after[0].quantity).toBe(4);
    expect(after[0].selectedPrice).toBe("100.00");
  });

  it("decrementing to zero removes the line", () => {
    const p = product();
    expect(updateItemQuantity([item(p, 1, "100.00")], 0, -1)).toEqual([]);
  });

  it("does not mutate the input array", () => {
    const p = product();
    const before = [item(p, 1, "100.00")];
    updateItemQuantity(before, 0, 1);
    expect(before[0].quantity).toBe(1);
  });

  it("removeItemAt drops only the targeted line", () => {
    const p = product();
    const lines = [item(p, 1, "100.00"), item(p, 2, "90.00")];
    const after = removeItemAt(lines, 0);
    expect(after).toHaveLength(1);
    expect(after[0].quantity).toBe(2);
  });

  it("addOrMergeItem merges same product+variant, appends otherwise", () => {
    const p = product();
    const merged = addOrMergeItem([item(p, 1, "100.00")], {
      product: p,
      quantity: 2,
      selectedPrice: "90.00",
    });
    expect(merged).toHaveLength(1);
    expect(merged[0].quantity).toBe(3);
    const otherVariant = addOrMergeItem(merged, {
      product: p,
      variantId: 9,
      quantity: 1,
      selectedPrice: "100.00",
    });
    expect(otherVariant).toHaveLength(2);
  });
});

describe("coupons", () => {
  const pct: CouponDef = {
    code: "SAVE10",
    kind: "percent",
    value: 10,
    description: "10% off",
  };

  it("applies a percent coupon to the subtotal", () => {
    expect(discountCentsForCoupon(84900, pct)).toBe(8490);
  });

  it("caps a flat coupon at the subtotal (no negative totals)", () => {
    const flat: CouponDef = {
      code: "BIG",
      kind: "flat",
      value: 500,
      description: "$500 off",
    };
    expect(discountCentsForCoupon(10000, flat)).toBe(10000);
  });

  it("rejects unknown codes with a message", () => {
    const res = validateCoupon("NOPE", 84900, DEMO_COUPONS);
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.error).toMatch(/not recognized/i);
  });

  it("rejects expired coupons, total unchanged", () => {
    const expired: CouponDef = {
      code: "OLD5",
      kind: "percent",
      value: 5,
      expiresAt: "2020-01-01T00:00:00Z",
      description: "expired",
    };
    const res = validateCoupon(
      "old5",
      84900,
      [expired],
      new Date("2026-09-10T00:00:00Z"),
    );
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.error).toMatch(/expired/i);
  });

  it("enforces minimum-subtotal gates", () => {
    const gated: CouponDef = {
      code: "FLAT50",
      kind: "flat",
      value: 50,
      minSubtotalCents: 20000,
      description: "$50 off $200+",
    };
    const short = validateCoupon("FLAT50", 19999, [gated]);
    expect(short.ok).toBe(false);
    const exact = validateCoupon("FLAT50", 20000, [gated]);
    expect(exact.ok).toBe(true);
  });

  it("matches codes case-insensitively, ignores surrounding whitespace", () => {
    const res = validateCoupon("  save10 ", 84900, [pct]);
    expect(res.ok).toBe(true);
  });
});

describe("isOrderable", () => {
  it("blocks out-of-stock SKUs", () => {
    expect(
      isOrderable(product({ stock_status: "out_of_stock", stock_quantity: 0 })),
    ).toBe(false);
  });

  it("blocks zero-quantity SKUs even when flagged in stock", () => {
    expect(
      isOrderable(product({ stock_status: "in_stock", stock_quantity: 0 })),
    ).toBe(false);
  });

  it("allows in-stock and low-stock SKUs", () => {
    expect(isOrderable(product())).toBe(true);
    expect(
      isOrderable(product({ stock_status: "low_stock", stock_quantity: 2 })),
    ).toBe(true);
  });
});
