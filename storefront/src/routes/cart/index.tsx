import {
  component$,
  useContext,
  useSignal,
  useVisibleTask$,
  $,
} from "@builder.io/qwik";
import { Link, type DocumentHead } from "@builder.io/qwik-city";
import { CartContext } from "../../context/cart-context";
import {
  OrderSubmitError,
  submitOrder,
  type OrderConfirmation,
  getBackendOrigin,
} from "../../lib/api-client";
import {
  DEMO_COUPONS,
  cartSubtotalCents,
  centsToAmount,
  discountCentsForCoupon,
  toCents,
  validateCoupon,
  type CouponDef,
} from "../../lib/cart-math";

export default component$(() => {
  const cart = useContext(CartContext);
  const companyName = useSignal<string>("Industrial Dynamics Corp");
  const contactName = useSignal<string>("Jane Doe");
  const email = useSignal<string>("procurement@industrial-dynamics.com");
  const poNumber = useSignal<string>("PO-2026-0881");
  const shippingAddress = useSignal<string>(
    "442 Industrial Parkway, Dock 4, Austin, TX 78701",
  );
  const paymentMethod = useSignal<
    "net_30" | "credit_card" | "crypto_usdc" | "bank_wire"
  >("net_30");
  const notes = useSignal<string>(
    "Please pack with certified freight pallet and deliver with liftgate.",
  );

  const isSubmitting = useSignal<boolean>(false);
  const confirmation = useSignal<OrderConfirmation | null>(null);
  const submitError = useSignal<string | null>(null);

  const couponInput = useSignal<string>("");
  const appliedCoupon = useSignal<CouponDef | null>(null);
  const couponError = useSignal<string | null>(null);

  // The drawer checkout link no longer closes the drawer itself: unmounting
  // the Link inside its own click handler aborts Qwik City client-side
  // navigation. Close it here once checkout renders instead.
  // eslint-disable-next-line qwik/no-use-visible-task
  useVisibleTask$(() => {
    cart.isOpen = false;
  });

  const subtotalCents = cartSubtotalCents(cart.items);
  const subtotal = centsToAmount(subtotalCents);
  const discountCents = appliedCoupon.value
    ? discountCentsForCoupon(subtotalCents, appliedCoupon.value)
    : 0;
  const total = centsToAmount(subtotalCents - discountCents);

  const applyCoupon = $(() => {
    const res = validateCoupon(couponInput.value, subtotalCents, DEMO_COUPONS);
    if (res.ok) {
      appliedCoupon.value = res.coupon;
      couponError.value = null;
    } else {
      // Invalid/expired/gated codes leave the total untouched.
      couponError.value = res.error;
    }
  });

  const removeCoupon = $(() => {
    appliedCoupon.value = null;
    couponInput.value = "";
    couponError.value = null;
  });

  const handleSubmit = $(async () => {
    if (cart.items.length === 0) return;
    isSubmitting.value = true;
    submitError.value = null;

    try {
      const res = await submitOrder({
        company_name: companyName.value,
        customer_name: contactName.value,
        email: email.value,
        shipping_address: shippingAddress.value,
        payment_method: paymentMethod.value,
        po_number: poNumber.value,
        notes: notes.value,
        coupon_code: appliedCoupon.value?.code,
        items: cart.items.map((i) => ({
          product_id: i.product.id,
          variant_id: i.variantId,
          quantity: i.quantity,
          unit_price: i.selectedPrice,
        })),
      });

      confirmation.value = res;
      cart.items = [];
    } catch (err) {
      // F-004: failures are shown, never swallowed into a fake confirmation.
      submitError.value =
        err instanceof OrderSubmitError
          ? err.message
          : "Unexpected checkout failure. No order was placed — try again.";
    } finally {
      isSubmitting.value = false;
    }
  });

  if (confirmation.value) {
    return (
      <div class="max-w-3xl mx-auto px-4 py-16 text-center space-y-6">
        <div
          class="w-16 h-16 rounded-2xl mx-auto flex items-center justify-center text-2xl font-black text-white shadow-xl"
          style={{ backgroundColor: "var(--color-t-accent, #5e6ad2)" }}
        >
          ✓
        </div>
        <div class="space-y-2">
          <h1
            class="text-3xl font-black tracking-tight"
            style={{ color: "var(--color-t-bright, #ffffff)" }}
          >
            Order Successfully Placed
          </h1>
          <p class="text-sm" style={{ color: "var(--color-t-muted, #8a8f98)" }}>
            Your sales order was confirmed by Fincas.
          </p>
        </div>

        <div
          class="rounded-2xl border p-6 text-left max-w-md mx-auto space-y-4 font-mono text-xs"
          style={{
            backgroundColor: "var(--color-t-card, #191a1b)",
            borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
          }}
        >
          <div class="flex justify-between border-b border-white/10 pb-2">
            <span style={{ color: "var(--color-t-muted, #8a8f98)" }}>
              Sales Order:
            </span>
            <span
              class="font-bold"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              {confirmation.value.order_number}
            </span>
          </div>
          <div class="flex justify-between border-b border-white/10 pb-2">
            <span style={{ color: "var(--color-t-muted, #8a8f98)" }}>
              Total Amount:
            </span>
            <span class="font-bold text-emerald-300">
              ${confirmation.value.total_amount} {confirmation.value.currency}
            </span>
          </div>
          <div class="flex justify-between border-b border-white/10 pb-2">
            <span style={{ color: "var(--color-t-muted, #8a8f98)" }}>
              Order Status:
            </span>
            <span
              class="font-bold"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              {confirmation.value.status}
            </span>
          </div>
          <div class="flex justify-between">
            <span style={{ color: "var(--color-t-muted, #8a8f98)" }}>
              Terms / Settlement:
            </span>
            <span class="font-bold text-indigo-300">
              {confirmation.value.payment_status}
            </span>
          </div>
        </div>

        <div class="pt-4 flex items-center justify-center gap-4">
          <Link
            href="/"
            class="px-5 py-2.5 rounded-xl font-bold text-xs uppercase tracking-wider text-white shadow-lg"
            style={{ backgroundColor: "var(--color-t-accent, #5e6ad2)" }}
          >
            ← Return to Catalog
          </Link>
          <a
            href={`${getBackendOrigin()}/erp/orders/`}
            target="_blank"
            rel="noreferrer"
            class="px-4 py-2.5 rounded-xl font-semibold text-xs border opacity-80 hover:opacity-100"
            style={{
              borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
            }}
          >
            View in ERP Back-Office ↗
          </a>
        </div>
      </div>
    );
  }

  return (
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-8">
      <h1
        class="text-3xl font-black tracking-tight"
        style={{ color: "var(--color-t-bright, #ffffff)" }}
      >
        Checkout & Order Submission
      </h1>

      {cart.items.length === 0 ? (
        <div
          class="rounded-3xl border p-16 text-center space-y-4"
          style={{
            backgroundColor: "var(--color-t-card, #191a1b)",
            borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
          }}
        >
          <p
            class="text-base font-semibold"
            style={{ color: "var(--color-t-bright, #ffffff)" }}
          >
            Your cart is empty.
          </p>
          <Link
            href="/"
            class="inline-block px-5 py-2.5 rounded-xl font-bold text-xs uppercase tracking-wider text-white shadow-md"
            style={{ backgroundColor: "var(--color-t-accent, #5e6ad2)" }}
          >
            Browse Products →
          </Link>
        </div>
      ) : (
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">
          {/* Checkout Form */}
          <div
            class="lg:col-span-2 rounded-3xl border p-6 sm:p-8 space-y-6"
            style={{
              backgroundColor: "var(--color-t-card, #191a1b)",
              borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
            }}
          >
            <h2
              class="font-bold uppercase tracking-wider text-xs"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              Commercial Account Details
            </h2>

            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs">
              <div class="space-y-1">
                <label
                  for="co-company"
                  class="font-medium"
                  style={{ color: "var(--color-t-muted, #8a8f98)" }}
                >
                  Company Entity Name
                </label>
                <input
                  id="co-company"
                  type="text"
                  value={companyName.value}
                  onInput$={(e) =>
                    (companyName.value = (e.target as HTMLInputElement).value)
                  }
                  class="w-full p-2.5 rounded-xl border bg-black/20 outline-none"
                  style={{
                    borderColor:
                      "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                    color: "var(--color-t-bright, #fff)",
                  }}
                />
              </div>

              <div class="space-y-1">
                <label
                  for="co-po"
                  class="font-medium"
                  style={{ color: "var(--color-t-muted, #8a8f98)" }}
                >
                  Customer PO Number (Optional)
                </label>
                <input
                  id="co-po"
                  type="text"
                  value={poNumber.value}
                  onInput$={(e) =>
                    (poNumber.value = (e.target as HTMLInputElement).value)
                  }
                  class="w-full p-2.5 rounded-xl border bg-black/20 outline-none font-mono"
                  style={{
                    borderColor:
                      "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                    color: "var(--color-t-bright, #fff)",
                  }}
                />
              </div>

              <div class="space-y-1">
                <label
                  for="co-contact"
                  class="font-medium"
                  style={{ color: "var(--color-t-muted, #8a8f98)" }}
                >
                  Contact Person
                </label>
                <input
                  id="co-contact"
                  type="text"
                  value={contactName.value}
                  onInput$={(e) =>
                    (contactName.value = (e.target as HTMLInputElement).value)
                  }
                  class="w-full p-2.5 rounded-xl border bg-black/20 outline-none"
                  style={{
                    borderColor:
                      "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                    color: "var(--color-t-bright, #fff)",
                  }}
                />
              </div>

              <div class="space-y-1">
                <label
                  for="co-email"
                  class="font-medium"
                  style={{ color: "var(--color-t-muted, #8a8f98)" }}
                >
                  Procurement Email
                </label>
                <input
                  id="co-email"
                  type="email"
                  value={email.value}
                  onInput$={(e) =>
                    (email.value = (e.target as HTMLInputElement).value)
                  }
                  class="w-full p-2.5 rounded-xl border bg-black/20 outline-none font-mono"
                  style={{
                    borderColor:
                      "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                    color: "var(--color-t-bright, #fff)",
                  }}
                />
              </div>

              <div class="sm:col-span-2 space-y-1">
                <label
                  for="co-ship"
                  class="font-medium"
                  style={{ color: "var(--color-t-muted, #8a8f98)" }}
                >
                  Shipping & Receiving Dock Address
                </label>
                <input
                  id="co-ship"
                  type="text"
                  value={shippingAddress.value}
                  onInput$={(e) =>
                    (shippingAddress.value = (
                      e.target as HTMLInputElement
                    ).value)
                  }
                  class="w-full p-2.5 rounded-xl border bg-black/20 outline-none"
                  style={{
                    borderColor:
                      "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                    color: "var(--color-t-bright, #fff)",
                  }}
                />
              </div>

              <div class="sm:col-span-2 space-y-1">
                <label
                  for="co-notes"
                  class="font-medium"
                  style={{ color: "var(--color-t-muted, #8a8f98)" }}
                >
                  Delivery & Pallet Instructions
                </label>
                <textarea
                  id="co-notes"
                  rows={2}
                  value={notes.value}
                  onInput$={(e) =>
                    (notes.value = (e.target as HTMLTextAreaElement).value)
                  }
                  class="w-full p-2.5 rounded-xl border bg-black/20 outline-none"
                  style={{
                    borderColor:
                      "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                    color: "var(--color-t-bright, #fff)",
                  }}
                />
              </div>
            </div>

            {/* Payment Method Selector */}
            <div class="space-y-3 pt-2">
              <span
                class="text-xs font-bold uppercase tracking-wider"
                style={{ color: "var(--color-t-muted, #8a8f98)" }}
              >
                Payment Terms & Settlement
              </span>
              <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
                <label
                  class={`p-3 rounded-xl border flex items-center gap-3 cursor-pointer transition-all ${
                    paymentMethod.value === "net_30"
                      ? "border-indigo-500 bg-white/5"
                      : ""
                  }`}
                  style={{
                    borderColor:
                      paymentMethod.value === "net_30"
                        ? "var(--color-t-accent, #5e6ad2)"
                        : "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                  }}
                >
                  <input
                    type="radio"
                    name="payment"
                    checked={paymentMethod.value === "net_30"}
                    onChange$={() => (paymentMethod.value = "net_30")}
                  />
                  <div>
                    <div
                      class="font-bold"
                      style={{ color: "var(--color-t-bright, #ffffff)" }}
                    >
                      NET 30 Invoicing
                    </div>
                    <div
                      class="text-[11px]"
                      style={{ color: "var(--color-t-muted, #8a8f98)" }}
                    >
                      B2B Credit Terms (Invoice on dispatch)
                    </div>
                  </div>
                </label>

                <label
                  class={`p-3 rounded-xl border flex items-center gap-3 cursor-pointer transition-all ${
                    paymentMethod.value === "credit_card"
                      ? "border-indigo-500 bg-white/5"
                      : ""
                  }`}
                  style={{
                    borderColor:
                      paymentMethod.value === "credit_card"
                        ? "var(--color-t-accent, #5e6ad2)"
                        : "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                  }}
                >
                  <input
                    type="radio"
                    name="payment"
                    checked={paymentMethod.value === "credit_card"}
                    onChange$={() => (paymentMethod.value = "credit_card")}
                  />
                  <div>
                    <div
                      class="font-bold"
                      style={{ color: "var(--color-t-bright, #ffffff)" }}
                    >
                      Credit Card (Stripe)
                    </div>
                    <div
                      class="text-[11px]"
                      style={{ color: "var(--color-t-muted, #8a8f98)" }}
                    >
                      Instant settlement via Stripe Gateway
                    </div>
                  </div>
                </label>

                <label
                  class={`p-3 rounded-xl border flex items-center gap-3 cursor-pointer transition-all ${
                    paymentMethod.value === "crypto_usdc"
                      ? "border-indigo-500 bg-white/5"
                      : ""
                  }`}
                  style={{
                    borderColor:
                      paymentMethod.value === "crypto_usdc"
                        ? "var(--color-t-accent, #5e6ad2)"
                        : "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                  }}
                >
                  <input
                    type="radio"
                    name="payment"
                    checked={paymentMethod.value === "crypto_usdc"}
                    onChange$={() => (paymentMethod.value = "crypto_usdc")}
                  />
                  <div>
                    <div
                      class="font-bold"
                      style={{ color: "var(--color-t-bright, #ffffff)" }}
                    >
                      USDC Settlement
                    </div>
                    <div
                      class="text-[11px]"
                      style={{ color: "var(--color-t-muted, #8a8f98)" }}
                    >
                      Fincas Plugin Gateway
                    </div>
                  </div>
                </label>

                <label
                  class={`p-3 rounded-xl border flex items-center gap-3 cursor-pointer transition-all ${
                    paymentMethod.value === "bank_wire"
                      ? "border-indigo-500 bg-white/5"
                      : ""
                  }`}
                  style={{
                    borderColor:
                      paymentMethod.value === "bank_wire"
                        ? "var(--color-t-accent, #5e6ad2)"
                        : "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                  }}
                >
                  <input
                    type="radio"
                    name="payment"
                    checked={paymentMethod.value === "bank_wire"}
                    onChange$={() => (paymentMethod.value = "bank_wire")}
                  />
                  <div>
                    <div
                      class="font-bold"
                      style={{ color: "var(--color-t-bright, #ffffff)" }}
                    >
                      Bank Wire / ACH
                    </div>
                    <div
                      class="text-[11px]"
                      style={{ color: "var(--color-t-muted, #8a8f98)" }}
                    >
                      Proforma invoice sent immediately
                    </div>
                  </div>
                </label>
              </div>
            </div>
          </div>

          {/* Order Summary Column */}
          <div
            class="rounded-3xl border p-6 sm:p-8 space-y-6 h-fit"
            style={{
              backgroundColor: "var(--color-t-surface, #0f1011)",
              borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
            }}
          >
            <h2
              class="text-xs font-bold uppercase tracking-wider"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              Order Summary
            </h2>

            <div class="divide-y divide-white/10 max-h-60 overflow-y-auto pr-2 space-y-2">
              {cart.items.map((item, idx) => (
                <div key={idx} class="pt-2 flex justify-between gap-3 text-xs">
                  <div class="truncate">
                    <div
                      class="font-semibold truncate"
                      style={{ color: "var(--color-t-bright, #ffffff)" }}
                    >
                      {item.product.title}
                    </div>
                    <div
                      class="font-mono text-[11px]"
                      style={{ color: "var(--color-t-muted, #8a8f98)" }}
                    >
                      Qty: {item.quantity} × ${item.selectedPrice}
                    </div>
                  </div>
                  <div
                    class="font-mono font-bold shrink-0"
                    style={{ color: "var(--color-t-bright, #ffffff)" }}
                  >
                    $
                    {(
                      (toCents(item.selectedPrice) * item.quantity) /
                      100
                    ).toFixed(2)}
                  </div>
                </div>
              ))}
            </div>

            {/* Coupon */}
            <div class="space-y-2">
              {appliedCoupon.value ? (
                <div class="flex items-center justify-between gap-2 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs">
                  <span class="font-mono font-bold text-emerald-300">
                    {appliedCoupon.value.code}
                  </span>
                  <span style={{ color: "var(--color-t-muted, #8a8f98)" }}>
                    {appliedCoupon.value.description}
                  </span>
                  <button
                    type="button"
                    onClick$={removeCoupon}
                    aria-label={`Remove coupon ${appliedCoupon.value.code}`}
                    class="font-semibold text-red-300 hover:text-red-200"
                  >
                    Remove
                  </button>
                </div>
              ) : (
                <div class="flex gap-2">
                  <label for="co-coupon" class="sr-only">
                    Coupon code
                  </label>
                  <input
                    id="co-coupon"
                    type="text"
                    placeholder="Coupon code (try SAVE10)"
                    value={couponInput.value}
                    onInput$={(e) =>
                      (couponInput.value = (e.target as HTMLInputElement).value)
                    }
                    class="flex-1 min-w-0 p-2.5 rounded-xl border bg-black/20 outline-none font-mono text-xs placeholder:text-gray-500"
                    style={{
                      borderColor:
                        "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                      color: "var(--color-t-bright, #fff)",
                    }}
                  />
                  <button
                    type="button"
                    onClick$={applyCoupon}
                    class="px-4 py-2.5 rounded-xl font-bold text-xs uppercase tracking-wider border transition-all hover:bg-white/5"
                    style={{
                      borderColor:
                        "var(--color-t-border, rgba(255, 255, 255, 0.15))",
                      color: "var(--color-t-bright, #ffffff)",
                    }}
                  >
                    Apply
                  </button>
                </div>
              )}
              {couponError.value && (
                <p role="alert" class="text-xs font-medium text-red-300">
                  {couponError.value}
                </p>
              )}
            </div>

            <div class="border-t border-white/10 pt-4 space-y-2 text-xs">
              <div
                class="flex justify-between"
                style={{ color: "var(--color-t-muted, #8a8f98)" }}
              >
                <span>Subtotal</span>
                <span class="font-mono font-semibold">${subtotal} USD</span>
              </div>
              {discountCents > 0 && (
                <div class="flex justify-between font-semibold text-emerald-300">
                  <span>Coupon {appliedCoupon.value?.code}</span>
                  <span class="font-mono">
                    −${centsToAmount(discountCents)} USD
                  </span>
                </div>
              )}
              <div
                class="flex justify-between"
                style={{ color: "var(--color-t-muted, #8a8f98)" }}
              >
                <span>Freight Shipping</span>
                <span class="font-semibold font-mono text-emerald-300">
                  Calculated on dispatch
                </span>
              </div>
              <div
                class="flex justify-between"
                style={{ color: "var(--color-t-muted, #8a8f98)" }}
              >
                <span>Estimated Tax</span>
                <span class="font-mono font-semibold">$0.00 (Exempt)</span>
              </div>
              <div
                class="border-t border-white/10 pt-2 flex justify-between text-base font-black"
                style={{ color: "var(--color-t-bright, #ffffff)" }}
              >
                <span>Total Amount:</span>
                <span
                  class="font-mono"
                  style={{
                    color:
                      "var(--color-t-accent-ink, var(--color-t-accent, #5e6ad2))",
                  }}
                >
                  ${total} USD
                </span>
              </div>
            </div>

            {submitError.value && (
              <div
                role="alert"
                class="rounded-xl border border-red-500/40 bg-red-500/10 p-3 text-xs space-y-1"
              >
                <p class="font-bold text-red-200">Order could not be placed.</p>
                <p class="text-red-200/90">{submitError.value}</p>
                <p style={{ color: "var(--color-t-muted, #8a8f98)" }}>
                  Your cart is unchanged — fix the issue and submit again.
                </p>
              </div>
            )}

            <button
              type="button"
              onClick$={handleSubmit}
              disabled={isSubmitting.value}
              class="w-full py-3.5 rounded-xl font-bold text-xs uppercase tracking-wider text-white shadow-xl transition-all active:scale-95 cursor-pointer disabled:opacity-50"
              style={{
                backgroundColor: "var(--color-t-accent, #5e6ad2)",
                boxShadow:
                  "0 4px 14px var(--color-t-accent-glow, rgba(94, 106, 210, 0.4))",
              }}
            >
              {isSubmitting.value
                ? "Transmitting to ERP..."
                : "Submit B2B Order →"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
});

export const head: DocumentHead = {
  title: "Checkout & Order Review — Fincas Store",
  meta: [
    {
      name: "description",
      content: "Review and submit your enterprise order.",
    },
  ],
};
