import { AuthError, type AuthProvider, type AuthSession } from "./types";

/** The mock rejects exactly this password, so the login error state is testable. */
export const MOCK_REJECTED_PASSWORD = "wrong-password";

/** Prefix of every token the mock issues; the portal API rejects them. */
export const MOCK_TOKEN_PREFIX = "mock-access-token.";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/**
 * Development/e2e provider: accepts any well-formed email and non-empty
 * password (except MOCK_REJECTED_PASSWORD) and issues a fake session held in
 * memory only, like the Supabase adapter. A real ERP rejects its token with
 * 401/403, so it is only useful against a mocked portal API.
 */
export function createMockAuthProvider(now: () => number = Date.now): AuthProvider {
  let session: AuthSession | null = null;

  return {
    kind: "mock",
    async getSession() {
      if (session && session.expiresAt !== null && session.expiresAt <= now()) {
        session = null;
      }
      return session;
    },
    async signIn(email: string, password: string) {
      const normalized = email.trim().toLowerCase();
      if (!EMAIL_RE.test(normalized) || !password) {
        throw new AuthError("Enter your email address and password.");
      }
      if (password === MOCK_REJECTED_PASSWORD) {
        throw new AuthError("Invalid email or password.");
      }
      session = {
        accessToken: `${MOCK_TOKEN_PREFIX}${encodeURIComponent(normalized)}`,
        expiresAt: now() + 60 * 60 * 1000,
        user: { id: `mock-${normalized}`, email: normalized },
      };
      return session;
    },
    async signOut() {
      session = null;
    },
    async getAccessToken() {
      return (await this.getSession())?.accessToken ?? null;
    },
  };
}
