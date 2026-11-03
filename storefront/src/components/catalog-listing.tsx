import { component$ } from "@builder.io/qwik";
import { Link } from "@builder.io/qwik-city";
import type { Product } from "../lib/api-client";
import { ProductCard } from "./product-card";

export interface CatalogListingProps {
  products: Product[];
  /** Total across every page. */
  count: number;
  page: number;
  hasNext: boolean;
  /** Path of this listing ("/c/power/"); page links add `?page=N`. */
  basePath: string;
  unavailable?: boolean;
  emptyText: string;
}

const cardStyle = {
  backgroundColor: "var(--color-t-card, #191a1b)",
  borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
};

function pageHref(basePath: string, page: number): string {
  return page <= 1 ? basePath : `${basePath}?page=${page}`;
}

/** Product grid with ERP-page pagination, shared by category and brand pages. */
export const CatalogListing = component$<CatalogListingProps>((props) => {
  if (props.unavailable) {
    return (
      <div
        class="rounded-2xl border p-12 text-center opacity-60 space-y-2"
        style={cardStyle}
      >
        <p class="text-sm font-semibold">
          The catalog is unavailable right now.
        </p>
        <p class="text-xs">Please try again in a few minutes.</p>
      </div>
    );
  }
  if (props.products.length === 0) {
    return (
      <div
        class="rounded-2xl border p-12 text-center opacity-60"
        style={cardStyle}
      >
        <p class="text-sm font-semibold">{props.emptyText}</p>
      </div>
    );
  }
  return (
    <div class="space-y-6">
      <p class="text-xs opacity-60" data-testid="listing-count">
        {props.count} {props.count === 1 ? "product" : "products"}
      </p>
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
        {props.products.map((product) => (
          <ProductCard key={product.id} product={product} />
        ))}
      </div>
      {(props.page > 1 || props.hasNext) && (
        <nav
          aria-label="Pagination"
          class="flex items-center justify-between text-xs"
        >
          {props.page > 1 ? (
            <Link
              href={pageHref(props.basePath, props.page - 1)}
              rel="prev"
              class="px-3 py-1.5 rounded-xl border hover:bg-white/5"
              style={{ borderColor: cardStyle.borderColor }}
            >
              ← Previous
            </Link>
          ) : (
            <span />
          )}
          <span class="opacity-60">Page {props.page}</span>
          {props.hasNext ? (
            <Link
              href={pageHref(props.basePath, props.page + 1)}
              rel="next"
              class="px-3 py-1.5 rounded-xl border hover:bg-white/5"
              style={{ borderColor: cardStyle.borderColor }}
            >
              Next →
            </Link>
          ) : (
            <span />
          )}
        </nav>
      )}
    </div>
  );
});
