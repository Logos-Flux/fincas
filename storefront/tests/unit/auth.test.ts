import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AuthError,
  createAuthProvider,
  getAuthProvider,
  loginPathFor,
  MOCK_REJECTED_PASSWORD,
  MOCK_TOKEN_PREFIX,
  readAuthConfig,
  safeNextPath,
  selectAuthProviderKind,
  __resetAuthProviderForTests,
  type AuthConfig,
} from "../../src/lib/auth";

function config(overrides: Partial<AuthConfig> = {}): AuthConfig {
  return {
    provider: "",
    supabaseUrl: "",
    supabaseAnonKey: "",
    dev: false,
    ...overrides,
  };
}

afterEach(() => {
  __resetAuthProviderForTests();
  vi.unstubAllEnvs();
});

describe("selectAuthProviderKind", () => {
  it("uses Supabase when both the URL and anon key are set", () => {
    expect(
      selectAuthProviderKind(
        config({
          supabaseUrl: "https://x.supabase.co",
          supabaseAnonKey: "anon",
        }),
      ),
    ).toBe("supabase");
  });

  it("falls back to the mock on the dev server when Supabase is not configured", () => {
    expect(selectAuthProviderKind(config({ dev: true }))).toBe("mock");
    expect(
      selectAuthProviderKind(
        config({ dev: true, supabaseUrl: "https://x.supabase.co" }),
      ),
    ).toBe("mock");
  });

  it("never hands out mock sessions in a production build without auth settings", () => {
    expect(selectAuthProviderKind(config({ dev: false }))).toBe("unconfigured");
  });

  it("honours an explicit VITE_AUTH_PROVIDER", () => {
    expect(selectAuthProviderKind(config({ provider: "mock" }))).toBe("mock");
    expect(
      selectAuthProviderKind(config({ provider: "supabase", dev: true })),
    ).toBe("unconfigured");
    expect(
      selectAuthProviderKind(config({ provider: "oidc", dev: true })),
    ).toBe("unconfigured");
    expect(
      selectAuthProviderKind(
        config({
          provider: "mock",
          supabaseUrl: "https://x.supabase.co",
          supabaseAnonKey: "anon",
        }),
      ),
    ).toBe("mock");
  });
});

describe("readAuthConfig", () => {
  it("reads the VITE_* auth settings", () => {
    vi.stubEnv("VITE_SUPABASE_URL", " https://proj.supabase.co ");
    vi.stubEnv("VITE_SUPABASE_ANON_KEY", "anon-key");
    vi.stubEnv("VITE_AUTH_PROVIDER", "Supabase");
    expect(readAuthConfig()).toMatchObject({
      provider: "supabase",
      supabaseUrl: "https://proj.supabase.co",
      supabaseAnonKey: "anon-key",
    });
  });

  it("selects the mock under vitest (a dev environment) with no settings", () => {
    vi.stubEnv("VITE_SUPABASE_URL", "");
    vi.stubEnv("VITE_SUPABASE_ANON_KEY", "");
    expect(getAuthProvider().kind).toBe("mock");
  });

  it("selects Supabase from the env without loading the SDK until it is used", () => {
    vi.stubEnv("VITE_SUPABASE_URL", "https://proj.supabase.co");
    vi.stubEnv("VITE_SUPABASE_ANON_KEY", "anon-key");
    expect(getAuthProvider().kind).toBe("supabase");
  });
});

describe("mock provider", () => {
  it("issues an in-memory session and clears it on sign-out", async () => {
    const auth = createAuthProvider(config({ provider: "mock" }));
    expect(await auth.getSession()).toBeNull();
    expect(await auth.getAccessToken()).toBeNull();

    const session = await auth.signIn("Pat@Example.com ", "secret-pass");
    expect(session.user.email).toBe("pat@example.com");
    expect(session.accessToken.startsWith(MOCK_TOKEN_PREFIX)).toBe(true);
    expect(await auth.getAccessToken()).toBe(session.accessToken);

    await auth.signOut();
    expect(await auth.getSession()).toBeNull();
  });

  it("rejects bad input and the documented wrong password", async () => {
    const auth = createAuthProvider(config({ provider: "mock" }));
    await expect(auth.signIn("not-an-email", "x")).rejects.toBeInstanceOf(
      AuthError,
    );
    await expect(auth.signIn("pat@example.com", "")).rejects.toBeInstanceOf(
      AuthError,
    );
    await expect(
      auth.signIn("pat@example.com", MOCK_REJECTED_PASSWORD),
    ).rejects.toThrow("Invalid email or password.");
    expect(await auth.getSession()).toBeNull();
  });
});

describe("unconfigured provider", () => {
  it("is never signed in and explains why sign-in fails", async () => {
    const auth = createAuthProvider(config());
    expect(auth.kind).toBe("unconfigured");
    expect(await auth.getAccessToken()).toBeNull();
    await expect(auth.signIn("pat@example.com", "pw")).rejects.toThrow(
      /not configured/,
    );
  });
});

describe("safeNextPath", () => {
  it("keeps same-site paths", () => {
    expect(safeNextPath("/account/orders/abc/?x=1")).toBe(
      "/account/orders/abc/?x=1",
    );
  });

  it("rejects off-site and looping destinations", () => {
    for (const bad of [
      null,
      "",
      "https://evil.example",
      "//evil.example",
      "/\\evil.example",
      "/\t/evil.example",
      "/\n/evil.example",
      "/\\/evil.example",
      "/.//evil.example",
      "/..//evil.example",
      "/%2e//evil.example",
      "account",
    ]) {
      expect(safeNextPath(bad)).toBe("/account/");
    }
    expect(safeNextPath("/account/login?next=/x")).toBe("/account/");
    expect(safeNextPath("/account/login/?next=/x")).toBe("/account/");
  });

  it("builds the login URL with the encoded destination", () => {
    expect(loginPathFor("/account/orders/?status=shipped")).toBe(
      "/account/login/?next=%2Faccount%2Forders%2F%3Fstatus%3Dshipped",
    );
  });
});
