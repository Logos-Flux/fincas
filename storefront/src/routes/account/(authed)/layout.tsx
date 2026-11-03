import {
  $,
  component$,
  Slot,
  useContextProvider,
  useStore,
  useVisibleTask$,
} from "@builder.io/qwik";
import { Link, useLocation, useNavigate } from "@builder.io/qwik-city";
import {
  AccountContext,
  type AccountStore,
} from "../../../context/account-context";
import { getAuthProvider } from "../../../lib/auth";
import { createPortalClient } from "../../../lib/portal-client";
import {
  portalFailure,
  redirectToLogin,
} from "../../../components/account/auth-redirect";
import { Notice } from "../../../components/account/panel";

const SECTIONS = [
  {
    href: "/account/",
    label: "Overview",
    match: (p: string) => p === "/account/",
  },
  {
    href: "/account/orders/",
    label: "Orders",
    match: (p: string) => p.startsWith("/account/orders"),
  },
];

/**
 * Gate for every signed-in /account page. Auth lives in the browser (the
 * adapter keeps tokens in memory), so the check runs client-side: logged-out
 * visitors are sent to /account/login?next=<this page>.
 */
export default component$(() => {
  const loc = useLocation();
  const nav = useNavigate();
  const account = useStore<AccountStore>({
    status: "checking",
    email: "",
    profile: null,
    profileError: "",
  });
  useContextProvider(AccountContext, account);

  // eslint-disable-next-line qwik/no-use-visible-task
  useVisibleTask$(async ({ track }) => {
    const navigating = track(() => loc.isNavigating);
    if (navigating || account.status === "ready") return;
    const here = loc.url.pathname + loc.url.search;
    const session = await getAuthProvider().getSession();
    if (!session) {
      redirectToLogin(here);
      return;
    }
    account.email = session.user.email;
    account.status = "ready";
    try {
      account.profile = await createPortalClient(getAuthProvider()).profile();
    } catch (err) {
      account.profileError = (await portalFailure(err, here)) ?? "";
    }
  });

  const signOut = $(async () => {
    try {
      await getAuthProvider().signOut();
    } finally {
      await nav("/account/login/?signed_out=1");
    }
  });

  const path = loc.url.pathname.endsWith("/")
    ? loc.url.pathname
    : `${loc.url.pathname}/`;

  return (
    <div class="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-6">
      <div class="flex flex-wrap items-end justify-between gap-4">
        <div class="space-y-1">
          <p
            class="text-xs font-semibold uppercase tracking-wider"
            style={{ color: "var(--color-t-accent-ink, #9da3ff)" }}
          >
            Your account
          </p>
          {account.email ? (
            <p
              class="text-sm break-all"
              style={{ color: "var(--color-t-text, #d0d6e0)" }}
            >
              Signed in as{" "}
              <span
                class="font-semibold"
                style={{ color: "var(--color-t-bright, #f7f8f8)" }}
              >
                {account.email}
              </span>
            </p>
          ) : null}
        </div>
        {account.status === "ready" ? (
          <button
            type="button"
            onClick$={signOut}
            class="px-3 py-1.5 rounded-lg border text-sm font-semibold cursor-pointer hover:opacity-90"
            style={{
              backgroundColor: "var(--color-t-card, #191a1b)",
              borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
              color: "var(--color-t-bright, #f7f8f8)",
            }}
          >
            Sign out
          </button>
        ) : null}
      </div>

      <nav
        aria-label="Account sections"
        class="flex gap-1 border-b"
        style={{
          borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
        }}
      >
        {SECTIONS.map((s) => {
          const active = s.match(path);
          return (
            <Link
              key={s.href}
              href={s.href}
              aria-current={active ? "page" : undefined}
              class="px-3 py-2 -mb-px border-b-2 text-sm font-semibold"
              style={{
                color: active
                  ? "var(--color-t-bright, #f7f8f8)"
                  : "var(--color-t-text, #d0d6e0)",
                borderColor: active
                  ? "var(--color-t-accent, #5e6ad2)"
                  : "transparent",
              }}
            >
              {s.label}
            </Link>
          );
        })}
      </nav>

      {account.status === "checking" ? (
        <Notice>Checking your session…</Notice>
      ) : (
        <Slot />
      )}
    </div>
  );
});
