import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { AuthError, type AuthProvider, type AuthSession } from "./types";

function toAuthSession(s: Session | null): AuthSession | null {
  if (!s?.access_token) return null;
  return {
    accessToken: s.access_token,
    expiresAt: typeof s.expires_at === "number" ? s.expires_at * 1000 : null,
    user: { id: s.user.id, email: s.user.email ?? "" },
  };
}

/**
 * Supabase Auth (email + password). The SDK is imported lazily so shoppers
 * who never open /account do not download it.
 *
 * Tokens stay in memory (`persistSession: false`) with the SDK's
 * auto-refresh, as the identity spec asks: nothing lands in localStorage,
 * at the cost of a full page reload signing the customer out.
 */
export function createSupabaseAuthProvider(url: string, anonKey: string): AuthProvider {
  let clientPromise: Promise<SupabaseClient> | null = null;
  const client = () => {
    if (!clientPromise) {
      clientPromise = import("@supabase/supabase-js").then(({ createClient }) =>
        createClient(url, anonKey, {
          auth: {
            persistSession: false,
            autoRefreshToken: true,
            detectSessionInUrl: false,
          },
        }),
      );
    }
    return clientPromise;
  };

  return {
    kind: "supabase",
    async getSession() {
      const { data, error } = await (await client()).auth.getSession();
      if (error) return null;
      return toAuthSession(data.session);
    },
    async signIn(email: string, password: string) {
      let result;
      try {
        result = await (await client()).auth.signInWithPassword({
          email: email.trim(),
          password,
        });
      } catch {
        throw new AuthError("Could not reach the sign-in service. Check your connection and try again.");
      }
      const { data, error } = result;
      if (error) {
        // Supabase answers 400 "Invalid login credentials" for both a wrong
        // password and an unknown email; keep it that vague on purpose.
        throw new AuthError(
          error.status === 400 ? "Invalid email or password." : error.message || "Sign-in failed.",
        );
      }
      const session = toAuthSession(data.session);
      if (!session) {
        throw new AuthError("Confirm your email address first, then sign in.");
      }
      return session;
    },
    async signOut() {
      await (await client()).auth.signOut({ scope: "local" });
    },
    async getAccessToken() {
      return (await this.getSession())?.accessToken ?? null;
    },
  };
}
