/**
 * Customer portal API client (`/api/v1/portal/customer/…`, served by the
 * ERP's `portal` app). Every request carries the signed-in customer's
 * bearer token from the auth adapter.
 *
 * Like the catalog client, it never fabricates: there is no demo fallback,
 * and every failure surfaces as a PortalError so the page can show an error
 * state (or send the customer back to sign in) instead of invented orders.
 */
import { getApiBaseUrl } from "./api-client";
import type { AuthProvider } from "./auth/types";

export interface PortalProfile {
  id: string;
  first_name: string;
  last_name: string;
  full_name: string;
  email: string;
  phone: string;
  company_name: string;
  customer_type: string;
  is_tax_exempt: boolean;
  accepts_marketing: boolean;
  default_currency: string;
  created_at: string;
  updated_at: string;
}

export interface PortalModuleCounts {
  orders: number;
  quotes: number;
  invoices: number;
  shared_docs: number;
  product_docs: number;
  proposals: number;
}

export interface PortalOrderSummary {
  id: string;
  order_number: string;
  status: string;
  payment_status: string;
  fulfillment_status: string;
  grand_total: string;
  created_at: string;
  item_count: number;
}

export interface PortalOrderLine {
  id: string;
  variant: string | number | null;
  sku: string;
  item_name: string;
  quantity: number;
  unit_price_ex_tax: string;
  unit_price_inc_tax: string;
  line_total_ex_tax: string;
  line_total_inc_tax: string;
  fulfillment_status: string;
}

export interface PortalSubOrder {
  id: string;
  sub_order_number: string;
  status: string;
  tracking_number: string;
  tracking_url: string;
  estimated_ship_date: string | null;
  actual_ship_date: string | null;
  estimated_delivery_date: string | null;
  actual_delivery_date: string | null;
}

export interface PortalOrderDetail {
  id: string;
  order_number: string;
  status: string;
  payment_status: string;
  fulfillment_status: string;
  order_date: string | null;
  grand_total: string;
  subtotal_ex_tax: string;
  subtotal_inc_tax: string;
  tax_total: string;
  shipping_cost_inc_tax: string;
  customer_message: string;
  created_at: string;
  updated_at: string;
  line_items: PortalOrderLine[];
  sub_orders: PortalSubOrder[];
}

export type PortalErrorKind = "unauthorized" | "not_found" | "unavailable";

export class PortalError extends Error {
  kind: PortalErrorKind;
  status: number;
  constructor(kind: PortalErrorKind, message: string, status: number) {
    super(message);
    this.name = "PortalError";
    this.kind = kind;
    this.status = status;
  }
}

export const PORTAL_PATH = "/v1/portal/customer";

export function portalUrl(path: string): string {
  return `${getApiBaseUrl()}${PORTAL_PATH}/${path.replace(/^\/+/, "")}`;
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface PortalClient {
  profile(): Promise<PortalProfile>;
  moduleCounts(): Promise<PortalModuleCounts>;
  orders(): Promise<PortalOrderSummary[]>;
  order(id: string): Promise<PortalOrderDetail>;
}

export function createPortalClient(
  auth: Pick<AuthProvider, "getAccessToken">,
  fetchImpl: FetchLike = (input, init) => fetch(input, init),
): PortalClient {
  async function get<T>(path: string): Promise<T> {
    const token = await auth.getAccessToken();
    if (!token) throw new PortalError("unauthorized", "Sign in to view your account.", 401);

    let res: Response;
    try {
      res = await fetchImpl(portalUrl(path), {
        headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(8000),
      });
    } catch {
      throw new PortalError(
        "unavailable",
        "We could not reach your account right now. Check your connection and try again.",
        0,
      );
    }
    // DRF answers 403 (not 401) for a missing or rejected portal token,
    // because the portal authenticator sends no WWW-Authenticate header.
    if (res.status === 401 || res.status === 403) {
      throw new PortalError("unauthorized", "Your session has ended. Sign in again.", res.status);
    }
    if (res.status === 404) {
      throw new PortalError("not_found", "We could not find that in your account.", 404);
    }
    if (!res.ok) {
      throw new PortalError(
        "unavailable",
        `Your account is unavailable right now (HTTP ${res.status}). Please try again shortly.`,
        res.status,
      );
    }
    try {
      return (await res.json()) as T;
    } catch {
      throw new PortalError("unavailable", "The account service sent an unreadable response.", res.status);
    }
  }

  return {
    profile: () => get<PortalProfile>("profile/"),
    moduleCounts: () => get<PortalModuleCounts>("module-counts/"),
    async orders() {
      // Unpaginated today (a plain array); accept DRF's paginated shape too.
      const data = await get<PortalOrderSummary[] | { results?: PortalOrderSummary[] }>("orders/");
      if (Array.isArray(data)) return data;
      if (data && Array.isArray(data.results)) return data.results;
      throw new PortalError("unavailable", "The account service sent an unexpected order list.", 200);
    },
    order: (id: string) => get<PortalOrderDetail>(`orders/${encodeURIComponent(id)}/`),
  };
}

/** "awaiting_payment" → "Awaiting payment". */
export function humanizeStatus(value: string | null | undefined): string {
  if (!value) return "—";
  // Stored values keep their historical spelling ("cancelled"); the copy is US English.
  const text = value.replace(/_/g, " ").trim().replace(/\bcancelled\b/g, "canceled");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** A date-only rendering of an ISO timestamp; the raw value when unparseable. */
export function formatDate(value: string | null | undefined, locale?: string): string {
  if (!value) return "—";
  // A bare date ("2026-10-01") parses as UTC midnight; format it in UTC so a
  // customer west of Greenwich does not see the previous day.
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(value);
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "short",
    day: "numeric",
    ...(dateOnly ? { timeZone: "UTC" } : {}),
  }).format(d);
}

/**
 * Money from the portal. Orders carry no currency code, so the caller passes
 * the customer's default currency when it has one; without it the amount is
 * shown as a plain number rather than under a guessed currency symbol.
 */
export function formatMoney(amount: string | number | null | undefined, currency: string | null, locale?: string): string {
  if (amount === null || amount === undefined || amount === "") return "—";
  const n = typeof amount === "string" ? Number(amount) : amount;
  if (!Number.isFinite(n)) return String(amount);
  if (currency) {
    try {
      return new Intl.NumberFormat(locale, { style: "currency", currency }).format(n);
    } catch {
      /* unknown code — fall through to the plain number */
    }
  }
  return new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);
}
