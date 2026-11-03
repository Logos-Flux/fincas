import { component$, useSignal } from "@builder.io/qwik";
import { routeLoader$, type DocumentHead } from "@builder.io/qwik-city";
import { fetchCatalog, type Product } from "../lib/api-client";
import { ProductCard } from "../components/product-card";

export const useCatalog = routeLoader$(async () => {
  // Production builds never fall back to demo products; say so instead.
  try {
    return { products: await fetchCatalog(), unavailable: false };
  } catch {
    return { products: [] as Product[], unavailable: true };
  }
});

export default component$(() => {
  const catalogSignal = useCatalog();
  const selectedCategory = useSignal<string>("All");
  const searchQuery = useSignal<string>("");

  const products = catalogSignal.value.products;
  const unavailable = catalogSignal.value.unavailable;

  const categories = [
    "All",
    ...Array.from(new Set(products.map((p) => p.category_name))),
  ];

  const filteredProducts = products.filter((p) => {
    const matchesCat =
      selectedCategory.value === "All" ||
      p.category_name === selectedCategory.value;
    const matchesSearch =
      searchQuery.value.trim() === "" ||
      p.title.toLowerCase().includes(searchQuery.value.toLowerCase()) ||
      p.sku.toLowerCase().includes(searchQuery.value.toLowerCase()) ||
      p.description.toLowerCase().includes(searchQuery.value.toLowerCase());
    return matchesCat && matchesSearch;
  });

  return (
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-12">
      {/* Hero Section */}
      <section
        class="rounded-3xl border p-8 md:p-12 relative overflow-hidden transition-all duration-300 shadow-2xl"
        style={{
          backgroundColor: "var(--color-t-card, #191a1b)",
          borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
        }}
      >
        {/* Subtle Ambient Glow */}
        <div
          class="absolute -right-20 -top-20 w-96 h-96 rounded-full blur-3xl pointer-events-none opacity-20"
          style={{ backgroundColor: "var(--color-t-accent, #5e6ad2)" }}
        />

        <div class="relative z-10 max-w-3xl space-y-4">
          <div
            class="inline-flex items-center gap-2 px-3 py-1 rounded-full text-xs font-semibold border backdrop-blur-md"
            style={{
              backgroundColor: "rgba(255, 255, 255, 0.03)",
              borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.15))",
              color: "var(--color-t-bright, #ffffff)",
            }}
          >
            <span
              class="w-2 h-2 rounded-full animate-ping"
              style={{ backgroundColor: "var(--color-t-accent, #5e6ad2)" }}
            />
            <span>Headless Commerce Engine + 75 Design Templates</span>
          </div>

          <h1
            class="text-3xl sm:text-5xl font-black tracking-tight leading-tight"
            style={{ color: "var(--color-t-bright, #ffffff)" }}
          >
            Enterprise Hardware, <br />
            <span
              style={{
                color:
                  "var(--color-t-accent-ink, var(--color-t-accent, #5e6ad2))",
              }}
            >
              Powered by Fincas
            </span>
          </h1>

          <p class="text-sm sm:text-base opacity-75 leading-relaxed max-w-2xl">
            A fully reactive B2B e-shop demonstrating zero-hydration Qwik City
            paired with Fincas’s double-entry general ledger, tiered price
            books, and outbound webhook engine.
          </p>

          <div class="pt-4 flex flex-wrap items-center gap-3">
            <a
              href="#catalog"
              class="px-5 py-2.5 rounded-xl font-bold text-xs uppercase tracking-wider text-white shadow-lg transition-transform active:scale-95 cursor-pointer"
              style={{
                backgroundColor: "var(--color-t-accent, #5e6ad2)",
                boxShadow:
                  "0 4px 14px var(--color-t-accent-glow, rgba(94, 106, 210, 0.4))",
              }}
            >
              Browse Catalog ↓
            </a>
            <a
              href="/design-specs"
              class="px-4 py-2.5 rounded-xl font-semibold text-xs border opacity-80 hover:opacity-100 hover:bg-white/5 transition-all"
              style={{
                borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.15))",
                color: "var(--color-t-text, #d0d6e0)",
              }}
            >
              Explore Design Systems →
            </a>
          </div>
        </div>

        {/* Feature Highlights Grid */}
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-4 mt-8 pt-8 border-t border-white/10 text-xs">
          <div>
            <div
              class="font-mono text-lg font-black"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              REST API
            </div>
            <div class="opacity-60 text-[11px] mt-0.5">
              OpenAPI 3.0 Headless
            </div>
          </div>
          <div>
            <div class="font-mono text-lg font-black text-emerald-400">
              Live Sync
            </div>
            <div class="opacity-60 text-[11px] mt-0.5">Tiered Price Books</div>
          </div>
          <div>
            <div
              class="font-mono text-lg font-black"
              style={{
                color:
                  "var(--color-t-accent-ink, var(--color-t-accent, #5e6ad2))",
              }}
            >
              75 Themes
            </div>
            <div class="opacity-60 text-[11px] mt-0.5">
              Instant Hot-Swapping
            </div>
          </div>
          <div>
            <div class="font-mono text-lg font-black text-amber-400">
              Double-Entry
            </div>
            <div class="opacity-60 text-[11px] mt-0.5">
              General Ledger Accounting
            </div>
          </div>
        </div>
      </section>

      {/* Catalog Search & Category Filter Section */}
      <section id="catalog" class="space-y-6">
        <div class="flex flex-col md:flex-row items-stretch md:items-center justify-between gap-4">
          {/* Category Tabs */}
          <div class="flex items-center gap-1.5 overflow-x-auto pb-1 max-w-full">
            {categories.map((cat) => (
              <button
                key={cat}
                type="button"
                data-cat={cat}
                onClick$={(e) =>
                  (selectedCategory.value =
                    (e.currentTarget as HTMLElement).dataset.cat ?? "")
                }
                class={`px-3 py-1.5 rounded-xl text-xs font-semibold transition-all whitespace-nowrap cursor-pointer ${
                  selectedCategory.value === cat
                    ? "shadow-sm"
                    : "opacity-75 hover:opacity-100 hover:bg-black/5 dark:hover:bg-white/5"
                }`}
                style={{
                  backgroundColor:
                    selectedCategory.value === cat
                      ? "var(--color-t-accent, #5e6ad2)"
                      : "transparent",
                  color:
                    selectedCategory.value === cat
                      ? "#ffffff"
                      : "var(--color-t-text, #d0d6e0)",
                }}
              >
                {cat}
              </button>
            ))}
          </div>

          {/* Search Input */}
          <div class="relative w-full md:w-72">
            <input
              type="text"
              placeholder="Search products or SKUs..."
              value={searchQuery.value}
              onInput$={(e) =>
                (searchQuery.value = (e.target as HTMLInputElement).value)
              }
              class="w-full px-3.5 py-2 text-xs rounded-xl border bg-black/20 outline-none transition-all placeholder:opacity-40"
              style={{
                borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                color: "var(--color-t-bright, #ffffff)",
              }}
            />
            {searchQuery.value && (
              <button
                type="button"
                onClick$={() => (searchQuery.value = "")}
                class="absolute right-3 top-2.5 text-xs opacity-50 hover:opacity-100"
              >
                ✕
              </button>
            )}
          </div>
        </div>

        {/* Product Grid */}
        {filteredProducts.length === 0 ? (
          <div
            class="rounded-2xl border p-12 text-center opacity-60 space-y-2"
            style={{
              backgroundColor: "var(--color-t-card, #191a1b)",
              borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
            }}
          >
            {unavailable ? (
              <>
                <p class="text-sm font-semibold">
                  The catalog is unavailable right now.
                </p>
                <p class="text-xs">Please try again in a few minutes.</p>
              </>
            ) : products.length === 0 ? (
              <p class="text-sm font-semibold">No products are listed yet.</p>
            ) : (
              <>
                <p class="text-sm font-semibold">
                  No products found matching your search.
                </p>
                <p class="text-xs">
                  Try selecting a different category or clearing your query.
                </p>
              </>
            )}
          </div>
        ) : (
          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
            {filteredProducts.map((product: Product) => (
              <ProductCard key={product.id} product={product} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
});

export const head: DocumentHead = {
  title: "Fincas Storefront — Dynamic B2B Commerce Demo",
  meta: [
    {
      name: "description",
      content:
        "Open-source B2B commerce storefront built with Qwik City and Fincas headless backend.",
    },
  ],
};
