import {
  component$,
  useContext,
  useSignal,
  useVisibleTask$,
} from "@builder.io/qwik";
import { Link, useLocation, type DocumentHead } from "@builder.io/qwik-city";
import { AccountContext } from "../../../../context/account-context";
import { getAuthProvider } from "../../../../lib/auth";
import {
  createPortalClient,
  formatDate,
  formatMoney,
  type PortalOrderSummary,
} from "../../../../lib/portal-client";
import { portalFailure } from "../../../../components/account/auth-redirect";
import { Notice, Panel } from "../../../../components/account/panel";
import { StatusBadge } from "../../../../components/account/status-badge";

export default component$(() => {
  const account = useContext(AccountContext);
  const loc = useLocation();
  const orders = useSignal<PortalOrderSummary[] | null>(null);
  const error = useSignal("");

  // eslint-disable-next-line qwik/no-use-visible-task
  useVisibleTask$(async ({ track }) => {
    if (track(() => account.status) !== "ready") return;
    try {
      orders.value = await createPortalClient(getAuthProvider()).orders();
    } catch (err) {
      error.value = (await portalFailure(err, loc.url.pathname)) ?? "";
    }
  });

  const currency = account.profile?.default_currency || null;
  const cell = "px-3 py-3 align-middle";
  const head =
    "px-3 py-2 text-left text-xs font-semibold uppercase tracking-wider";

  return (
    <div class="space-y-6">
      <h1
        class="text-2xl sm:text-3xl font-black tracking-tight"
        style={{ color: "var(--color-t-bright, #f7f8f8)" }}
      >
        Orders
      </h1>
      <Panel flush>
        {orders.value === null ? (
          <div class="p-5">
            {error.value ? (
              <Notice tone="alert">{error.value}</Notice>
            ) : (
              <Notice>Loading your orders…</Notice>
            )}
          </div>
        ) : orders.value.length === 0 ? (
          <div class="p-5 space-y-3">
            <Notice>You have not placed any orders yet.</Notice>
            <Link
              href="/"
              class="text-sm font-semibold underline"
              style={{ color: "var(--color-t-accent-ink, #9da3ff)" }}
            >
              Browse the catalog →
            </Link>
          </div>
        ) : (
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <caption class="sr-only">Your orders, newest first</caption>
              <thead style={{ color: "var(--color-t-text, #d0d6e0)" }}>
                <tr
                  class="border-b"
                  style={{
                    borderColor:
                      "var(--color-t-border, rgba(255, 255, 255, 0.08))",
                  }}
                >
                  <th scope="col" class={head}>
                    Order
                  </th>
                  <th scope="col" class={head}>
                    Date
                  </th>
                  <th scope="col" class={head}>
                    Status
                  </th>
                  <th scope="col" class={[head, "hidden md:table-cell"]}>
                    Payment
                  </th>
                  <th scope="col" class={[head, "hidden md:table-cell"]}>
                    Fulfillment
                  </th>
                  <th scope="col" class={[head, "text-right"]}>
                    Total
                  </th>
                </tr>
              </thead>
              <tbody>
                {orders.value.map((o) => (
                  <tr
                    key={o.id}
                    class="border-b last:border-b-0"
                    style={{
                      borderColor:
                        "var(--color-t-border, rgba(255, 255, 255, 0.08))",
                    }}
                  >
                    <th scope="row" class={[cell, "text-left font-semibold"]}>
                      <Link
                        href={`/account/orders/${encodeURIComponent(o.id)}/`}
                        class="underline"
                        style={{ color: "var(--color-t-accent-ink, #9da3ff)" }}
                      >
                        {o.order_number}
                      </Link>
                    </th>
                    <td
                      class={[cell, "whitespace-nowrap"]}
                      style={{ color: "var(--color-t-text, #d0d6e0)" }}
                    >
                      {formatDate(o.created_at)}
                    </td>
                    <td class={cell}>
                      <StatusBadge status={o.status} />
                    </td>
                    <td class={[cell, "hidden md:table-cell"]}>
                      <StatusBadge status={o.payment_status} />
                    </td>
                    <td class={[cell, "hidden md:table-cell"]}>
                      <StatusBadge status={o.fulfillment_status} />
                    </td>
                    <td
                      class={[
                        cell,
                        "text-right font-semibold whitespace-nowrap",
                      ]}
                      style={{ color: "var(--color-t-bright, #f7f8f8)" }}
                    >
                      {formatMoney(o.grand_total, currency)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
});

export const head: DocumentHead = {
  title: "Your orders | Fincas Store",
  meta: [{ name: "robots", content: "noindex" }],
};
