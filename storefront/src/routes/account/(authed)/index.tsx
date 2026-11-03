import {
  component$,
  useContext,
  useSignal,
  useVisibleTask$,
} from "@builder.io/qwik";
import { Link, useLocation, type DocumentHead } from "@builder.io/qwik-city";
import { AccountContext } from "../../../context/account-context";
import { getAuthProvider } from "../../../lib/auth";
import {
  createPortalClient,
  humanizeStatus,
  type PortalModuleCounts,
} from "../../../lib/portal-client";
import { portalFailure } from "../../../components/account/auth-redirect";
import { Notice, Panel } from "../../../components/account/panel";

const COUNT_TILES: {
  key: keyof PortalModuleCounts;
  label: string;
  href?: string;
}[] = [
  { key: "orders", label: "Orders", href: "/account/orders/" },
  { key: "quotes", label: "Quotes" },
  { key: "invoices", label: "Invoices" },
  { key: "product_docs", label: "Product documents" },
];

export default component$(() => {
  const account = useContext(AccountContext);
  const loc = useLocation();
  const counts = useSignal<PortalModuleCounts | null>(null);
  const countsError = useSignal("");

  // eslint-disable-next-line qwik/no-use-visible-task
  useVisibleTask$(async ({ track }) => {
    if (track(() => account.status) !== "ready") return;
    try {
      counts.value = await createPortalClient(getAuthProvider()).moduleCounts();
    } catch (err) {
      countsError.value = (await portalFailure(err, loc.url.pathname)) ?? "";
    }
  });

  const profile = account.profile;

  return (
    <div class="space-y-6">
      <h1
        class="text-2xl sm:text-3xl font-black tracking-tight"
        style={{ color: "var(--color-t-bright, #f7f8f8)" }}
      >
        {profile
          ? `Welcome back, ${profile.first_name || profile.full_name}`
          : "Account overview"}
      </h1>

      <Panel>
        <h2
          class="text-sm font-bold uppercase tracking-wider mb-4"
          style={{ color: "var(--color-t-bright, #f7f8f8)" }}
        >
          Profile
        </h2>
        {profile ? (
          <dl class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3 text-sm">
            {[
              ["Name", profile.full_name],
              ["Email", profile.email],
              ["Company", profile.company_name],
              ["Phone", profile.phone],
              ["Account type", humanizeStatus(profile.customer_type)],
              ["Tax exempt", profile.is_tax_exempt ? "Yes" : "No"],
            ].map(([label, value]) => (
              <div key={label} class="min-w-0">
                <dt
                  class="text-xs font-semibold uppercase tracking-wider"
                  style={{ color: "var(--color-t-text, #d0d6e0)" }}
                >
                  {label}
                </dt>
                <dd
                  class="break-words"
                  style={{ color: "var(--color-t-bright, #f7f8f8)" }}
                >
                  {value || "—"}
                </dd>
              </div>
            ))}
          </dl>
        ) : account.profileError ? (
          <Notice tone="alert">{account.profileError}</Notice>
        ) : (
          <Notice>Loading your profile…</Notice>
        )}
      </Panel>

      <Panel>
        <h2
          class="text-sm font-bold uppercase tracking-wider mb-4"
          style={{ color: "var(--color-t-bright, #f7f8f8)" }}
        >
          Activity
        </h2>
        {counts.value ? (
          <ul
            class="grid grid-cols-2 sm:grid-cols-4 gap-3"
            aria-label="Account totals"
          >
            {COUNT_TILES.map((tile) => {
              const body = (
                <>
                  <span
                    class="block text-2xl font-black"
                    style={{ color: "var(--color-t-bright, #f7f8f8)" }}
                  >
                    {counts.value![tile.key]}
                  </span>
                  <span
                    class="block text-xs font-semibold uppercase tracking-wider"
                    style={{ color: "var(--color-t-text, #d0d6e0)" }}
                  >
                    {tile.label}
                  </span>
                </>
              );
              const boxStyle = {
                backgroundColor: "var(--color-t-surface, #0f1011)",
                borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
              };
              return (
                <li key={tile.key}>
                  {tile.href ? (
                    <Link
                      href={tile.href}
                      class="block rounded-xl border p-4 hover:opacity-90"
                      style={boxStyle}
                    >
                      {body}
                    </Link>
                  ) : (
                    <div class="rounded-xl border p-4" style={boxStyle}>
                      {body}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        ) : countsError.value ? (
          <Notice tone="alert">{countsError.value}</Notice>
        ) : (
          <Notice>Loading account totals…</Notice>
        )}
        <p class="mt-4 text-sm">
          <Link
            href="/account/orders/"
            class="font-semibold underline"
            style={{ color: "var(--color-t-accent-ink, #9da3ff)" }}
          >
            View your orders →
          </Link>
        </p>
      </Panel>
    </div>
  );
});

export const head: DocumentHead = {
  title: "Your account | Fincas Store",
  meta: [{ name: "robots", content: "noindex" }],
};
