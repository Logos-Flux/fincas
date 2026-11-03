/**
 * Customer auth adapter contract (identity-unification spec §5).
 *
 * The /account pages only ever talk to an AuthProvider, so swapping
 * Supabase for another identity provider (OIDC PKCE, per the spec) is a new
 * implementation of this interface, not a change to the pages.
 */

export type AuthProviderKind = "supabase" | "mock" | "unconfigured";

export interface AuthUser {
  id: string;
  email: string;
}

export interface AuthSession {
  /** Bearer token for `/api/v1/portal/...`. */
  accessToken: string;
  /** Epoch milliseconds, or null when the provider does not say. */
  expiresAt: number | null;
  user: AuthUser;
}

export interface AuthProvider {
  readonly kind: AuthProviderKind;
  /** The current session, refreshed by the provider when it can; null when signed out. */
  getSession(): Promise<AuthSession | null>;
  /** Resolves with a session or rejects with an AuthError carrying a user-facing message. */
  signIn(email: string, password: string): Promise<AuthSession>;
  signOut(): Promise<void>;
  /** Shortcut for `(await getSession())?.accessToken ?? null`. */
  getAccessToken(): Promise<string | null>;
}

export class AuthError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AuthError";
  }
}
