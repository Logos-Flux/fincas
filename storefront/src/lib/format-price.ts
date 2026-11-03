/**
 * Locale/currency price formatting (testing-plan §6 unit).
 * Thin wrapper over Intl.NumberFormat with a plain-text fallback so the
 * UI never throws on unknown currency codes or malformed amounts.
 */
export function formatPrice(
  amount: string | number,
  currency = "USD",
  locale?: string,
): string {
  const n = typeof amount === "string" ? parseFloat(amount) : amount;
  if (!Number.isFinite(n)) return `${amount} ${currency}`;
  try {
    return new Intl.NumberFormat(locale, {
      style: "currency",
      currency,
    }).format(n);
  } catch {
    return `${typeof amount === "number" ? amount.toFixed(2) : amount} ${currency}`;
  }
}
