/**
 * Category page: /c/<slug>/ (?page=N for later pages).
 *
 * Lists the category's products and those of every sub-category below it,
 * links to the visible sub-categories, and renders the ERP's description and
 * approved body copy. SEO copy comes only from the ERP (docs/storefront-seo.md);
 * the head falls back to "<name> | <site name>" and a whole-sentence summary.
 */
import { component$ } from "@builder.io/qwik";
import { Link, routeLoader$, type DocumentHead } from "@builder.io/qwik-city";
import {
  ApiError,
  fetchCategoryBySlug,
  fetchProductPage,
  type Category,
  type ProductPage,
} from "../../../lib/api-client";
import { renderBodyCopy } from "../../../lib/body-copy";
import {
  categoryCrumbs,
  categoryPath,
  pageParam,
} from "../../../lib/catalog-paths";
import {
  breadcrumbJsonLd,
  buildSeoHead,
  collectionPageJsonLd,
  summarize,
} from "../../../lib/seo-head";
import { Breadcrumbs } from "../../../components/breadcrumbs";
import { CatalogListing } from "../../../components/catalog-listing";

interface CategoryPageData {
  category: Category;
  listing: ProductPage;
  unavailable: boolean;
}

export const useCategoryPage = routeLoader$(
  async ({ params, url, status }): Promise<CategoryPageData | null> => {
    const page = pageParam(url);
    let category: Category | undefined;
    try {
      category = await fetchCategoryBySlug(params.slug);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
      status(503);
      return null;
    }
    if (!category) {
      status(404);
      return null;
    }
    try {
      const listing = await fetchProductPage({
        categorySlug: category.slug,
        page,
      });
      return { category, listing, unavailable: false };
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
      return {
        category,
        listing: { products: [], count: 0, page, hasNext: false },
        unavailable: true,
      };
    }
  },
);

export default component$(() => {
  const data = useCategoryPage().value;
  if (!data) {
    return (
      <div class="max-w-4xl mx-auto px-4 py-20 text-center space-y-4">
        <h1 class="text-2xl font-bold">Category not found</h1>
        <p class="text-sm opacity-70">
          This category does not exist or is not listed.
        </p>
        <Link
          href="/"
          class="inline-block px-4 py-2 rounded-lg text-xs font-bold"
          style={{
            backgroundColor: "var(--color-t-accent, #5e6ad2)",
            color: "var(--color-t-on-accent, #ffffff)",
          }}
        >
          ← Back to Catalog
        </Link>
      </div>
    );
  }
  const { category, listing, unavailable } = data;
  const intro = category.description || category.short_description;
  const bodyHtml = renderBodyCopy(category.body_copy);

  return (
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-8">
      <Breadcrumbs crumbs={categoryCrumbs(category)} />

      <header class="space-y-3 max-w-3xl">
        <h1
          class="text-3xl sm:text-4xl font-black tracking-tight"
          style={{ color: "var(--color-t-bright, #ffffff)" }}
        >
          {category.title}
        </h1>
        {intro && (
          <p class="text-sm sm:text-base opacity-75 leading-relaxed">{intro}</p>
        )}
      </header>

      {category.children.length > 0 && (
        <nav aria-label="Sub-categories" class="flex flex-wrap gap-2">
          {category.children.map((child) => (
            <Link
              key={child.id}
              href={categoryPath(child.slug)}
              class="px-3 py-1.5 rounded-xl text-xs font-semibold border hover:bg-white/5"
              style={{
                borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.15))",
                color: "var(--color-t-text, #d0d6e0)",
              }}
            >
              {child.title}
            </Link>
          ))}
        </nav>
      )}

      <CatalogListing
        products={listing.products}
        count={listing.count}
        page={listing.page}
        hasNext={listing.hasNext}
        basePath={categoryPath(category.slug)}
        unavailable={unavailable}
        emptyText="No products are listed in this category yet."
      />

      {bodyHtml && (
        <section
          aria-labelledby="category-about"
          class="rounded-3xl border p-8 space-y-4 text-sm leading-relaxed max-w-none body-copy"
          style={{
            backgroundColor: "var(--color-t-card, #191a1b)",
            borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
          }}
        >
          <h2
            id="category-about"
            class="text-lg font-bold"
            style={{ color: "var(--color-t-bright, #ffffff)" }}
          >
            About {category.title}
          </h2>
          <div dangerouslySetInnerHTML={bodyHtml} />
        </section>
      )}
    </div>
  );
});

export const head: DocumentHead = ({ resolveValue, url }) => {
  const data = resolveValue(useCategoryPage);
  if (!data)
    return {
      title: "Category not found",
      meta: [{ name: "robots", content: "noindex" }],
    };
  const { category, listing } = data;
  const path =
    categoryPath(category.slug) +
    (listing.page > 1 ? `?page=${listing.page}` : "");
  const canonical = category.seo_canonical_url || path;
  const source =
    category.description || category.short_description || category.body_copy;
  const head = buildSeoHead({
    name: category.title,
    seo_title: category.seo_title,
    seo_meta_description: category.seo_meta_description,
    seo_og_title: category.seo_og_title,
    seo_og_description: category.seo_og_description,
    seo_og_image_url: category.seo_og_image_url,
    image: category.image_url,
    description: source,
    canonical,
    origin: url.origin,
    jsonLd: [
      breadcrumbJsonLd(categoryCrumbs(category), url.origin),
      collectionPageJsonLd({
        name: category.title,
        url: canonical,
        description: category.seo_meta_description || summarize(source),
        origin: url.origin,
      }),
    ],
  });
  return head;
};
