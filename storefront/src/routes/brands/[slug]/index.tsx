/**
 * Brand page: /brands/<slug>/ (?page=N for later pages). The brand's
 * description and its public products. SEO copy comes only from the ERP
 * (docs/storefront-seo.md).
 */
import { component$ } from "@builder.io/qwik";
import { Link, routeLoader$, type DocumentHead } from "@builder.io/qwik-city";
import {
  ApiError,
  fetchBrandBySlug,
  fetchProductPage,
  type Brand,
  type ProductPage,
} from "../../../lib/api-client";
import {
  BRANDS_PATH,
  CATALOG_CRUMB,
  brandPath,
  pageParam,
} from "../../../lib/catalog-paths";
import {
  breadcrumbJsonLd,
  buildSeoHead,
  collectionPageJsonLd,
  summarize,
  type Crumb,
} from "../../../lib/seo-head";
import { Breadcrumbs } from "../../../components/breadcrumbs";
import { CatalogListing } from "../../../components/catalog-listing";
import { ProductImage } from "../../../components/product-image";

interface BrandPageData {
  brand: Brand;
  listing: ProductPage;
  unavailable: boolean;
}

function brandCrumbs(brand: Brand): Crumb[] {
  return [
    CATALOG_CRUMB,
    { name: "Brands", url: BRANDS_PATH },
    { name: brand.title, url: brandPath(brand.slug) },
  ];
}

export const useBrandPage = routeLoader$(
  async ({ params, url, status }): Promise<BrandPageData | null> => {
    const page = pageParam(url);
    let brand: Brand | undefined;
    try {
      brand = await fetchBrandBySlug(params.slug);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
      status(503);
      return null;
    }
    if (!brand) {
      status(404);
      return null;
    }
    try {
      const listing = await fetchProductPage({ brandSlug: brand.slug, page });
      return { brand, listing, unavailable: false };
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
      return {
        brand,
        listing: { products: [], count: 0, page, hasNext: false },
        unavailable: true,
      };
    }
  },
);

export default component$(() => {
  const data = useBrandPage().value;
  if (!data) {
    return (
      <div class="max-w-4xl mx-auto px-4 py-20 text-center space-y-4">
        <h1 class="text-2xl font-bold">Brand not found</h1>
        <p class="text-sm opacity-70">
          This brand does not exist or is not listed.
        </p>
        <Link
          href={BRANDS_PATH}
          class="inline-block text-xs font-bold hover:underline"
        >
          ← All brands
        </Link>
      </div>
    );
  }
  const { brand, listing, unavailable } = data;
  return (
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-8">
      <Breadcrumbs crumbs={brandCrumbs(brand)} />
      <header class="flex flex-col sm:flex-row sm:items-center gap-6">
        {brand.logo_url && (
          <ProductImage
            src={brand.logo_url}
            alt={`${brand.title} logo`}
            width={160}
            height={80}
            class="h-20 w-auto object-contain"
          />
        )}
        <div class="space-y-3 max-w-3xl">
          <h1
            class="text-3xl sm:text-4xl font-black tracking-tight"
            style={{ color: "var(--color-t-bright, #ffffff)" }}
          >
            {brand.title}
          </h1>
          {brand.description && (
            <p class="text-sm sm:text-base opacity-75 leading-relaxed whitespace-pre-line">
              {brand.description}
            </p>
          )}
        </div>
      </header>
      <CatalogListing
        products={listing.products}
        count={listing.count}
        page={listing.page}
        hasNext={listing.hasNext}
        basePath={brandPath(brand.slug)}
        unavailable={unavailable}
        emptyText="No products from this brand are listed yet."
      />
    </div>
  );
});

export const head: DocumentHead = ({ resolveValue, url }) => {
  const data = resolveValue(useBrandPage);
  if (!data)
    return {
      title: "Brand not found",
      meta: [{ name: "robots", content: "noindex" }],
    };
  const { brand, listing } = data;
  const canonical =
    brandPath(brand.slug) + (listing.page > 1 ? `?page=${listing.page}` : "");
  return buildSeoHead({
    name: brand.title,
    seo_title: brand.seo_title,
    seo_meta_description: brand.seo_meta_description,
    seo_og_title: brand.seo_og_title,
    seo_og_description: brand.seo_og_description,
    seo_og_image_url: brand.seo_og_image_url,
    image: brand.banner_url || brand.logo_url,
    description: brand.description,
    canonical,
    origin: url.origin,
    jsonLd: [
      breadcrumbJsonLd(brandCrumbs(brand), url.origin),
      collectionPageJsonLd({
        name: brand.title,
        url: canonical,
        description: brand.seo_meta_description || summarize(brand.description),
        origin: url.origin,
      }),
    ],
  });
};
