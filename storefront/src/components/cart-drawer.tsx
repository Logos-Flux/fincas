import { component$, useContext, $ } from "@builder.io/qwik";
import { Link } from "@builder.io/qwik-city";
import { CartContext } from "../context/cart-context";
import { ProductImage } from "./product-image";
import {
  cartSubtotalCents,
  centsToAmount,
  removeItemAt,
  updateItemQuantity,
} from "../lib/cart-math";

export const CartDrawer = component$(() => {
  const cart = useContext(CartContext);

  const removeItem = $((idx: number) => {
    cart.items = removeItemAt(cart.items, idx);
  });

  const changeQuantity = $((idx: number, delta: number) => {
    cart.items = updateItemQuantity(cart.items, idx, delta);
  });

  const subtotal = centsToAmount(cartSubtotalCents(cart.items));

  if (!cart.isOpen) return null;

  return (
    <div class="fixed inset-0 z-50 overflow-hidden">
      {/* Backdrop */}
      <div
        class="absolute inset-0 bg-black/60 backdrop-blur-sm transition-opacity"
        onClick$={() => (cart.isOpen = false)}
      />

      <div class="fixed inset-y-0 right-0 max-w-full flex pl-10">
        <div
          class="w-screen max-w-md border-l shadow-2xl flex flex-col"
          style={{
            backgroundColor: "var(--color-t-surface, #0f1011)",
            borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
            color: "var(--color-t-text, #d0d6e0)",
          }}
        >
          {/* Header */}
          <div class="p-5 border-b border-white/10 flex items-center justify-between">
            <h2
              class="text-base font-bold flex items-center gap-2"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              <span>Your B2B Cart</span>
              <span
                class="text-xs px-2 py-0.5 rounded-full font-mono font-medium border"
                style={{
                  borderColor:
                    "var(--color-t-border, rgba(255, 255, 255, 0.15))",
                  color: "var(--color-t-bright, #ffffff)",
                  backgroundColor: "rgba(128, 128, 128, 0.1)",
                }}
              >
                {cart.items.reduce((s, i) => s + i.quantity, 0)} items
              </span>
            </h2>
            <button
              type="button"
              onClick$={() => (cart.isOpen = false)}
              class="p-1.5 rounded-lg opacity-70 hover:opacity-100 hover:bg-white/5 text-sm"
            >
              ✕
            </button>
          </div>

          {/* Items List */}
          <div class="flex-1 overflow-y-auto p-5 space-y-4">
            {cart.items.length === 0 ? (
              <div class="h-64 flex flex-col items-center justify-center text-center opacity-60">
                <svg
                  class="w-12 h-12 mb-3 stroke-1"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                >
                  <path
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    d="M16 11V7a4 4 0 00-8 0v4M5 9h14l1 12H4L5 9z"
                  />
                </svg>
                <p class="text-sm font-medium">Your cart is currently empty.</p>
                <p class="text-xs mt-1">
                  Browse catalog to add industrial products.
                </p>
              </div>
            ) : (
              cart.items.map((item, idx) => (
                <div
                  key={idx}
                  class="p-3.5 rounded-xl border flex gap-3 relative transition-all"
                  style={{
                    backgroundColor: "var(--color-t-card, #191a1b)",
                    borderColor:
                      "var(--color-t-border, rgba(255, 255, 255, 0.08))",
                  }}
                >
                  <ProductImage
                    src={item.product.image_url}
                    alt={item.product.title}
                    width={64}
                    height={64}
                    class="w-16 h-16 object-cover rounded-lg border border-white/10 bg-black/20 shrink-0"
                  />
                  <div class="flex-1 min-w-0">
                    <h3
                      class="text-xs font-semibold truncate"
                      style={{ color: "var(--color-t-bright, #ffffff)" }}
                    >
                      {item.product.title}
                    </h3>
                    <p class="text-[11px] opacity-60 font-mono mt-0.5">
                      SKU: {item.product.sku}
                    </p>
                    <div class="mt-2 flex items-center justify-between">
                      <div class="flex items-center gap-1.5 border border-white/10 rounded-md px-1.5 py-0.5 bg-black/20 text-xs">
                        <button
                          type="button"
                          aria-label={`Decrease quantity of ${item.product.title}`}
                          onClick$={() => changeQuantity(idx, -1)}
                          class="px-1 opacity-70 hover:opacity-100"
                        >
                          -
                        </button>
                        <span class="font-mono px-1">{item.quantity}</span>
                        <button
                          type="button"
                          aria-label={`Increase quantity of ${item.product.title}`}
                          onClick$={() => changeQuantity(idx, 1)}
                          class="px-1 opacity-70 hover:opacity-100"
                        >
                          +
                        </button>
                      </div>
                      <div class="text-right">
                        <div
                          class="text-xs font-bold font-mono"
                          style={{ color: "var(--color-t-bright, #ffffff)" }}
                        >
                          $
                          {(
                            parseFloat(item.selectedPrice) * item.quantity
                          ).toFixed(2)}
                        </div>
                        {parseFloat(item.selectedPrice) <
                          parseFloat(item.product.base_price ?? "") && (
                          <div class="text-[10px] text-emerald-400 font-medium">
                            Tier discount applied
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                  <button
                    type="button"
                    aria-label={`Remove ${item.product.title} from cart`}
                    onClick$={() => removeItem(idx)}
                    class="absolute top-2 right-2 text-xs opacity-40 hover:opacity-100 text-red-400"
                  >
                    ✕
                  </button>
                </div>
              ))
            )}
          </div>

          {/* Footer & Checkout */}
          {cart.items.length > 0 && (
            <div class="p-5 border-t border-white/10 space-y-3 bg-black/20">
              <div class="flex items-center justify-between text-sm">
                <span class="opacity-70">Estimated Subtotal:</span>
                <span
                  class="text-lg font-black font-mono"
                  style={{ color: "var(--color-t-bright, #ffffff)" }}
                >
                  ${subtotal} USD
                </span>
              </div>
              <p class="text-[11px] opacity-60">
                Taxes, freight carriers, and payment terms (NET 30 / Wire /
                Card) calculated at checkout.
              </p>
              <Link
                href="/cart"
                class="w-full py-3 rounded-xl font-bold text-xs uppercase tracking-wider text-white text-center flex items-center justify-center gap-2 transition-transform active:scale-[0.98] shadow-lg"
                style={{
                  backgroundColor: "var(--color-t-accent, #5e6ad2)",
                  boxShadow:
                    "0 4px 14px var(--color-t-accent-glow, rgba(94, 106, 210, 0.4))",
                }}
              >
                Proceed to Checkout →
              </Link>
            </div>
          )}
        </div>
      </div>
    </div>
  );
});
