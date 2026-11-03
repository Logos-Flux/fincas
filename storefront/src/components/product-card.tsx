import { component$, useContext, $ } from "@builder.io/qwik";
import { Link } from "@builder.io/qwik-city";
import type { Product } from "../lib/api-client";
import { addOrMergeItem, isOrderable } from "../lib/cart-math";
import { CartContext } from "../context/cart-context";
import { ProductImage } from "./product-image";

export interface ProductCardProps {
  product: Product;
}

export const ProductCard = component$<ProductCardProps>(({ product }) => {
  const cart = useContext(CartContext);
  const orderable = isOrderable(product);

  const addToCart = $(() => {
    if (!isOrderable(product) || product.base_price == null) return;
    cart.items = addOrMergeItem(cart.items, {
      product,
      quantity: 1,
      selectedPrice: product.base_price,
    });
    cart.isOpen = true;
  });

  const lowestTierPrice =
    product.tiered_pricing && product.tiered_pricing.length > 0
      ? product.tiered_pricing[product.tiered_pricing.length - 1].unit_price
      : null;

  return (
    <div
      class="group rounded-2xl border p-4 flex flex-col justify-between transition-all duration-300 hover:scale-[1.01] hover:shadow-xl relative overflow-hidden"
      style={{
        backgroundColor: "var(--color-t-card, #191a1b)",
        borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
      }}
    >
      {/* Top Media Container */}
      <div class="relative w-full aspect-4/3 rounded-xl overflow-hidden bg-black/40 mb-4 border border-white/5">
        <ProductImage
          src={product.image_url}
          alt={product.title}
          width={400}
          height={300}
          class="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
        />
        {/* Category Pill */}
        <div class="absolute top-2.5 left-2.5">
          <span class="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider backdrop-blur-md bg-black/60 border border-white/10 text-white">
            {product.category_name}
          </span>
        </div>

        {/* Stock Status Badge */}
        <div class="absolute top-2.5 right-2.5">
          {!orderable ? (
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-red-950/80 text-red-200 border border-red-500/30">
              <span class="w-1.5 h-1.5 rounded-full bg-red-400" />
              {product.base_price == null ? "Unavailable" : "Out of Stock"}
            </span>
          ) : product.stock_status === "in_stock" ? (
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-950/80 text-emerald-300 border border-emerald-500/30">
              <span class="w-1.5 h-1.5 rounded-full bg-emerald-400" />
              {product.stock_quantity} In Stock
            </span>
          ) : (
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-amber-950/80 text-amber-300 border border-amber-500/30">
              <span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-ping" />
              Low Stock
            </span>
          )}
        </div>
      </div>

      {/* Content */}
      <div class="flex-1 flex flex-col justify-between">
        <div>
          <div class="text-[11px] font-mono opacity-60 mb-1 flex items-center justify-between">
            <span>{product.brand_name}</span>
            <span>SKU: {product.sku}</span>
          </div>

          <Link href={`/products/${product.id}`} class="block">
            <h3
              class="text-sm font-bold tracking-tight line-clamp-1 group-hover:opacity-80 transition-opacity"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              {product.title}
            </h3>
          </Link>

          <p class="text-xs opacity-70 mt-1 line-clamp-2 leading-relaxed">
            {product.description}
          </p>
        </div>

        {/* Pricing & Actions */}
        <div class="mt-4 pt-3 border-t border-white/5 flex items-center justify-between gap-2">
          <div>
            <div class="flex items-baseline gap-1">
              {product.base_price == null ? (
                <span
                  class="text-xs font-semibold"
                  style={{ color: "var(--color-t-muted, #8a8f98)" }}
                >
                  Price on request
                </span>
              ) : (
                <>
                  <span
                    class="text-base font-black font-mono"
                    style={{ color: "var(--color-t-bright, #ffffff)" }}
                  >
                    ${product.base_price}
                  </span>
                  <span class="text-[10px] opacity-60 font-mono uppercase">
                    {product.currency}
                  </span>
                </>
              )}
            </div>
            {lowestTierPrice && (
              <div
                class="text-[10px]"
                style={{ color: "var(--color-t-muted, #8a8f98)" }}
              >
                As low as{" "}
                <span class="font-bold text-emerald-300">
                  ${lowestTierPrice}
                </span>{" "}
                in volume
              </div>
            )}
          </div>

          <div class="flex items-center gap-1.5">
            <Link
              href={`/products/${product.id}`}
              class="px-2.5 py-1.5 rounded-lg border text-xs font-semibold opacity-70 hover:opacity-100 hover:bg-white/5 transition-all"
              style={{
                borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
              }}
            >
              Details
            </Link>
            <button
              type="button"
              onClick$={addToCart}
              disabled={!orderable}
              aria-disabled={!orderable}
              aria-label={
                orderable
                  ? `Add ${product.title} to cart`
                  : product.base_price == null
                    ? `${product.title} has no listed price`
                    : `${product.title} is out of stock`
              }
              class={`px-3 py-1.5 rounded-lg text-xs font-bold text-white shadow-md transition-all active:scale-95 ${orderable ? "cursor-pointer" : "cursor-not-allowed opacity-50"}`}
              style={{
                backgroundColor: "var(--color-t-accent, #5e6ad2)",
                boxShadow:
                  "0 2px 8px var(--color-t-accent-glow, rgba(94, 106, 210, 0.3))",
              }}
            >
              {orderable
                ? "+ Add"
                : product.base_price == null
                  ? "Ask"
                  : "Sold Out"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
});
