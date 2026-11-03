import { readAuthConfig, selectAuthProviderKind, type AuthConfig } from "./config";
import { createMockAuthProvider } from "./mock";
import { createSupabaseAuthProvider } from "./supabase";
import { createUnconfiguredAuthProvider } from "./unconfigured";
import type { AuthProvider } from "./types";

export * from "./types";
export { readAuthConfig, selectAuthProviderKind, type AuthConfig } from "./config";
export { MOCK_REJECTED_PASSWORD, MOCK_TOKEN_PREFIX } from "./mock";

export function createAuthProvider(config: AuthConfig = readAuthConfig()): AuthProvider {
  switch (selectAuthProviderKind(config)) {
    case "supabase":
      return createSupabaseAuthProvider(config.supabaseUrl, config.supabaseAnonKey);
    case "mock":
      return createMockAuthProvider();
    default:
      return createUnconfiguredAuthProvider();
  }
}

let provider: AuthProvider | null = null;

/** The page-lifetime provider (sessions live in its memory). Browser-only. */
export function getAuthProvider(): AuthProvider {
  if (!provider) provider = createAuthProvider();
  return provider;
}

/** Test-only: drop the cached provider. */
export function __resetAuthProviderForTests(): void {
  provider = null;
}

/**
 * The post-login destination from a `next` query value. Only same-site
 * paths are honoured, so the login page cannot be used as an open redirect.
 * The value is resolved with the URL parser rather than prefix checks,
 * because the parser drops tabs/newlines and treats `\` as `/`: a raw
 * "/\t/evil.example" passes a startsWith("//") test yet resolves off-site.
 */
export function safeNextPath(next: string | null | undefined, fallback = "/account/"): string {
  if (!next || !next.startsWith("/")) return fallback;
  const base = "https://storefront.invalid";
  let url: URL;
  try {
    url = new URL(next, base);
  } catch {
    return fallback;
  }
  if (url.origin !== base) return fallback;
  // Dot segments normalise "/.//evil.example" to the path "//evil.example",
  // which a router would read as a protocol-relative URL: refuse it.
  if (url.pathname.startsWith("//") || url.pathname.startsWith("/\\")) return fallback;
  if (url.pathname.startsWith("/account/login")) return fallback;
  return url.pathname + url.search + url.hash;
}

/** `/account/login?next=…` for a protected path the visitor tried to open. */
export function loginPathFor(path: string): string {
  return `/account/login/?next=${encodeURIComponent(path)}`;
}
