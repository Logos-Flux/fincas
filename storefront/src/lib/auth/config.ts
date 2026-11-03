import type { AuthProviderKind } from "./types";

export interface AuthConfig {
  /** VITE_AUTH_PROVIDER: "supabase" | "mock"; empty = choose from the other vars. */
  provider: string;
  supabaseUrl: string;
  supabaseAnonKey: string;
  /** True on the Vite dev server (and in vitest). */
  dev: boolean;
}

function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}

/**
 * Read the auth settings. Each key is the literal `import.meta.env.X`
 * expression on purpose: Vite only replaces that exact form at build time,
 * so a guarded or computed access would read nothing in the browser (see
 * the "import.meta.env literal" note in CLAUDE.md).
 */
export function readAuthConfig(): AuthConfig {
  return {
    provider: str(import.meta.env.VITE_AUTH_PROVIDER).toLowerCase(),
    supabaseUrl: str(import.meta.env.VITE_SUPABASE_URL),
    supabaseAnonKey: str(import.meta.env.VITE_SUPABASE_ANON_KEY),
    dev: import.meta.env.DEV === true,
  };
}

/**
 * Which provider to build:
 *
 * - `VITE_AUTH_PROVIDER=mock` always selects the mock (explicit opt-in, e.g.
 *   a demo build).
 * - Supabase URL + anon key present → Supabase.
 * - Neither, on the dev server (dev, e2e) → mock.
 * - Neither, in a production build → "unconfigured": sign-in reports that
 *   accounts are not set up instead of handing out fake sessions.
 */
export function selectAuthProviderKind(config: AuthConfig): AuthProviderKind {
  if (config.provider === "mock") return "mock";
  const hasSupabase = Boolean(config.supabaseUrl && config.supabaseAnonKey);
  if (config.provider === "supabase") return hasSupabase ? "supabase" : "unconfigured";
  if (config.provider) return "unconfigured"; // a provider this build does not implement
  if (hasSupabase) return "supabase";
  return config.dev ? "mock" : "unconfigured";
}
