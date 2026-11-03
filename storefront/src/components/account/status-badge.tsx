import { component$ } from "@builder.io/qwik";
import { humanizeStatus } from "../../lib/portal-client";

const SUCCESS = new Set([
  "completed",
  "shipped",
  "paid",
  "fulfilled",
  "delivered",
]);
const WARNING = new Set([
  "pending",
  "awaiting_payment",
  "awaiting_fulfillment",
  "awaiting_shipment",
  "partially_shipped",
  "partial",
  "unpaid",
  "unfulfilled",
  "on_hold",
  "manual_verification_required",
  "incomplete",
]);
const ERROR = new Set([
  "cancelled",
  "declined",
  "disputed",
  "refunded",
  "void",
  "returned",
]);

function tone(status: string): string {
  if (SUCCESS.has(status)) return "var(--color-t-success, #27a644)";
  if (WARNING.has(status)) return "var(--color-t-warning, #f59e0b)";
  if (ERROR.has(status)) return "var(--color-t-error, #ef4444)";
  return "var(--color-t-info, #828fff)";
}

/**
 * A status pill. The text stays in the skin's bright colour on the card so it
 * passes contrast in every theme; the coloured dot carries the tone and is
 * decorative (the words already say it).
 */
export const StatusBadge = component$<{ status: string; label?: string }>(
  ({ status, label }) => (
    <span
      class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap"
      style={{
        borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.12))",
        color: "var(--color-t-bright, #f7f8f8)",
        backgroundColor: "var(--color-t-surface, #0f1011)",
      }}
    >
      <span
        aria-hidden="true"
        class="h-1.5 w-1.5 rounded-full"
        style={{ backgroundColor: tone(status) }}
      />
      {label ? <span class="sr-only">{label}: </span> : null}
      {humanizeStatus(status)}
    </span>
  ),
);
