import { $, component$, useSignal, useVisibleTask$ } from "@builder.io/qwik";
import {
  Link,
  useLocation,
  useNavigate,
  type DocumentHead,
} from "@builder.io/qwik-city";
import {
  AuthError,
  getAuthProvider,
  MOCK_REJECTED_PASSWORD,
  safeNextPath,
} from "../../../lib/auth";

const inputClass =
  "w-full rounded-lg border px-3 py-2 text-sm outline-none focus-visible:ring-2";
const inputStyle = {
  backgroundColor: "var(--color-t-surface, #0f1011)",
  borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.16))",
  color: "var(--color-t-bright, #f7f8f8)",
};

export default component$(() => {
  const loc = useLocation();
  const nav = useNavigate();
  const email = useSignal("");
  const password = useSignal("");
  const pending = useSignal(false);
  const error = useSignal("");
  const isMock = useSignal(false);

  const params = loc.url.searchParams;
  const notice =
    params.get("signed_out") === "1"
      ? "You have been signed out."
      : params.get("reason") === "expired"
        ? "Your session has ended. Sign in again to continue."
        : params.get("next")
          ? "Sign in to view your account."
          : "";

  // Already signed in (e.g. the Account link while a session is live): go on.
  // eslint-disable-next-line qwik/no-use-visible-task
  useVisibleTask$(async () => {
    const provider = getAuthProvider();
    isMock.value = provider.kind === "mock";
    if (await provider.getSession()) {
      await nav(safeNextPath(loc.url.searchParams.get("next")), {
        replaceState: true,
      });
    }
  });

  // Read the fields from the form itself, not only the bound signals: a
  // password manager's autofill (or input typed before the page resumed)
  // may never fire the input event the binding listens for.
  const submit = $(async (_: SubmitEvent, form: HTMLFormElement) => {
    const data = new FormData(form);
    email.value = String(data.get("email") ?? "");
    password.value = String(data.get("password") ?? "");
    error.value = "";
    if (!email.value.trim() || !password.value) {
      error.value = "Enter your email address and password.";
      return;
    }
    pending.value = true;
    try {
      await getAuthProvider().signIn(email.value, password.value);
      password.value = "";
      await nav(safeNextPath(loc.url.searchParams.get("next")));
    } catch (err) {
      error.value =
        err instanceof AuthError
          ? err.message
          : "Sign-in failed. Please try again.";
    } finally {
      pending.value = false;
    }
  });

  return (
    <div class="max-w-md mx-auto px-4 sm:px-6 py-12 space-y-6">
      <h1
        class="text-3xl font-black tracking-tight"
        style={{ color: "var(--color-t-bright, #f7f8f8)" }}
      >
        Sign in to your account
      </h1>

      {notice ? (
        <p
          role="status"
          class="text-sm"
          style={{ color: "var(--color-t-text, #d0d6e0)" }}
        >
          {notice}
        </p>
      ) : null}

      <form
        preventdefault:submit
        onSubmit$={submit}
        noValidate
        class="rounded-2xl border p-6 space-y-4"
        style={{
          backgroundColor: "var(--color-t-card, #191a1b)",
          borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
        }}
      >
        <div class="space-y-1">
          <label
            for="account-email"
            class="block text-sm font-semibold"
            style={{ color: "var(--color-t-bright, #f7f8f8)" }}
          >
            Email
          </label>
          <input
            id="account-email"
            type="email"
            name="email"
            autoComplete="email"
            required
            bind:value={email}
            aria-invalid={error.value ? "true" : undefined}
            aria-describedby={error.value ? "account-login-error" : undefined}
            class={inputClass}
            style={inputStyle}
          />
        </div>
        <div class="space-y-1">
          <label
            for="account-password"
            class="block text-sm font-semibold"
            style={{ color: "var(--color-t-bright, #f7f8f8)" }}
          >
            Password
          </label>
          <input
            id="account-password"
            type="password"
            name="password"
            autoComplete="current-password"
            required
            bind:value={password}
            aria-invalid={error.value ? "true" : undefined}
            aria-describedby={error.value ? "account-login-error" : undefined}
            class={inputClass}
            style={inputStyle}
          />
        </div>

        <div aria-live="assertive">
          {error.value ? (
            <p
              id="account-login-error"
              role="alert"
              class="rounded-lg border px-3 py-2 text-sm font-semibold"
              style={{
                borderColor: "var(--color-t-error, #ef4444)",
                color: "var(--color-t-bright, #f7f8f8)",
              }}
            >
              {error.value}
            </p>
          ) : null}
        </div>

        <button
          type="submit"
          disabled={pending.value}
          class="w-full rounded-lg px-4 py-2.5 text-sm font-bold cursor-pointer disabled:cursor-wait disabled:opacity-80"
          style={{
            backgroundColor: "var(--color-t-accent, #5e6ad2)",
            color: "var(--color-t-on-accent, #ffffff)",
          }}
        >
          {pending.value ? "Signing in…" : "Sign in"}
        </button>

        <div class="flex flex-wrap justify-between gap-2 text-sm">
          <Link
            href="/account/reset/"
            class="underline"
            style={{ color: "var(--color-t-accent-ink, #9da3ff)" }}
          >
            Forgot your password?
          </Link>
          <Link
            href="/account/register/"
            class="underline"
            style={{ color: "var(--color-t-accent-ink, #9da3ff)" }}
          >
            Create an account
          </Link>
        </div>
      </form>

      {isMock.value ? (
        <p class="text-xs" style={{ color: "var(--color-t-text, #d0d6e0)" }}>
          Development sign-in: no auth provider is configured, so any email and
          password work (except "{MOCK_REJECTED_PASSWORD}"). Set
          VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY to use Supabase.
        </p>
      ) : null}
    </div>
  );
});

export const head: DocumentHead = {
  title: "Sign in | Fincas Store",
  meta: [{ name: "robots", content: "noindex" }],
};
