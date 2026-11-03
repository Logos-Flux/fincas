import { component$ } from "@builder.io/qwik";
import { Link, type DocumentHead } from "@builder.io/qwik-city";

// Placeholder until the flow ships (identity-unification spec, phase 5b).
export default component$(() => (
  <div class="max-w-md mx-auto px-4 sm:px-6 py-12 space-y-4">
    <h1
      class="text-3xl font-black tracking-tight"
      style={{ color: "var(--color-t-bright, #f7f8f8)" }}
    >
      Reset your password
    </h1>
    <p class="text-sm" style={{ color: "var(--color-t-text, #d0d6e0)" }}>
      Self-service password reset is not available yet. Contact us and we will
      send you a reset link.
    </p>
    <Link
      href="/account/login/"
      class="inline-block text-sm font-semibold underline"
      style={{ color: "var(--color-t-accent-ink, #9da3ff)" }}
    >
      ← Back to sign in
    </Link>
  </div>
));

export const head: DocumentHead = {
  title: "Reset password | Fincas Store",
  meta: [{ name: "robots", content: "noindex" }],
};
