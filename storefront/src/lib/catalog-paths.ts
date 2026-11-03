/**
 * Storefront URL conventions for catalog pages (docs/storefront-seo.md):
 *
 * | Page            | URL                          |
 * | --------------- | ---------------------------- |
 * | category        | `/c/<category slug>/`        |
 * | brand index     | `/brands/`                   |
 * | brand           | `/brands/<brand slug>/`      |
 * | product         | `/products/<product id>/`    |
 *
 * Slugs are the ERP's; later listing pages add `?page=N`.
 */
import type { Category } from "./api-client";
import type { Crumb } from "./seo-head";

export const categoryPath = (slug: string) => `/c/${encodeURIComponent(slug)}/`;
export const brandPath = (slug: string) => `/brands/${encodeURIComponent(slug)}/`;
export const BRANDS_PATH = "/brands/";
export const productPath = (id: string | number) =>
  `/products/${encodeURIComponent(String(id))}/`;

export const CATALOG_CRUMB: Crumb = { name: "Catalog", url: "/" };

export function categoryCrumbs(category: Category): Crumb[] {
  return [
    CATALOG_CRUMB,
    ...category.ancestors.map((a) => ({ name: a.title, url: categoryPath(a.slug) })),
    { name: category.title, url: categoryPath(category.slug) },
  ];
}

/** Read `?page=N` (1-based); anything else is page 1. */
export function pageParam(url: URL): number {
  return Math.max(1, Number.parseInt(url.searchParams.get("page") ?? "1", 10) || 1);
}
