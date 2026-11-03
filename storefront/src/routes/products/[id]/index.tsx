import { component$, useSignal, useContext, $ } from "@builder.io/qwik";
import { routeLoader$, Link, type DocumentHead } from "@builder.io/qwik-city";
import { ApiError, fetchProductById } from "../../../lib/api-client";
import {
  addOrMergeItem,
  isOrderable,
  resolveTierPrice,
} from "../../../lib/cart-math";
import { CartContext } from "../../../context/cart-context";
import { ProductImage } from "../../../components/product-image";
import {
  CATALOG_CRUMB,
  brandPath,
  categoryPath,
  productPath,
} from "../../../lib/catalog-paths";
import { breadcrumbJsonLd, buildSeoHead } from "../../../lib/seo-head";

export const useProduct = routeLoader$(async ({ params, status }) => {
  // Product ids are UUIDs in the ERP; Number() turned every real one into NaN.
  let product;
  try {
    product = await fetchProductById(params.id);
  } catch (err) {
    // A rejected session must surface as such, not as "unavailable".
    if (err instanceof ApiError && err.status === 401) throw err;
    // Catalog unreachable in a production build: no demo stand-in.
    status(503);
    return null;
  }
  if (!product) {
    status(404);
    return null;
  }
  return product;
});

export default component$(() => {
  const productSignal = useProduct();
  const cart = useContext(CartContext);
  const selectedVariantId = useSignal<number | string | undefined>(undefined);
  const quantity = useSignal<number>(1);
  const isAdded = useSignal<boolean>(false);

  const product = productSignal.value;
  if (!product) {
    return (
      <div class="max-w-4xl mx-auto px-4 py-20 text-center space-y-4">
        <h1 class="text-2xl font-bold">Product Not Found</h1>
        <p class="text-sm opacity-70">
          The requested product does not exist in the catalog.
        </p>
        <Link
          href="/"
          class="inline-block px-4 py-2 rounded-lg bg-indigo-600 text-white text-xs font-bold"
        >
          ← Back to Catalog
        </Link>
      </div>
    );
  }

  const orderable = isOrderable(product);

  // Active price: variant price first, then the quantity-tier break.
  const variantPrice =
    selectedVariantId.value && product.variants.length > 0
      ? product.variants.find((v) => v.id === selectedVariantId.value)?.price
      : undefined;
  const listedPrice = variantPrice ?? product.base_price;
  const activePrice =
    listedPrice == null
      ? null
      : resolveTierPrice(listedPrice, product.tiered_pricing, quantity.value);

  const addToCart = $(() => {
    if (!isOrderable(product) || activePrice == null) return;
    cart.items = addOrMergeItem(cart.items, {
      product,
      variantId: selectedVariantId.value,
      quantity: quantity.value,
      selectedPrice: activePrice,
    });

    isAdded.value = true;
    setTimeout(() => {
      isAdded.value = false;
    }, 2000);
    cart.isOpen = true;
  });

  return (
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-10">
      {/* Breadcrumb Navigation */}
      <nav class="flex items-center gap-2 text-xs opacity-60">
        <Link href="/" class="hover:underline">
          Catalog
        </Link>
        <span>/</span>
        {product.category_slug ? (
          <Link
            href={categoryPath(product.category_slug)}
            class="hover:underline"
          >
            {product.category_name}
          </Link>
        ) : (
          <span>{product.category_name}</span>
        )}
        <span>/</span>
        <span
          class="font-medium truncate"
          style={{ color: "var(--color-t-bright, #ffffff)" }}
        >
          {product.title}
        </span>
      </nav>

      {/* Main Product Layout */}
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-10 items-start">
        {/* Product Media */}
        <div class="space-y-4">
          <div
            class="rounded-3xl border overflow-hidden bg-black/40 aspect-4/3 relative flex items-center justify-center p-4"
            style={{
              borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
            }}
          >
            <ProductImage
              src={product.image_url}
              alt={product.title}
              width={600}
              height={450}
              class="w-full h-full object-cover rounded-2xl"
              loading="eager"
            />
            <div class="absolute top-4 left-4">
              {product.brand_slug ? (
                <Link
                  href={brandPath(product.brand_slug)}
                  class="px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider backdrop-blur-md bg-black/70 border border-white/10 text-white hover:underline"
                >
                  {product.brand_name}
                </Link>
              ) : (
                <span class="px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider backdrop-blur-md bg-black/70 border border-white/10 text-white">
                  {product.brand_name}
                </span>
              )}
            </div>
          </div>
        </div>

        {/* Product Details & Ordering */}
        <div class="space-y-6">
          <div>
            <div
              class="flex items-center gap-2 text-xs font-mono mb-2"
              style={{ color: "var(--color-t-muted, #8a8f98)" }}
            >
              <span>SKU: {product.sku}</span>
              <span aria-hidden="true">•</span>
              <span
                class={`capitalize font-semibold ${orderable ? "text-emerald-300" : "text-red-300"}`}
              >
                {product.stock_status.replace("_", " ")} (
                {product.stock_quantity} available)
              </span>
            </div>
            <h1
              class="text-2xl sm:text-4xl font-black tracking-tight leading-tight"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              {product.title}
            </h1>
            <p class="text-sm opacity-75 mt-3 leading-relaxed">
              {product.description}
            </p>
          </div>

          {/* Pricing Box */}
          <div
            class="p-5 rounded-2xl border flex items-center justify-between"
            style={{
              backgroundColor: "var(--color-t-card, #191a1b)",
              borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
            }}
          >
            <div>
              <div class="text-xs opacity-60">Unit Price:</div>
              <div class="flex items-baseline gap-1.5 mt-0.5">
                {activePrice == null ? (
                  <span
                    class="text-lg font-semibold"
                    style={{ color: "var(--color-t-muted, #8a8f98)" }}
                  >
                    Price on request
                  </span>
                ) : (
                  <>
                    <span
                      class="text-3xl font-black font-mono"
                      style={{ color: "var(--color-t-bright, #ffffff)" }}
                    >
                      ${activePrice}
                    </span>
                    <span class="text-xs font-mono opacity-60 uppercase">
                      {product.currency}
                    </span>
                  </>
                )}
              </div>
            </div>

            <div class="text-right text-xs">
              <div class="font-semibold text-emerald-400">
                Direct From Warehouse
              </div>
              <div class="opacity-60 text-[11px] mt-0.5">
                Ships within 24h via Freight/Air
              </div>
            </div>
          </div>

          {/* Variants (if available) */}
          {product.variants.length > 0 && (
            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-wider opacity-80">
                Select Configuration / Variant:
              </label>
              <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {product.variants.map((v) => (
                  <button
                    key={v.id}
                    type="button"
                    onClick$={() => (selectedVariantId.value = v.id)}
                    class={`p-3 rounded-xl border text-left text-xs transition-all cursor-pointer ${
                      selectedVariantId.value === v.id
                        ? "border-indigo-500 ring-1 ring-indigo-500 font-bold shadow-sm"
                        : "hover:bg-white/5"
                    }`}
                    style={{
                      backgroundColor:
                        selectedVariantId.value === v.id
                          ? "var(--color-t-card, #191a1b)"
                          : "transparent",
                      borderColor:
                        selectedVariantId.value === v.id
                          ? "var(--color-t-accent, #5e6ad2)"
                          : "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                      color:
                        selectedVariantId.value === v.id
                          ? "var(--color-t-bright, #ffffff)"
                          : "inherit",
                    }}
                  >
                    <div class="font-semibold">{v.name}</div>
                    <div
                      class="font-mono mt-1"
                      style={{ color: "var(--color-t-muted, #8a8f98)" }}
                    >
                      ${v.price} {product.currency}
                    </div>
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Tiered Pricing Table */}
          {product.tiered_pricing && product.tiered_pricing.length > 0 && (
            <div class="space-y-2">
              <div
                class="flex items-center justify-between text-xs font-bold uppercase tracking-wider"
                style={{ color: "var(--color-t-muted, #8a8f98)" }}
              >
                <span>Volume Price Breaks:</span>
                <span class="text-[10px] text-emerald-300 font-mono">
                  Synced with Fincas Price Books
                </span>
              </div>
              <div
                class="rounded-xl border overflow-hidden text-xs"
                style={{
                  backgroundColor: "var(--color-t-surface, #0f1011)",
                  borderColor:
                    "var(--color-t-border, rgba(255, 255, 255, 0.08))",
                }}
              >
                <table class="w-full text-left">
                  <thead class="border-b border-white/5 opacity-60 text-[11px]">
                    <tr>
                      <th class="p-2.5">Min Quantity</th>
                      <th class="p-2.5">Unit Price</th>
                      <th class="p-2.5">Volume Savings</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-white/5 font-mono">
                    {product.tiered_pricing.map((tier, idx) => (
                      <tr
                        key={idx}
                        class={
                          quantity.value >= tier.min_quantity
                            ? "bg-white/5 text-white font-bold"
                            : "opacity-80"
                        }
                      >
                        <td class="p-2.5">{tier.min_quantity}+ units</td>
                        <td class="p-2.5">${tier.unit_price}</td>
                        <td class="p-2.5 text-emerald-400">
                          {tier.discount_percentage || "Base"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Add to Cart Controls */}
          {!orderable ? (
            <div
              class="rounded-xl border p-4 text-sm"
              role="status"
              style={{
                backgroundColor: "var(--color-t-card, #191a1b)",
                borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
              }}
            >
              <p
                class="font-bold"
                style={{ color: "var(--color-t-bright, #ffffff)" }}
              >
                Out of Stock
              </p>
              <p
                class="text-xs mt-1"
                style={{ color: "var(--color-t-muted, #8a8f98)" }}
              >
                SKU {product.sku} has no available units and cannot be ordered
                right now.
              </p>
              <button
                type="button"
                disabled
                aria-disabled="true"
                class="mt-3 w-full py-3 px-6 rounded-xl font-bold text-xs uppercase tracking-wider cursor-not-allowed opacity-50"
                style={{
                  backgroundColor: "var(--color-t-hover, #28282c)",
                  color: "var(--color-t-muted, #8a8f98)",
                }}
              >
                Out of Stock — Cannot Add
              </button>
            </div>
          ) : (
            <div class="flex items-center gap-4 pt-2">
              <div class="flex items-center border border-white/10 rounded-xl px-3 py-2 bg-black/20 text-sm">
                <button
                  type="button"
                  aria-label="Decrease quantity"
                  onClick$={() =>
                    (quantity.value = Math.max(1, quantity.value - 1))
                  }
                  class="px-2 font-bold opacity-70 hover:opacity-100"
                >
                  -
                </button>
                <input
                  type="number"
                  min="1"
                  aria-label="Quantity"
                  value={quantity.value}
                  onInput$={(e) =>
                    (quantity.value = Math.max(
                      1,
                      parseInt((e.target as HTMLInputElement).value) || 1,
                    ))
                  }
                  class="w-12 text-center font-mono font-bold bg-transparent outline-none"
                />
                <button
                  type="button"
                  aria-label="Increase quantity"
                  onClick$={() => (quantity.value = quantity.value + 1)}
                  class="px-2 font-bold opacity-70 hover:opacity-100"
                >
                  +
                </button>
              </div>

              <button
                type="button"
                onClick$={addToCart}
                class="flex-1 py-3 px-6 rounded-xl font-bold text-xs uppercase tracking-wider text-white shadow-xl transition-all active:scale-95 cursor-pointer flex items-center justify-center gap-2"
                style={{
                  backgroundColor: "var(--color-t-accent, #5e6ad2)",
                  boxShadow:
                    "0 4px 14px var(--color-t-accent-glow, rgba(94, 106, 210, 0.4))",
                }}
              >
                {isAdded.value ? "✓ Added to Order" : "+ Add to B2B Cart"}
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Specifications Section */}
      {product.specs && Object.keys(product.specs).length > 0 && (
        <section
          class="rounded-3xl border p-8 space-y-6"
          style={{
            backgroundColor: "var(--color-t-card, #191a1b)",
            borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
          }}
        >
          <h2 class="text-lg font-bold text-white flex items-center gap-2">
            <span>Engineering Specifications</span>
            <span class="text-xs px-2 py-0.5 rounded bg-white/10 opacity-70 font-mono font-normal">
              Technical Profile
            </span>
          </h2>
          <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4 text-xs font-mono">
            {Object.entries(product.specs).map(([key, value]) => (
              <div
                key={key}
                class="p-3 rounded-xl border border-white/5 bg-black/20"
              >
                <div
                  class="text-[10px] uppercase"
                  style={{ color: "var(--color-t-muted, #8a8f98)" }}
                >
                  {key}
                </div>
                <div class="text-white font-semibold mt-1 truncate">
                  {value}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
});

export const head: DocumentHead = ({ resolveValue, url }) => {
  const product = resolveValue(useProduct);
  if (!product)
    return {
      title: "Product not found",
      meta: [{ name: "robots", content: "noindex" }],
    };
  const crumbs = [
    CATALOG_CRUMB,
    ...(product.category_slug
      ? [
          {
            name: product.category_name,
            url: categoryPath(product.category_slug),
          },
        ]
      : []),
    { name: product.title, url: productPath(product.id) },
  ];
  return buildSeoHead({
    name: product.title,
    seo_title: product.seo_title,
    seo_meta_description: product.seo_meta_description,
    seo_og_title: product.seo_og_title,
    seo_og_description: product.seo_og_description,
    image: product.image_url,
    description: product.long_description || product.description,
    canonical: productPath(product.id),
    origin: url.origin,
    ogType: "product",
    jsonLd: [breadcrumbJsonLd(crumbs, url.origin)],
  });
};
