/**
 * One head builder for every catalog page (product, category, brand).
 *
 * The ERP is the only source of SEO copy (docs/storefront-seo.md). This
 * module never writes copy of its own: it uses the ERP's `seo_*` fields when
 * they are set and otherwise falls back to the plain name and the page's own
 * description, cut at a sentence boundary. No templated phrases, no
 * hard-coded store name (that comes from site-config).
 */
import type { DocumentHeadValue } from "@builder.io/qwik-city";
import { getSiteConfig, type SiteConfig } from "./site-config";

/** Search engines show roughly this many characters of a description. */
export const META_DESCRIPTION_MAX = 160;
/** A single first sentence longer than this is omitted rather than cut. */
export const SINGLE_SENTENCE_MAX = 300;

const ENTITIES: Record<string, string> = {
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&quot;": '"',
  "&#39;": "'",
  "&apos;": "'",
  "&nbsp;": " ",
  "&rsquo;": "\u2019",
  "&lsquo;": "\u2018",
  "&rdquo;": "\u201d",
  "&ldquo;": "\u201c",
  "&mdash;": "\u2014",
  "&ndash;": "\u2013",
  "&hellip;": "\u2026",
};

function decodeEntity(entity: string): string {
  const numeric = /^&#(x[0-9a-f]+|[0-9]+);$/i.exec(entity);
  if (numeric) {
    const raw = numeric[1];
    const code =
      raw[0].toLowerCase() === "x" ? parseInt(raw.slice(1), 16) : parseInt(raw, 10);
    if (Number.isFinite(code) && code > 0 && code <= 0x10ffff) {
      try {
        return String.fromCodePoint(code);
      } catch {
        return " ";
      }
    }
    return " ";
  }
  return ENTITIES[entity.toLowerCase()] ?? " ";
}

/** True when text ends in an ellipsis (a cut-off excerpt, never a summary). */
function endsInEllipsis(text: string): boolean {
  return /(\.\.\.|\u2026)["'\u201d\u2019)\]]*$/.test(text.trim());
}

/** Plain text from HTML or markdown-ish copy: tags out, entities decoded, whitespace folded. */
export function toPlainText(text: string | null | undefined): string {
  if (!text) return "";
  return text
    .replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, " ")
    .replace(/<\/(p|div|li|h[1-6]|tr|td|th)\s*>|<br\s*\/?>/gi, ". ")
    .replace(/<\/?(p|div|ul|ol|li|h[1-6]|table|tr|td|th)\b[^>]*>/gi, " ")
    .replace(/<[^>]*>/g, "")
    .replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);/gi, (e) => decodeEntity(e))
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/\*\*|__/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\s+/g, " ")
    .replace(/\s+([.,;:!?])/g, "$1")
    .replace(/([!?])\./g, "$1")
    .replace(/\.(\s+\.)+/g, ".")
    .replace(/\.{4,}/g, "...")
    .replace(/(?<!\.)\.\.(?!\.)/g, ".")
    .replace(/^[.\s]+/, "")
    .trim();
}

/**
 * Split plain text into sentences, keeping their end punctuation. A boundary
 * is end punctuation followed by space and a capital, digit or quote, so
 * "3.5 kW" and "e.g. a cabin" stay inside their sentence.
 */
function sentences(text: string): string[] {
  return text
    .split(/(?<=[.!?]["'”’)\]]*)\s+(?=[A-Z0-9"'“‘(\[])/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * A meta-description summary built from whole sentences only.
 *
 * Takes as many leading sentences as fit in `max` characters. If the first
 * sentence alone is longer, it is used whole up to SINGLE_SENTENCE_MAX and
 * omitted beyond that: a summary is never cut mid-sentence, and never gets
 * an ellipsis. Returns undefined when there is nothing usable.
 */
export function summarize(
  text: string | null | undefined,
  max = META_DESCRIPTION_MAX,
): string | undefined {
  const plain = toPlainText(text);
  if (!plain) return undefined;
  const parts = sentences(plain);
  if (parts.length === 0) return undefined;
  let summary = "";
  for (const s of parts) {
    // A sentence ending in an ellipsis is an excerpt cut by someone else
    // (e.g. a 300-character API summary): stop before it.
    if (endsInEllipsis(s)) break;
    const next = summary ? `${summary} ${s}` : s;
    if (next.length > max) break;
    summary = next;
  }
  if (summary) return summary;
  const first = parts[0];
  return first.length <= SINGLE_SENTENCE_MAX && !endsInEllipsis(first) ? first : undefined;
}

export interface SeoFields {
  seo_title?: string | null;
  seo_meta_description?: string | null;
  seo_og_title?: string | null;
  seo_og_description?: string | null;
  seo_og_image_url?: string | null;
}

export interface SeoHeadInput extends SeoFields {
  /** The page's plain name (product/category/brand title). */
  name: string;
  /** The page's own description, used only when seo_meta_description is empty. */
  description?: string | null;
  /** Path ("/c/power/") or absolute URL of the canonical page. */
  canonical: string;
  /** Request origin, used when VITE_SITE_URL is unset. */
  origin?: string;
  /** Fallback share image when seo_og_image_url is empty. */
  image?: string | null;
  ogType?: "website" | "product";
  /** JSON-LD objects to emit as <script type="application/ld+json">. */
  jsonLd?: object[];
  /** Override site config (tests). */
  site?: SiteConfig;
}

function clean(v: string | null | undefined): string {
  return (v ?? "").replace(/\s+/g, " ").trim();
}

/** `seo_title`, else `${name} | ${site name}`, else the name alone. */
export function pageTitle(
  name: string,
  seoTitle?: string | null,
  site: SiteConfig = getSiteConfig(),
): string {
  const explicit = clean(seoTitle);
  if (explicit) return explicit;
  const plain = clean(name);
  if (!site.name) return plain;
  return plain ? `${plain} | ${site.name}` : site.name;
}

/** Absolute URL for a canonical path, on VITE_SITE_URL or the request origin. */
export function absoluteUrl(
  pathOrUrl: string,
  origin?: string,
  site: SiteConfig = getSiteConfig(),
): string {
  if (/^https?:\/\//i.test(pathOrUrl)) return pathOrUrl;
  const base = site.url || (origin ?? "").replace(/\/+$/, "");
  return `${base}${pathOrUrl.startsWith("/") ? "" : "/"}${pathOrUrl}`;
}

/** JSON for an inline <script>: `<` escaped so copy can never close the tag. */
export function jsonLdScript(data: object): string {
  return JSON.stringify(data)
    .replace(/</g, "\\u003c")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

export function buildSeoHead(input: SeoHeadInput): DocumentHeadValue {
  const site = input.site ?? getSiteConfig();
  const title = pageTitle(input.name, input.seo_title, site);
  const description =
    clean(input.seo_meta_description) || summarize(input.description);
  const ogTitle = clean(input.seo_og_title) || title;
  const ogDescription = clean(input.seo_og_description) || description;
  const canonical = absoluteUrl(input.canonical, input.origin, site);
  const image = clean(input.seo_og_image_url) || clean(input.image);

  const meta: { name?: string; property?: string; content: string }[] = [];
  if (description) meta.push({ name: "description", content: description });
  meta.push({ property: "og:title", content: ogTitle });
  if (ogDescription) {
    meta.push({ property: "og:description", content: ogDescription });
  }
  meta.push({ property: "og:type", content: input.ogType ?? "website" });
  meta.push({ property: "og:url", content: canonical });
  if (site.name) meta.push({ property: "og:site_name", content: site.name });
  if (image) meta.push({ property: "og:image", content: image });
  meta.push({
    name: "twitter:card",
    content: image ? "summary_large_image" : "summary",
  });

  return {
    title,
    meta,
    links: [{ rel: "canonical", href: canonical }],
    scripts: (input.jsonLd ?? []).map((data, i) => ({
      key: `ld-${i}`,
      props: { type: "application/ld+json" },
      script: jsonLdScript(data),
    })),
  };
}

export interface Crumb {
  name: string;
  /** Path or absolute URL. */
  url: string;
}

export function breadcrumbJsonLd(
  crumbs: Crumb[],
  origin?: string,
  site: SiteConfig = getSiteConfig(),
): object {
  return {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: crumbs.map((c, i) => ({
      "@type": "ListItem",
      position: i + 1,
      name: c.name,
      item: absoluteUrl(c.url, origin, site),
    })),
  };
}

export function collectionPageJsonLd(input: {
  name: string;
  url: string;
  description?: string;
  origin?: string;
  site?: SiteConfig;
}): object {
  const site = input.site ?? getSiteConfig();
  const data: Record<string, unknown> = {
    "@context": "https://schema.org",
    "@type": "CollectionPage",
    name: input.name,
    url: absoluteUrl(input.url, input.origin, site),
  };
  if (input.description) data.description = input.description;
  if (site.name) data.isPartOf = { "@type": "WebSite", name: site.name };
  return data;
}
