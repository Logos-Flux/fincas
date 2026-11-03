/**
 * Tenant identity for the storefront: the one place a deployment names its
 * store. Nothing else in the storefront may hard-code a store name.
 *
 * | Setting          | Used for                                                  |
 * | ---------------- | --------------------------------------------------------- |
 * | `VITE_SITE_NAME` | the `| <name>` title suffix, `og:site_name`               |
 * | `VITE_SITE_URL`  | absolute canonical / `og:url` (else the request origin)   |
 *
 * Unset, the title is the page name alone: a missing setting must never put
 * another store's name (or golden's own) on a tenant's pages.
 */

function readSiteEnv(key: "VITE_SITE_NAME" | "VITE_SITE_URL"): string {
  // Literal `import.meta.env.VITE_*`: Vite only replaces the literal
  // expression (see the note in api-client.ts readEnv).
  let value: unknown;
  try {
    value =
      key === "VITE_SITE_NAME"
        ? import.meta.env.VITE_SITE_NAME
        : import.meta.env.VITE_SITE_URL;
  } catch {
    value = undefined;
  }
  if (typeof value === "string" && value.trim()) return value.trim();
  try {
    const fromProcess = (
      globalThis as { process?: { env?: Record<string, unknown> } }
    ).process?.env?.[key];
    if (typeof fromProcess === "string" && fromProcess.trim()) {
      return fromProcess.trim();
    }
  } catch {
    /* no process env in this runtime */
  }
  return "";
}

export interface SiteConfig {
  /** Store name for title suffixes; "" when the deployment set none. */
  name: string;
  /** Public origin without a trailing slash; "" means "use the request origin". */
  url: string;
}

export function getSiteConfig(): SiteConfig {
  return {
    name: readSiteEnv("VITE_SITE_NAME"),
    url: readSiteEnv("VITE_SITE_URL").replace(/\/+$/, ""),
  };
}
