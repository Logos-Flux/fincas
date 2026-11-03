import { afterEach, describe, expect, it, vi } from "vitest";
import {
  absoluteUrl,
  breadcrumbJsonLd,
  buildSeoHead,
  jsonLdScript,
  pageTitle,
  summarize,
  toPlainText,
} from "../../src/lib/seo-head";
import { getSiteConfig } from "../../src/lib/site-config";

const SITE = { name: "Example Supply", url: "https://shop.example.com" };
const NO_SITE = { name: "", url: "" };

function meta(head: ReturnType<typeof buildSeoHead>, key: string) {
  return head.meta?.find((m) => m.name === key || m.property === key)?.content;
}

afterEach(() => vi.unstubAllEnvs());

describe("pageTitle", () => {
  it("uses seo_title verbatim when the ERP set one", () => {
    expect(
      pageTitle("Batteries", "Off-grid batteries, sized right", SITE),
    ).toBe("Off-grid batteries, sized right");
  });

  it("falls back to name | site name from tenant config", () => {
    expect(pageTitle("Batteries", "", SITE)).toBe("Batteries | Example Supply");
    expect(pageTitle("Batteries", "   ", SITE)).toBe(
      "Batteries | Example Supply",
    );
  });

  it("is the name alone when no site name is configured (never a hard-coded brand)", () => {
    expect(pageTitle("Batteries", null, NO_SITE)).toBe("Batteries");
  });

  it("reads the site name from VITE_SITE_NAME", () => {
    vi.stubEnv("VITE_SITE_NAME", "Tenant Co");
    expect(getSiteConfig().name).toBe("Tenant Co");
    expect(pageTitle("Cables")).toBe("Cables | Tenant Co");
  });
});

describe("summarize", () => {
  it("keeps whole sentences that fit and never cuts mid-sentence", () => {
    const text =
      "Lithium batteries for cabins and boats. Each pack ships with a battery management system. " +
      "Every model is rated for six thousand cycles at eighty percent depth of discharge, and comes with a ten-year warranty.";
    const out = summarize(text, 160)!;
    expect(out).toBe(
      "Lithium batteries for cabins and boats. Each pack ships with a battery management system.",
    );
    expect(out.length).toBeLessThanOrEqual(160);
    expect(out.endsWith(".")).toBe(true);
    expect(out).not.toContain("…");
  });

  it("does not split on decimals or lower-case abbreviations", () => {
    expect(
      summarize("A 3.5 kW unit, e.g. for a cabin. Second sentence.", 40),
    ).toBe("A 3.5 kW unit, e.g. for a cabin.");
  });

  it("uses a long first sentence whole, and omits one that is far too long", () => {
    const long = `${"word ".repeat(40).trim()}.`; // ~200 chars
    expect(summarize(long, 160)).toBe(long);
    expect(summarize(`${"word ".repeat(80).trim()}.`, 160)).toBeUndefined();
  });

  it("strips HTML and entities and returns undefined for empty input", () => {
    expect(summarize("<p>Quiet &amp; <b>strong</b>.</p><p>Second.</p>")).toBe(
      "Quiet & strong. Second.",
    );
    expect(summarize("")).toBeUndefined();
    expect(summarize(null)).toBeUndefined();
    expect(summarize("<p> </p>")).toBeUndefined();
  });

  it("drops markdown syntax", () => {
    expect(toPlainText("## About\n\nSee **real** [ground mounts](/g/).")).toBe(
      "About See real ground mounts.",
    );
  });
});

describe("buildSeoHead", () => {
  it("uses every seo_* field when set", () => {
    const head = buildSeoHead({
      name: "Batteries",
      seo_title: "Batteries for remote sites",
      seo_meta_description: "Sized for cabins.",
      seo_og_title: "Share title",
      seo_og_description: "Share text.",
      seo_og_image_url: "https://cdn.example.com/og.jpg",
      description: "Ignored when the meta description is set.",
      canonical: "/c/batteries/",
      site: SITE,
    });
    expect(head.title).toBe("Batteries for remote sites");
    expect(meta(head, "description")).toBe("Sized for cabins.");
    expect(meta(head, "og:title")).toBe("Share title");
    expect(meta(head, "og:description")).toBe("Share text.");
    expect(meta(head, "og:image")).toBe("https://cdn.example.com/og.jpg");
    expect(meta(head, "og:site_name")).toBe("Example Supply");
    expect(meta(head, "og:url")).toBe("https://shop.example.com/c/batteries/");
    expect(head.links).toEqual([
      { rel: "canonical", href: "https://shop.example.com/c/batteries/" },
    ]);
  });

  it("falls back to name | site, a sentence summary, and og = title/description", () => {
    const head = buildSeoHead({
      name: "Batteries",
      description: "<p>Packs for cabins. Built to last.</p>",
      canonical: "/c/batteries/",
      origin: "http://localhost:5173",
      site: { name: "Example Supply", url: "" },
    });
    expect(head.title).toBe("Batteries | Example Supply");
    expect(meta(head, "description")).toBe("Packs for cabins. Built to last.");
    expect(meta(head, "og:title")).toBe("Batteries | Example Supply");
    expect(meta(head, "og:description")).toBe(
      "Packs for cabins. Built to last.",
    );
    expect(head.links?.[0].href).toBe("http://localhost:5173/c/batteries/");
  });

  it("omits the description entirely when there is nothing to summarize", () => {
    const head = buildSeoHead({
      name: "Batteries",
      canonical: "/c/b/",
      site: NO_SITE,
    });
    expect(meta(head, "description")).toBeUndefined();
    expect(meta(head, "og:description")).toBeUndefined();
    expect(meta(head, "og:site_name")).toBeUndefined();
    expect(head.title).toBe("Batteries");
  });

  it("keeps an absolute canonical (ERP seo_canonical_url) as-is", () => {
    expect(
      absoluteUrl("https://other.example.com/x/", "http://localhost", SITE),
    ).toBe("https://other.example.com/x/");
  });

  it("emits JSON-LD that cannot close its script tag", () => {
    const head = buildSeoHead({
      name: "X",
      canonical: "/c/x/",
      site: SITE,
      jsonLd: [
        breadcrumbJsonLd(
          [{ name: "</script><b>", url: "/c/x/" }],
          undefined,
          SITE,
        ),
      ],
    });
    const script = head.scripts?.[0];
    expect(script?.props).toEqual({ type: "application/ld+json" });
    expect(script?.script).not.toContain("</script>");
    const parsed = JSON.parse(script!.script!);
    expect(parsed["@type"]).toBe("BreadcrumbList");
    expect(parsed.itemListElement[0]).toEqual({
      "@type": "ListItem",
      position: 1,
      name: "</script><b>",
      item: "https://shop.example.com/c/x/",
    });
    expect(jsonLdScript({ a: "<" })).toBe('{"a":"\\u003c"}');
  });
});

describe("entities and cut-off excerpts", () => {
  it("decodes numeric and common typographic entities", () => {
    expect(
      toPlainText(
        "It&rsquo;s &ldquo;quiet&rdquo; &mdash; 5&ndash;10 kW&hellip; &#169; &#x2122; &#8217;",
      ),
    ).toBe("It’s “quiet” — 5–10 kW… © ™ ’");
    expect(toPlainText("&#0; &#xFFFFFFF; ok")).toBe("ok");
  });

  it("never returns a summary that ends in an ellipsis", () => {
    expect(
      summarize(
        "First sentence here. Then a cut-off excerpt that goes on and…",
      ),
    ).toBe("First sentence here.");
    expect(summarize("Only a cut-off excerpt that goes on...")).toBeUndefined();
    expect(
      summarize("Only a cut-off excerpt that goes on&hellip;"),
    ).toBeUndefined();
  });

  it("still collapses the stray dots block tags leave", () => {
    expect(summarize("<p>One.</p><p>Two.</p>")).toBe("One. Two.");
  });
});
