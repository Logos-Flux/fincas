import { AuthError, type AuthProvider } from "./types";

/** Production build without auth settings: never signed in, sign-in explains why. */
export function createUnconfiguredAuthProvider(): AuthProvider {
  return {
    kind: "unconfigured",
    async getSession() {
      return null;
    },
    async signIn() {
      throw new AuthError(
        "Customer accounts are not configured for this store yet. Please contact us about your order.",
      );
    },
    async signOut() {},
    async getAccessToken() {
      return null;
    },
  };
}
