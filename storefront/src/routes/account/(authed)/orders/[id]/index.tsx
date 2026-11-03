import {
  component$,
  useContext,
  useSignal,
  useVisibleTask$,
} from "@builder.io/qwik";
import { Link, useLocation, type DocumentHead } from "@builder.io/qwik-city";
import { AccountContext } from "../../../../../context/account-context";
import { getAuthProvider } from "../../../../../lib/auth";
import {
  createPortalClient,
  formatDate,
  formatMoney,
  PortalError,
  type PortalOrderDetail,
  type PortalSubOrder,
} from "../../../../../lib/portal-client";
import { portalFailure } from "../../../../../components/account/auth-redirect";
import { Notice, Panel } from "../../../../../components/account/panel";
import { StatusBadge } from "../../../../../components/account/status-badge";

/** The milestones a shipment has reached or is expected to reach, in order. */
function shipmentSteps(
  s: PortalSubOrder,
): { label: string; date: string; done: boolean }[] {
  const steps: { label: string; date: string; done: boolean }[] = [];
  if (s.actual_ship_date)
    steps.push({ label: "Shipped", date: s.actual_ship_date, done: true });
  else if (s.estimated_ship_date)
    steps.push({
      label: "Estimated ship date",
      date: s.estimated_ship_date,
      done: false,
    });
  if (s.actual_delivery_date)
    steps.push({
      label: "Delivered",
      date: s.actual_delivery_date,
      done: true,
    });
  else if (s.estimated_delivery_date)
    steps.push({
      label: "Estimated delivery",
      date: s.estimated_delivery_date,
      done: false,
    });
  return steps;
}

function isHttpUrl(u: string): boolean {
  return /^https?:\/\//i.test(u);
}

export default component$(() => {
  const account = useContext(AccountContext);
  const loc = useLocation();
  const order = useSignal<PortalOrderDetail | null>(null);
  const error = useSignal("");
  const notFound = useSignal(false);

  // eslint-disable-next-line qwik/no-use-visible-task
  useVisibleTask$(async ({ track }) => {
    const id = track(() => loc.params.id);
    if (track(() => account.status) !== "ready") return;
    order.value = null;
    error.value = "";
    notFound.value = false;
    try {
      order.value = await createPortalClient(getAuthProvider()).order(id);
    } catch (err) {
      if (err instanceof PortalError && err.kind === "not_found") {
        notFound.value = true;
        return;
      }
      error.value = (await portalFailure(err, loc.url.pathname)) ?? "";
    }
  });

  const currency = account.profile?.default_currency || null;
  const o = order.value;
  const label = "text-xs font-semibold uppercase tracking-wider";
  const border = {
    borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
  };

  return (
    <div class="space-y-6">
      <p class="text-sm">
        <Link
          href="/account/orders/"
          class="font-semibold underline"
          style={{ color: "var(--color-t-accent-ink, #9da3ff)" }}
        >
          ← All orders
        </Link>
      </p>

      {!o ? (
        <>
          <h1
            class="text-2xl sm:text-3xl font-black tracking-tight"
            style={{ color: "var(--color-t-bright, #f7f8f8)" }}
          >
            {notFound.value ? "Order not found" : "Order"}
          </h1>
          <Panel>
            {notFound.value ? (
              <Notice tone="alert">
                We could not find that order in your account.
              </Notice>
            ) : error.value ? (
              <Notice tone="alert">{error.value}</Notice>
            ) : (
              <Notice>Loading order…</Notice>
            )}
          </Panel>
        </>
      ) : (
        <>
          <div class="flex flex-wrap items-center gap-3">
            <h1
              class="text-2xl sm:text-3xl font-black tracking-tight"
              style={{ color: "var(--color-t-bright, #f7f8f8)" }}
            >
              Order {o.order_number}
            </h1>
            <StatusBadge status={o.status} label="Order status" />
          </div>

          <Panel>
            <dl class="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <div>
                <dt
                  class={label}
                  style={{ color: "var(--color-t-text, #d0d6e0)" }}
                >
                  Placed
                </dt>
                <dd style={{ color: "var(--color-t-bright, #f7f8f8)" }}>
                  {formatDate(o.order_date || o.created_at)}
                </dd>
              </div>
              <div>
                <dt
                  class={label}
                  style={{ color: "var(--color-t-text, #d0d6e0)" }}
                >
                  Payment
                </dt>
                <dd>
                  <StatusBadge status={o.payment_status} />
                </dd>
              </div>
              <div>
                <dt
                  class={label}
                  style={{ color: "var(--color-t-text, #d0d6e0)" }}
                >
                  Fulfillment
                </dt>
                <dd>
                  <StatusBadge status={o.fulfillment_status} />
                </dd>
              </div>
              <div>
                <dt
                  class={label}
                  style={{ color: "var(--color-t-text, #d0d6e0)" }}
                >
                  Last updated
                </dt>
                <dd style={{ color: "var(--color-t-bright, #f7f8f8)" }}>
                  {formatDate(o.updated_at)}
                </dd>
              </div>
            </dl>
          </Panel>

          <Panel flush>
            <h2
              class="px-5 sm:px-6 pt-5 text-sm font-bold uppercase tracking-wider"
              style={{ color: "var(--color-t-bright, #f7f8f8)" }}
            >
              Items
            </h2>
            <div class="overflow-x-auto mt-3">
              <table class="w-full text-sm">
                <caption class="sr-only">
                  Items in order {o.order_number}
                </caption>
                <thead style={{ color: "var(--color-t-text, #d0d6e0)" }}>
                  <tr class="border-b" style={border}>
                    <th
                      scope="col"
                      class="px-5 sm:px-6 py-2 text-left text-xs font-semibold uppercase tracking-wider"
                    >
                      Item
                    </th>
                    <th
                      scope="col"
                      class="px-3 py-2 text-right text-xs font-semibold uppercase tracking-wider"
                    >
                      Qty
                    </th>
                    <th
                      scope="col"
                      class="px-3 py-2 text-right text-xs font-semibold uppercase tracking-wider hidden sm:table-cell"
                    >
                      Unit price
                    </th>
                    <th
                      scope="col"
                      class="px-5 sm:px-6 py-2 text-right text-xs font-semibold uppercase tracking-wider"
                    >
                      Total
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {o.line_items.map((line) => (
                    <tr key={line.id} class="border-b" style={border}>
                      <th
                        scope="row"
                        class="px-5 sm:px-6 py-3 text-left font-normal"
                      >
                        <span
                          class="block font-semibold"
                          style={{ color: "var(--color-t-bright, #f7f8f8)" }}
                        >
                          {line.item_name}
                        </span>
                        <span
                          class="block text-xs font-mono"
                          style={{ color: "var(--color-t-text, #d0d6e0)" }}
                        >
                          SKU {line.sku || "—"}
                        </span>
                      </th>
                      <td
                        class="px-3 py-3 text-right"
                        style={{ color: "var(--color-t-text, #d0d6e0)" }}
                      >
                        {line.quantity}
                      </td>
                      <td
                        class="px-3 py-3 text-right hidden sm:table-cell whitespace-nowrap"
                        style={{ color: "var(--color-t-text, #d0d6e0)" }}
                      >
                        {formatMoney(line.unit_price_inc_tax, currency)}
                      </td>
                      <td
                        class="px-5 sm:px-6 py-3 text-right font-semibold whitespace-nowrap"
                        style={{ color: "var(--color-t-bright, #f7f8f8)" }}
                      >
                        {formatMoney(line.line_total_inc_tax, currency)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <dl class="px-5 sm:px-6 py-4 space-y-1 text-sm ml-auto max-w-xs">
              {[
                ["Subtotal (excl. tax)", o.subtotal_ex_tax],
                ["Tax", o.tax_total],
                ["Shipping", o.shipping_cost_inc_tax],
              ].map(([k, v]) => (
                <div
                  key={k}
                  class="flex justify-between gap-4"
                  style={{ color: "var(--color-t-text, #d0d6e0)" }}
                >
                  <dt>{k}</dt>
                  <dd class="whitespace-nowrap">{formatMoney(v, currency)}</dd>
                </div>
              ))}
              <div
                class="flex justify-between gap-4 border-t pt-2 font-bold"
                style={{ ...border, color: "var(--color-t-bright, #f7f8f8)" }}
              >
                <dt>Order total</dt>
                <dd class="whitespace-nowrap">
                  {formatMoney(o.grand_total, currency)}
                </dd>
              </div>
            </dl>
          </Panel>

          <Panel>
            <h2
              class="text-sm font-bold uppercase tracking-wider mb-4"
              style={{ color: "var(--color-t-bright, #f7f8f8)" }}
            >
              Shipments
            </h2>
            {o.sub_orders.length === 0 ? (
              <Notice>
                No shipments have been scheduled for this order yet.
              </Notice>
            ) : (
              <ul class="space-y-4">
                {o.sub_orders.map((s) => {
                  const steps = shipmentSteps(s);
                  return (
                    <li
                      key={s.id}
                      class="rounded-xl border p-4 space-y-3"
                      style={{
                        ...border,
                        backgroundColor: "var(--color-t-surface, #0f1011)",
                      }}
                    >
                      <div class="flex flex-wrap items-center justify-between gap-2">
                        <h3
                          class="text-sm font-semibold"
                          style={{ color: "var(--color-t-bright, #f7f8f8)" }}
                        >
                          Shipment {s.sub_order_number}
                        </h3>
                        <StatusBadge
                          status={s.status}
                          label="Shipment status"
                        />
                      </div>
                      {steps.length > 0 ? (
                        <ol class="space-y-1 text-sm">
                          {steps.map((step) => (
                            <li
                              key={step.label}
                              class="flex items-center gap-2"
                              style={{ color: "var(--color-t-text, #d0d6e0)" }}
                            >
                              <span
                                aria-hidden="true"
                                class="h-2 w-2 rounded-full border"
                                style={{
                                  borderColor: "var(--color-t-accent, #5e6ad2)",
                                  backgroundColor: step.done
                                    ? "var(--color-t-accent, #5e6ad2)"
                                    : "transparent",
                                }}
                              />
                              <span>{step.label}:</span>
                              <span
                                style={{
                                  color: "var(--color-t-bright, #f7f8f8)",
                                }}
                              >
                                {formatDate(step.date)}
                              </span>
                            </li>
                          ))}
                        </ol>
                      ) : null}
                      {s.tracking_number ? (
                        <p
                          class="text-sm"
                          style={{ color: "var(--color-t-text, #d0d6e0)" }}
                        >
                          Tracking:{" "}
                          {s.tracking_url && isHttpUrl(s.tracking_url) ? (
                            <a
                              href={s.tracking_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              class="font-semibold underline break-all"
                              style={{
                                color: "var(--color-t-accent-ink, #9da3ff)",
                              }}
                            >
                              {s.tracking_number}
                              <span class="sr-only"> (opens in a new tab)</span>
                            </a>
                          ) : (
                            <span
                              class="font-mono"
                              style={{
                                color: "var(--color-t-bright, #f7f8f8)",
                              }}
                            >
                              {s.tracking_number}
                            </span>
                          )}
                        </p>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )}
          </Panel>

          {o.customer_message ? (
            <Panel>
              <h2
                class="text-sm font-bold uppercase tracking-wider mb-2"
                style={{ color: "var(--color-t-bright, #f7f8f8)" }}
              >
                Your note
              </h2>
              <p
                class="text-sm whitespace-pre-line"
                style={{ color: "var(--color-t-text, #d0d6e0)" }}
              >
                {o.customer_message}
              </p>
            </Panel>
          ) : null}
        </>
      )}
    </div>
  );
});

export const head: DocumentHead = {
  title: "Order details | Fincas Store",
  meta: [{ name: "robots", content: "noindex" }],
};
