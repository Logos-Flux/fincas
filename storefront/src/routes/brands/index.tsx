/** Brand index: /brands/. Every visible brand, linking to /brands/<slug>/. */
import { component$ } from "@builder.io/qwik";
import { Link, routeLoader$, type DocumentHead } from "@builder.io/qwik-city";
import { ApiError, fetchBrands, type Brand } from "../../lib/api-client";
import { BRANDS_PATH, CATALOG_CRUMB, brandPath } from "../../lib/catalog-paths";
import {
  breadcrumbJsonLd,
  buildSeoHead,
  collectionPageJsonLd,
} from "../../lib/seo-head";
import { Breadcrumbs } from "../../components/breadcrumbs";
import { ProductImage } from "../../components/product-image";

export const useBrands = routeLoader$(async ({ status }) => {
  try {
    return { brands: await fetchBrands(), unavailable: false };
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) throw err;
    status(503);
    return { brands: [] as Brand[], unavailable: true };
  }
});

const crumbs = [CATALOG_CRUMB, { name: "Brands", url: BRANDS_PATH }];

export default component$(() => {
  const { brands, unavailable } = useBrands().value;
  return (
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-8">
      <Breadcrumbs crumbs={crumbs} />
      <h1
        class="text-3xl sm:text-4xl font-black tracking-tight"
        style={{ color: "var(--color-t-bright, #ffffff)" }}
      >
        Brands
      </h1>
      {unavailable || brands.length === 0 ? (
        <div
          class="rounded-2xl border p-12 text-center opacity-60"
          style={{
            backgroundColor: "var(--color-t-card, #191a1b)",
            borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
          }}
        >
          <p class="text-sm font-semibold">
            {unavailable
              ? "The catalog is unavailable right now."
              : "No brands are listed yet."}
          </p>
        </div>
      ) : (
        <ul class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4">
          {brands.map((brand) => (
            <li key={brand.id}>
              <Link
                href={brandPath(brand.slug)}
                class="group flex flex-col items-center gap-3 rounded-2xl border p-5 h-full text-center transition-all hover:shadow-xl"
                style={{
                  backgroundColor: "var(--color-t-card, #191a1b)",
                  borderColor:
                    "var(--color-t-border, rgba(255, 255, 255, 0.08))",
                }}
              >
                {brand.logo_url && (
                  <ProductImage
                    src={brand.logo_url}
                    alt=""
                    width={160}
                    height={80}
                    class="h-16 w-auto object-contain"
                  />
                )}
                <span
                  class="text-sm font-bold group-hover:underline"
                  style={{ color: "var(--color-t-bright, #ffffff)" }}
                >
                  {brand.title}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
});

export const head: DocumentHead = ({ url }) =>
  buildSeoHead({
    name: "Brands",
    canonical: BRANDS_PATH,
    origin: url.origin,
    jsonLd: [
      breadcrumbJsonLd(crumbs, url.origin),
      collectionPageJsonLd({
        name: "Brands",
        url: BRANDS_PATH,
        origin: url.origin,
      }),
    ],
  });
