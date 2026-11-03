# Storefront SEO: categories, brands and products

How the storefront builds page titles, descriptions, canonicals and structured
data for catalog pages, and what the ERP must provide.

## The contract

1. **The ERP is the only source of SEO copy.** Titles, meta descriptions and
   social copy are typed into the ERP (Category, Brand and Product edit pages,
   "SEO" section) and served by the API. The storefront never stores SEO copy
   of its own: no per-slug maps in storefront code, no copy generated from a
   template.
2. **When a field is empty, the storefront falls back to plain facts**, never
   to marketing phrasing:

   | Head field       | Source, in order                                                                                   |
   | ---------------- | -------------------------------------------------------------------------------------------------- |
   | `<title>`        | `seo_title`; else `<name> \| <site name>`; else `<name>` when no site name is configured           |
   | meta description | `seo_meta_description`; else whole leading sentences of the description (see below); else omitted |
   | `og:title`       | `seo_og_title`; else the title above                                                               |
   | `og:description` | `seo_og_description`; else the meta description above                                             |
   | `og:image`       | `seo_og_image_url` (categories, brands); else the page's own image                                 |
   | canonical        | category `seo_canonical_url`; else the page's own URL on `VITE_SITE_URL` (or the request origin)   |

   The description fallback (`summarize` in `storefront/src/lib/seo-head.ts`)
   takes as many whole sentences as fit in 160 characters. It never cuts a
   sentence and never adds an ellipsis: a single first sentence of up to 300
   characters is used whole, and a longer one is omitted. Category pages
   summarize `description`, then `short_description`, then `body_copy`.
3. **The site name comes from tenant config**, not code: `VITE_SITE_NAME`
   (and `VITE_SITE_URL` for absolute canonicals), read in
   `storefront/src/lib/site-config.ts`. Unset, titles carry no suffix, so a
   deployment that forgets it never shows another store's name.

   **`VITE_SITE_URL` is required in production.** Without it the canonical
   and `og:url` fall back to the request's own origin, i.e. whatever `Host`
   the request arrived with: a staging hostname, a CDN origin, the raw app
   hostname or a spoofed header would each become the page's canonical. The
   fallback exists for local development only.

All three page types (product, category, brand) build their head through
`buildSeoHead`, so the rules above hold everywhere. `RouterHead` adds its
request-URL canonical only to pages that set none.

## URLs

| Page        | URL                     | Notes                                               |
| ----------- | ----------------------- | --------------------------------------------------- |
| category    | `/c/<slug>/`            | lists its own products and every sub-category's     |
| brand index | `/brands/`              | every visible brand                                 |
| brand       | `/brands/<slug>/`       | the brand's public products                         |
| product     | `/products/<id>/`       | unchanged                                           |

Slugs are the ERP's (`Category.slug`, `Brand.slug`). Later listing pages add
`?page=N` (the API's 25-row pages) and are self-canonical. Unknown or hidden
slugs answer 404 with `robots: noindex`.

Category and brand pages emit `BreadcrumbList` and `CollectionPage` JSON-LD;
product pages emit `BreadcrumbList`. JSON-LD is serialized with `<` escaped,
so ERP copy can never close the script tag.

## API

The storefront reads the same DRF API it uses for products:

- `GET /api/products/categories/` and `/api/products/brands/` are **public for
  reads**, like `/api/products/products/`. Anonymous visitors, shoppers and the
  storefront identity see only `visible_on_website` rows (a category is hidden
  too when any ancestor is, so `parent`, `parent_slug` and `ancestors` never
  name a hidden row), through `PublicCategorySerializer` /
  `PublicBrandSerializer`: display fields, the `seo_*` copy, image URLs, and
  for categories `parent_slug`, `ancestors` (root first) and visible
  `children`. Migration provenance (`bc_id`, `erpnext_name`, `d1_id`) is
  staff-only. Staff, and service accounts (`svc-*`, API keys) whose role
  contract allows the read, keep the full rows, hidden ones included. Writes
  are unchanged (`PrincipalPermission`).
- Both accept `?slug=`; the storefront looks pages up that way.
- `GET /api/products/products/?category_slug=<slug>` lists the category's whole
  subtree; `?brand_slug=<slug>` one brand. For non-staff readers both resolve
  against public rows only: a hidden category or brand lists nothing, and the
  subtree walk skips hidden sub-categories (and everything below them). An
  unknown slug is an empty page, never the whole catalog. Catalog rows also carry `category_slug` and
  `brand_slug` (null when there is no public page to link to).
- A category's `body_copy` is withheld (null) if it carries draft metadata,
  even when it was written around the model's `clean()`. The storefront renders
  it with `storefront/src/lib/body-copy.ts`: paragraphs, `##`/`###`, `- `
  lists, `**bold**` and links, all HTML-escaped; only site-relative and
  http(s) links survive.

## ERP warnings

Saving a Brand or Category in `/erp/` flashes **warnings, not errors**
(`products/seo_checks.py`) when its SEO copy:

- contains a template placeholder (`{name}`, `%s`, `[brand]`, ...);
- ends in an ellipsis, or a description doesn't end on a full stop;
- is over 60 (title) or 160 (meta description) characters, or a meta
  description is under 50;
- matches another row's copy with only the name swapped ("Shop X Equipment |
  Store" on every category), which reads to search engines as duplicate pages;
- stops mid-sentence on a connector or separator ("...support, and.",
  "...for Solar, Golf,"), per `products/seo_quality.py`;
- matches one of the deployment's own generated-copy patterns (below).

The row saves either way. The form help text carries the same length guidance.

## Copy-quality rules (`products/seo_quality.py`)

One definition of "generated or cut-off copy" for everything that judges a
`seo_title` / `seo_meta_description`: the warnings above, and a deployment's
own restore or audit commands (`is_replaceable`, `is_unusable`, `problems`).

- **Rules are data.** golden ships tenant-neutral truncation shapes only
  (`products/data/seo_quality_defaults.json`): no template patterns, since a
  shop's filler always names the shop. A deployment lists its own files in
  `GOLDEN_SEO_PATTERN_FILES` (same shape, `"version": 1`): `patterns` are
  appended (ids unique across files), `truncation` lists merge onto the
  defaults. A bad file is `ImproperlyConfigured` at startup.
- **Two severities.** `template`: the whole value is filler, safe to blank.
  `padding`: real copy with a generated phrase added, replaceable but kept.
- **Two strengths of truncation.** `is_cut_off` (ends on a connector or a
  configured cut shape) is certain and makes a value unusable; a meta with no
  closing punctuation, or ending on a configured soft word, is only
  *possibly* truncated, because honest copy ends that way too ("15kW output").
- **Patterns mirror to JavaScript** (`patterns_payload()`): no lookbehind, no
  named groups, no inline flags; `flags` is `""` or `"i"`. The loader refuses
  anything else.
- Length (60 / 160) is reported, never fixed by cutting.

## Variant SEO

`ProductVariant.seo_title` and `seo_meta_description` override the parent
product's on a variant page; blank inherits (`get_effective_field`). They are
in the variant cards' "SEO (override)" section. Every `ProductVariantForm`
field must be listed in `VARIANT_SECTIONS`: the form uses `exclude`, so a
field the cards don't render is posted empty and wipes the stored value on
each product save.
