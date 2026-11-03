import type { CartItem, Product, ProductTierPrice } from "./api-client";

/**
 * Pure cart-math helpers (testing-plan §6 unit: cart math, tier prices,
 * coupon add/remove). Extracted from logic previously inline in
 * `cart-drawer.tsx` / `products/[id]` / `cart/index.tsx` so it is unit
 * testable without a Qwik runtime. Money is integer cents throughout to
 * avoid binary-float drift; only the boundaries convert to "xx.yy" strings.
 */

export function toCents(amount: string | number): number {
  const n = typeof amount === "string" ? parseFloat(amount) : amount;
  if (!Number.isFinite(n)) return 0;
  return Math.round(n * 100);
}

export function centsToAmount(cents: number): string {
  return (cents / 100).toFixed(2);
}

export function resolveTierPrice(
  basePrice: string,
  tiers: ProductTierPrice[] | undefined,
  quantity: number,
): string {
  if (!tiers || tiers.length === 0) return basePrice;
  const sorted = [...tiers].sort((a, b) => b.min_quantity - a.min_quantity);
  return sorted.find((t) => quantity >= t.min_quantity)?.unit_price ?? basePrice;
}

export function lineTotalCents(item: Pick<CartItem, "quantity" | "selectedPrice">): number {
  return toCents(item.selectedPrice) * Math.max(0, Math.floor(item.quantity));
}

export function cartSubtotalCents(items: Pick<CartItem, "quantity" | "selectedPrice">[]): number {
  return items.reduce((sum, item) => sum + lineTotalCents(item), 0);
}

function retier(item: CartItem): CartItem {
  return {
    ...item,
    selectedPrice: resolveTierPrice(
      // Only priced products reach the cart (isOrderable); keep the line's price otherwise.
      item.product.base_price ?? item.selectedPrice,
      item.product.tiered_pricing,
      item.quantity,
    ),
  };
}

export function addOrMergeItem(items: CartItem[], incoming: CartItem): CartItem[] {
  const idx = items.findIndex(
    (i) => i.product.id === incoming.product.id && i.variantId === incoming.variantId,
  );
  if (idx === -1) return [...items, { ...incoming }];
  const merged = items.map((line, i) =>
    i === idx ? { ...line, quantity: line.quantity + incoming.quantity } : line,
  );
  merged[idx] = retier(merged[idx]);
  return merged;
}

export function updateItemQuantity(items: CartItem[], index: number, delta: number): CartItem[] {
  const line = items[index];
  if (!line) return items;
  const nextQty = line.quantity + delta;
  if (nextQty <= 0) return removeItemAt(items, index);
  const next = items.map((l, i) => (i === index ? { ...l, quantity: nextQty } : l));
  next[index] = retier(next[index]);
  return next;
}

export function removeItemAt(items: CartItem[], index: number): CartItem[] {
  return items.filter((_, i) => i !== index);
}

/** A SKU is orderable only when it is not flagged out-of-stock AND has units. */
export function isOrderable(
  product: Pick<Product, "stock_status" | "stock_quantity" | "base_price">,
): boolean {
  // No price in the ERP means "Price on request" — never a cart line.
  if (product.base_price == null) return false;
  return product.stock_status !== "out_of_stock" && product.stock_quantity > 0;
}

export interface CouponDef {
  code: string;
  kind: "percent" | "flat";
  /** percent 0-100, or flat whole-currency-units (dollars). */
  value: number;
  minSubtotalCents?: number;
  expiresAt?: string;
  description: string;
}

/** Demo coupon book for the offline storefront (no backend coupon endpoint). */
export const DEMO_COUPONS: CouponDef[] = [
  { code: "SAVE10", kind: "percent", value: 10, description: "10% off the order subtotal" },
  {
    code: "FLAT50",
    kind: "flat",
    value: 50,
    minSubtotalCents: 20000,
    description: "$50 off orders of $200 or more",
  },
];

export type CouponValidation =
  | { ok: true; coupon: CouponDef }
  | { ok: false; error: string };

export function validateCoupon(
  rawCode: string,
  subtotalCents: number,
  book: CouponDef[] = DEMO_COUPONS,
  now: Date = new Date(),
): CouponValidation {
  const code = rawCode.trim().toUpperCase();
  const coupon = book.find((c) => c.code.toUpperCase() === code);
  if (!coupon) return { ok: false, error: `Coupon "${rawCode.trim()}" is not recognized.` };
  if (coupon.expiresAt && new Date(coupon.expiresAt).getTime() <= now.getTime()) {
    return { ok: false, error: `Coupon "${coupon.code}" has expired.` };
  }
  if (coupon.minSubtotalCents !== undefined && subtotalCents < coupon.minSubtotalCents) {
    return {
      ok: false,
      error: `Coupon "${coupon.code}" needs a subtotal of at least $${centsToAmount(coupon.minSubtotalCents)}.`,
    };
  }
  return { ok: true, coupon };
}

/** Discount in cents, capped at the subtotal so totals never go negative. */
export function discountCentsForCoupon(subtotalCents: number, coupon: CouponDef): number {
  const raw =
    coupon.kind === "percent"
      ? Math.round((subtotalCents * coupon.value) / 100)
      : toCents(coupon.value);
  return Math.max(0, Math.min(subtotalCents, raw));
}
