export interface ProductVariant {
  id: number | string;
  sku: string;
  name: string;
  price: string;
  inventory_level: number;
  attributes: Record<string, string>;
}

export interface ProductTierPrice {
  min_quantity: number;
  unit_price: string;
  discount_percentage?: string;
}

export interface Product {
  id: number | string;
  title: string;
  slug: string;
  sku: string;
  description: string;
  category_name: string;
  /** Slug of the product's category page; null when it has none to link to. */
  category_slug: string | null;
  brand_name: string;
  /** Slug of the brand page; null when the brand has no public page. */
  brand_slug: string | null;
  /** Full ERP description (detail reads); the head helper summarizes it. */
  long_description: string;
  seo_title: string | null;
  seo_meta_description: string | null;
  seo_og_title: string | null;
  seo_og_description: string | null;
  /** Lowest sellable price; null when the ERP has no price ("Price on request"). */
  base_price: string | null;
  currency: string;
  stock_status: "in_stock" | "low_stock" | "out_of_stock" | "preorder" | "backorder";
  stock_quantity: number;
  /** CDN image; null renders a neutral placeholder, never another product's photo. */
  image_url: string | null;
  specs: Record<string, string>;
  variants: ProductVariant[];
  tiered_pricing: ProductTierPrice[];
}

export interface CartItem {
  product: Product;
  variantId?: number | string;
  quantity: number;
  selectedPrice: string;
}

/**
 * One cart line as the ERP takes it (OP #3426): a variant id, or a product id
 * when the cart line has no variant (the server resolves a product with a
 * single sellable variant and answers `variant_required` otherwise). Never a
 * price: the server prices every line.
 */
export interface CheckoutLine {
  variant?: number | string;
  product?: number | string;
  quantity: number;
}

export interface CheckoutPayload {
  customer_name: string;
  company_name: string;
  email: string;
  shipping_address: string;
  payment_method: "net_30" | "credit_card" | "crypto_usdc" | "bank_wire";
  po_number?: string;
  notes?: string;
  /** Recorded on the order for staff; the ERP does not apply it at checkout. */
  coupon_code?: string;
  lines: CheckoutLine[];
}

/** A line as the ERP priced it. */
export interface ConfirmedLine {
  variant: string | null;
  sku: string;
  item_name: string;
  quantity: number;
  unit_price: string;
  line_total: string;
}

export interface OrderConfirmation {
  order_number: string;
  total_amount: string;
  currency: string;
  status: string;
  payment_status: string;
  created_at: string;
  lines: ConfirmedLine[];
  subtotal: string;
  tax_total: string;
  shipping_total: string;
  discount_total: string;
}

const DEMO_SEEDS: DemoSeed[] = [
  {
    id: 1,
    title: "Quantum Edge Gateway X9",
    slug: "quantum-edge-gateway-x9",
    sku: "QEG-X900-PRO",
    category_name: "Industrial IoT",
    brand_name: "AeroCore Systems",
    base_price: "849.00",
    currency: "USD",
    stock_status: "in_stock",
    stock_quantity: 42,
    image_url: "https://images.unsplash.com/photo-1544716278-ca5e3f4abd8c?w=800&auto=format&fit=crop&q=80",
    description: "Enterprise ruggedized computing gateway with dual Gigabit TSN ethernet, CAN-bus telemetry, and AI inference acceleration.",
    specs: {
      Processor: "Octa-core 2.4GHz Industrial SoC",
      Memory: "32GB ECC LPDDR5",
      Storage: "512GB NVMe Industrial SSD",
      Power: "9-36V DC Redundant Input",
      Rating: "IP67 Ingress Protection",
      OperatingTemp: "-40°C to +85°C",
    },
    variants: [
      { id: 101, sku: "QEG-X900-32G", name: "Standard (32GB ECC)", price: "849.00", inventory_level: 28, attributes: { RAM: "32GB" } },
      { id: 102, sku: "QEG-X900-64G", name: "Heavy Telemetry (64GB ECC)", price: "1099.00", inventory_level: 14, attributes: { RAM: "64GB" } },
    ],
    tiered_pricing: [
      { min_quantity: 1, unit_price: "849.00" },
      { min_quantity: 5, unit_price: "799.00", discount_percentage: "6%" },
      { min_quantity: 25, unit_price: "729.00", discount_percentage: "14%" },
      { min_quantity: 100, unit_price: "649.00", discount_percentage: "24%" },
    ],
  },
  {
    id: 2,
    title: "Apex Precision Optic Sensor Hub",
    slug: "apex-precision-optic-hub",
    sku: "APX-OPT-440",
    category_name: "Sensors & Optics",
    brand_name: "Apex Instruments",
    base_price: "320.00",
    currency: "USD",
    stock_status: "in_stock",
    stock_quantity: 85,
    image_url: "https://images.unsplash.com/photo-1517077304055-6e89abbf09b0?w=800&auto=format&fit=crop&q=80",
    description: "Sub-millimeter laser distance and optical surface profiling module with RS-485 Modbus and IO-Link capability.",
    specs: {
      Resolution: "0.02 mm",
      Range: "50 mm - 10,000 mm",
      Protocol: "IO-Link v1.1 / Modbus-RTU",
      LaserClass: "Class 1 Safe Eye-Light",
      Housing: "Anodized Aircraft Aluminum",
    },
    variants: [
      { id: 201, sku: "APX-OPT-440-S", name: "Short Range (0.05 - 2m)", price: "320.00", inventory_level: 50, attributes: { Lens: "Short" } },
      { id: 202, sku: "APX-OPT-440-L", name: "Long Range (0.2 - 10m)", price: "410.00", inventory_level: 35, attributes: { Lens: "Telephoto" } },
    ],
    tiered_pricing: [
      { min_quantity: 1, unit_price: "320.00" },
      { min_quantity: 10, unit_price: "295.00", discount_percentage: "8%" },
      { min_quantity: 50, unit_price: "260.00", discount_percentage: "19%" },
    ],
  },
  {
    id: 3,
    title: "VoltMaster 48V High-Density Power Cell",
    slug: "voltmaster-48v-power-cell",
    sku: "VMT-48V-100AH",
    category_name: "Power Systems",
    brand_name: "VoltMaster Energy",
    base_price: "1450.00",
    currency: "USD",
    stock_status: "in_stock",
    stock_quantity: 19,
    image_url: "https://images.unsplash.com/photo-1558494949-ef010cbdcc31?w=800&auto=format&fit=crop&q=80",
    description: "48V 100Ah 5.12kWh LiFePO4 rack-mount energy module with built-in intelligent BMS, CAN-bus integration, and 6,000 cycle warranty.",
    specs: {
      NominalVoltage: "51.2 V",
      Capacity: "100 Ah / 5.12 kWh",
      CycleLife: "6000 cycles @ 80% DoD",
      FormFactor: "19-inch 3U Rackmount",
      Weight: "44.5 kg",
      Certifications: "UL1973, CE, IEC62619",
    },
    variants: [],
    tiered_pricing: [
      { min_quantity: 1, unit_price: "1450.00" },
      { min_quantity: 4, unit_price: "1350.00", discount_percentage: "7%" },
      { min_quantity: 12, unit_price: "1220.00", discount_percentage: "16%" },
    ],
  },
  {
    id: 4,
    title: "NovaFlex Actuator Servo Driver",
    slug: "novaflex-actuator-servo-driver",
    sku: "NVF-SRV-015",
    category_name: "Motion & Robotics",
    brand_name: "NovaMotion Labs",
    base_price: "580.00",
    currency: "USD",
    stock_status: "in_stock",
    stock_quantity: 63,
    image_url: "https://images.unsplash.com/photo-1581092160607-ee22621dd758?w=800&auto=format&fit=crop&q=80",
    description: "Synchronous AC brushless servo driver with EtherCAT real-time fieldbus, safe torque off (STO), and 1.5kW continuous rating.",
    specs: {
      PowerOutput: "1.5 kW Continuous (4.5kW Peak)",
      Fieldbus: "EtherCAT / CANopen",
      Feedback: "23-bit Optical Absolute Encoder",
      Protection: "Overcurrent, Short Circuit, Thermal",
    },
    variants: [
      { id: 401, sku: "NVF-SRV-015-ECAT", name: "EtherCAT Protocol", price: "580.00", inventory_level: 40, attributes: { Bus: "EtherCAT" } },
      { id: 402, sku: "NVF-SRV-015-CAN", name: "CANopen Protocol", price: "520.00", inventory_level: 23, attributes: { Bus: "CANopen" } },
    ],
    tiered_pricing: [
      { min_quantity: 1, unit_price: "580.00" },
      { min_quantity: 6, unit_price: "530.00", discount_percentage: "9%" },
      { min_quantity: 20, unit_price: "475.00", discount_percentage: "18%" },
    ],
  },
  {
    id: 5,
    title: "Strata Core Gigabit Managed Switch",
    slug: "strata-core-managed-switch",
    sku: "STR-SW-24P",
    category_name: "Networking",
    brand_name: "Strata Networks",
    base_price: "490.00",
    currency: "USD",
    stock_status: "in_stock",
    stock_quantity: 34,
    image_url: "https://images.unsplash.com/photo-1544197150-b99a580bb7a8?w=800&auto=format&fit=crop&q=80",
    description: "24-Port Gigabit DIN-rail industrial managed switch with 4x 10G SFP+ uplink cages, redundant power, and IEEE 1588v2 PTP sync.",
    specs: {
      Ports: "24x 10/100/1000Base-T + 4x 10G SFP+",
      PoECapacity: "370W Total PoE+ Budget",
      Management: "VLAN, QoS, LACP, SNMPv3, SSH",
      Mounting: "DIN-Rail & 19in Rack Mountable",
    },
    variants: [],
    tiered_pricing: [
      { min_quantity: 1, unit_price: "490.00" },
      { min_quantity: 5, unit_price: "450.00", discount_percentage: "8%" },
      { min_quantity: 20, unit_price: "399.00", discount_percentage: "19%" },
    ],
  },
  {
    id: 6,
    title: "Cerberus Thermal Imaging Imager",
    slug: "cerberus-thermal-imager",
    sku: "CBR-THM-640",
    category_name: "Sensors & Optics",
    brand_name: "Apex Instruments",
    base_price: "1890.00",
    currency: "USD",
    stock_status: "low_stock",
    stock_quantity: 6,
    image_url: "https://images.unsplash.com/photo-1508739773434-c26b3d09e071?w=800&auto=format&fit=crop&q=80",
    description: "Long-wave infrared (LWIR) 640x512 thermal radiometric camera for preventative maintenance, electrical inspections, and process monitoring.",
    specs: {
      SensorResolution: "640 x 512 Uncooled VOx Microbolometer",
      SpectralRange: "7.5 - 14 um",
      FrameRate: "30 Hz / 60 Hz",
      Interface: "GigE Vision / RTSP Streaming",
    },
    variants: [],
    tiered_pricing: [
      { min_quantity: 1, unit_price: "1890.00" },
      { min_quantity: 3, unit_price: "1750.00", discount_percentage: "7%" },
      { min_quantity: 10, unit_price: "1590.00", discount_percentage: "16%" },
    ],
  },
  {
    id: 7,
    title: "Helios Cryogenic Flow Regulator",
    slug: "helios-cryogenic-flow-regulator",
    sku: "HLO-CRY-012",
    category_name: "Power Systems",
    brand_name: "VoltMaster Energy",
    base_price: "2310.00",
    currency: "USD",
    stock_status: "out_of_stock",
    stock_quantity: 0,
    image_url: "https://images.unsplash.com/photo-1581092160607-ee22621dd758?w=800&auto=format&fit=crop&q=80",
    description: "Cryogenic helium flow regulator for superconducting test stands. Currently out of stock — backorder opens next quarter.",
    specs: {
      FlowRange: "0.1 - 12 g/s GHe",
      PressureRating: "25 bar MAWP",
      Interface: "CF40 Cryogenic Coupling",
    },
    variants: [],
    tiered_pricing: [{ min_quantity: 1, unit_price: "2310.00" }],
  },
];

/** URL slug for a name (demo catalog only: real slugs come from the ERP). */
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

type DemoSeed = Omit<
  Product,
  | "category_slug"
  | "brand_slug"
  | "long_description"
  | "seo_title"
  | "seo_meta_description"
  | "seo_og_title"
  | "seo_og_description"
>;

function withDemoDefaults(p: DemoSeed): Product {
  return {
    ...p,
    category_slug: slugify(p.category_name),
    brand_slug: p.brand_name ? slugify(p.brand_name) : null,
    long_description: p.description,
    seo_title: null,
    seo_meta_description: null,
    seo_og_title: null,
    seo_og_description: null,
  };
}

const DEMO_PRODUCTS: Product[] = DEMO_SEEDS.map(withDemoDefaults);

export const DEFAULT_API_BASE_URL = "http://localhost:8000/api";

/**
 * Read a VITE_* setting from the Vite client env first, then from the
 * Node/SSR process env (covers vitest + server-side route loaders).
 */
function readEnv(key: string): string {
  try {
    // Literal `import.meta.env`: Vite replaces exactly that expression with
    // the VITE_* values at build time. The old cast `import.meta?.env?.[key]`
    // was never replaced, so browsers ignored VITE_API_BASE_URL entirely and
    // every client-side call (checkout!) went to the localhost default.
    const v = (import.meta.env as Record<string, unknown> | undefined)?.[key];
    if (typeof v === "string" && v) return v;
  } catch {
    /* import.meta is unavailable in some runtimes — fall through */
  }
  try {
    const g = (globalThis as { process?: { env?: Record<string, unknown> } })?.process?.env?.[key];
    if (typeof g === "string" && g) return g;
  } catch {
    /* ignore */
  }
  return "";
}

/** Base URL for the Django API. Override per environment with VITE_API_BASE_URL. */
export function getApiBaseUrl(): string {
  return readEnv("VITE_API_BASE_URL").trim().replace(/\/+$/, "") || DEFAULT_API_BASE_URL;
}

/**
 * Origin (scheme + host) of the Django backend, derived from the API base
 * URL. Powers the ERP Admin / API-docs links so they track the environment
 * instead of pointing at localhost on deployed previews.
 */
export function getBackendOrigin(): string {
  try {
    return new URL(getApiBaseUrl()).origin;
  } catch {
    return "http://localhost:8000";
  }
}

/** Bearer token when configured (VITE_API_TOKEN or a previously stored session token). */
export function getApiToken(): string | undefined {
  const fromEnv = readEnv("VITE_API_TOKEN");
  if (fromEnv.trim()) return fromEnv.trim();
  if (typeof window !== "undefined") {
    try {
      return window.localStorage.getItem("golden_api_token") ?? undefined;
    } catch {
      return undefined;
    }
  }
  return undefined;
}

/**
 * Fetch a short-lived, least-privilege storefront token from the ERP
 * (F-037). The public endpoint issues tokens scoped to the dedicated
 * storefront identity — catalog reads + guest order creation only — so a
 * token visible in every visitor's browser is near-powerless if leaked.
 * The token is cached in localStorage under the same key getApiToken()
 * already reads; an env-provided VITE_API_TOKEN always takes precedence.
 * Returns null when the endpoint is unreachable (callers then send no
 * Authorization header rather than blocking).
 */
export async function fetchStorefrontToken(): Promise<string | null> {
  try {
    const res = await fetch(`${getApiBaseUrl()}/core/auth/storefront-token/`, {
      method: "POST",
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(2500),
    });
    if (!res.ok) return null;
    const data: unknown = await res.json();
    const token = (data as { token?: unknown })?.token;
    if (typeof token !== "string" || !token) return null;
    if (typeof window !== "undefined") {
      try {
        window.localStorage.setItem("golden_api_token", token);
      } catch {
        /* storage unavailable — the caller still gets the token */
      }
    }
    return token;
  } catch {
    return null;
  }
}

let storefrontTokenPromise: Promise<string | null> | null = null;

/** Test-only hook: clear the in-flight/resolved guest-token promise. */
export function __resetStorefrontTokenForTests(): void {
  storefrontTokenPromise = null;
}

/**
 * Resolve the token to send: an existing env/stored token wins; otherwise
 * fetch a guest storefront token once (in-flight requests share the same
 * promise). Resolves to undefined when no token is available.
 */
async function ensureStorefrontToken(): Promise<string | undefined> {
  const existing = getApiToken();
  if (existing) return existing;
  if (!storefrontTokenPromise) {
    storefrontTokenPromise = fetchStorefrontToken().catch(() => null);
  }
  return (await storefrontTokenPromise) ?? undefined;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Thrown when an order cannot be placed. The storefront never fabricates
 * confirmations: every failure path (offline, timeout, 4xx/5xx, malformed
 * confirmation) surfaces here so the UI can show an error state instead
 * of a fake order number (F-004).
 */
/** Why the ERP refused one cart line; `index` is the line's position in `lines`. */
export interface CheckoutLineError {
  index: number;
  code: string;
  message: string;
  available?: number;
}

export class OrderSubmitError extends ApiError {
  /** The ERP's stable refusal code (`invalid_lines`, `lines_required`, ...), if any. */
  code?: string;
  lineErrors: CheckoutLineError[];
  /**
   * True when the order may have been placed: the POST went out but no
   * refusal came back (timeout, network error, 5xx, unreadable answer).
   * The shopper must check before trying again, never be told "no order".
   */
  unconfirmed: boolean;
  constructor(
    message: string,
    status: number,
    code?: string,
    lineErrors: CheckoutLineError[] = [],
    unconfirmed = false,
  ) {
    super(message, status);
    this.name = "OrderSubmitError";
    this.code = code;
    this.lineErrors = lineErrors;
    this.unconfirmed = unconfirmed;
  }
}

/** The order POST went out and we can't tell whether it was placed. */
export const UNCONFIRMED_ORDER_MESSAGE =
  "We couldn't confirm your order. It may have gone through. If you submit the same cart again from this browser today, you'll get that order back, not a second one.";

const RETRYABLE_STATUSES = new Set([502, 503, 504]);

interface ApiFetchOptions {
  method?: string;
  body?: unknown;
  timeoutMs?: number;
  /** GETs retry once on 502/503/504 or network failure; POSTs never retry. */
  retry?: boolean;
  /** Explicit bearer token override; when omitted getApiToken() is used. */
  token?: string;
  /** Extra request headers (e.g. Idempotency-Key). */
  headers?: Record<string, string>;
}

async function apiFetch(path: string, options: ApiFetchOptions = {}): Promise<Response> {
  const { method = "GET", body, timeoutMs = 1500, retry = true, token } = options;
  const headers: Record<string, string> = { ...options.headers, Accept: "application/json" };
  const authToken = token ?? getApiToken();
  if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const canRetry = retry && method === "GET";
  for (let attempt = 0; ; attempt++) {
    try {
      const res = await fetch(`${getApiBaseUrl()}${path}`, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (res.status === 401) {
        throw new ApiError("Session is not authorized (HTTP 401). Sign in again.", 401);
      }
      if (RETRYABLE_STATUSES.has(res.status) && canRetry && attempt === 0) continue;
      return res;
    } catch (err) {
      if (err instanceof ApiError) throw err;
      if (canRetry && attempt === 0) continue;
      throw err;
    }
  }
}

/** Public catalog only, even when a shopper or guest-checkout token is attached. */
export const CATALOG_PATH = "/products/products/?visible_on_website=true&is_active=true";

/**
 * The built-in demo catalog is a development aid: offline dev servers and the
 * e2e suite render it when no API answers. A production build must never
 * pass fake products off as real ones, so there it only applies when
 * VITE_DEMO_CATALOG=1 is set explicitly.
 */
export function demoCatalogAllowed(): boolean {
  if (readEnv("VITE_DEMO_CATALOG") === "1") return true;
  // Must stay the literal `import.meta.env.DEV`: Vite replaces that exact
  // expression at build time, and at SSR runtime import.meta.env may not
  // exist — a guarded/cast access reads undefined even on the dev server.
  return import.meta.env.DEV === true;
}

function demoOrUnavailable(): Product[] {
  if (demoCatalogAllowed()) return DEMO_PRODUCTS;
  throw new ApiError("The catalog is unavailable right now.", 0);
}

const STOCK_STATUSES: Product["stock_status"][] = [
  "in_stock",
  "low_stock",
  "out_of_stock",
  "preorder",
  "backorder",
];

function mapVariant(v: any): ProductVariant | null {
  if (v.is_active === false || v.visible_on_website === false) return null;
  // The API sends each variant's selling price (a zero sale price means
  // "no sale" there); raw sale/retail only appear on staff responses.
  const price = v.price ?? (Number(v.sale_price) > 0 ? v.sale_price : v.retail_price) ?? null;
  if (price == null) return null;
  return {
    id: v.id,
    sku: v.sku ?? "",
    name: v.name || v.sku || "",
    price: String(price),
    inventory_level: v.stock ?? v.inventory_level ?? 0,
    attributes: v.attributes ?? {},
  };
}

/** Map an API product row to the storefront shape. Missing data stays missing. */
export function mapApiProduct(p: any): Product {
  const status = STOCK_STATUSES.includes(p.stock_status) ? p.stock_status : "out_of_stock";
  const price = p.price ?? p.base_price ?? null;
  return {
    id: p.id,
    title: p.title || p.name || "",
    slug: p.slug || `product-${p.id}`,
    sku: p.part_number || p.sku || "",
    description: p.summary ?? p.short_description ?? p.description ?? "",
    category_name: p.category_name || "Uncategorized",
    category_slug: p.category_slug || null,
    brand_name: p.brand_name || "",
    brand_slug: p.brand_slug || null,
    long_description: p.description || p.short_description || p.summary || "",
    seo_title: p.seo_title || null,
    seo_meta_description: p.seo_meta_description || null,
    seo_og_title: p.seo_og_title || null,
    seo_og_description: p.seo_og_description || null,
    base_price: price == null ? null : String(price),
    currency: p.currency || "USD",
    stock_status: status,
    stock_quantity: p.stock_quantity ?? 0,
    image_url: p.image_url || null,
    specs: p.specs || p.specifications || {},
    variants: (p.variants || []).map(mapVariant).filter(Boolean) as ProductVariant[],
    tiered_pricing: p.tiered_pricing || [],
  };
}

export async function fetchCatalog(options: { strict?: boolean } = {}): Promise<Product[]> {
  let res: Response;
  try {
    res = await apiFetch(CATALOG_PATH);
  } catch (err) {
    // A 401 must never degrade silently: the UI needs to redirect to login.
    if (err instanceof ApiError && (err.status === 401 || options.strict)) throw err;
    return demoOrUnavailable();
  }
  if (!res.ok) {
    if (options.strict) throw new ApiError(`Catalog request failed (HTTP ${res.status}).`, res.status);
    return demoOrUnavailable();
  }
  try {
    const data = await res.json();
    const results = Array.isArray(data) ? data : data.results || [];
    // An empty catalog is a real answer — never backfill it with demo data.
    return results.map(mapApiProduct);
  } catch {
    if (options.strict) throw new ApiError("Catalog response was not valid JSON.", 0);
    return demoOrUnavailable();
  }
}

/** One product with its sellable variants; undefined when it doesn't exist or isn't public. */
export async function fetchProductById(id: number | string): Promise<Product | undefined> {
  let res: Response;
  try {
    res = await apiFetch(`/products/products/${encodeURIComponent(String(id))}/`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return undefined;
    if (err instanceof ApiError && err.status === 401) throw err;
    return demoOrUnavailable().find((p) => String(p.id) === String(id));
  }
  if (res.status === 404) return undefined;
  if (!res.ok) return demoOrUnavailable().find((p) => String(p.id) === String(id));
  return mapApiProduct(await res.json());
}

/**
 * The cart as the ERP takes it: variant ids and whole quantities, no prices.
 * A line without a chosen variant sends the product's only variant, or the
 * product itself (the ERP resolves it or answers `variant_required`).
 */
export function checkoutLines(
  items: Pick<CartItem, "product" | "variantId" | "quantity">[],
): CheckoutLine[] {
  return items.map((item) => {
    const quantity = Math.max(0, Math.floor(item.quantity));
    if (item.variantId != null && item.variantId !== "") {
      return { variant: item.variantId, quantity };
    }
    const variants = item.product.variants || [];
    if (variants.length === 1) return { variant: variants[0].id, quantity };
    return { product: item.product.id, quantity };
  });
}

/** Shopper-facing copy for a refused line; branches on the code, never the ERP's text. */
export function lineErrorMessage(code: string, available?: number): string {
  switch (code) {
    case "out_of_stock":
      return "Out of stock. Remove it to order the rest of your cart.";
    case "insufficient_stock":
      return available != null && available > 0
        ? `Only ${available} in stock. Lower the quantity to ${available} or fewer.`
        : "Not enough in stock. Lower the quantity.";
    case "not_available":
      return "No longer available online. Remove it from your cart.";
    case "variant_required":
      return "This product comes in several options. Open it and choose one.";
    case "price_unavailable":
      return "No online price. Remove it and ask us for a quote.";
    case "invalid_quantity":
      return "Enter a whole quantity from 1 to 9,999.";
    case "order_too_large":
      return "Too large for an online order. Lower the quantity or ask us for a quote.";
    default:
      return "This item can't be ordered as it is. Remove it and add it again.";
  }
}

/** Shopper-facing copy for a refused checkout (400/503 with a `code`). */
export function checkoutErrorMessage(code: string | undefined, status: number): string {
  switch (code) {
    case "invalid_lines":
      return "Some items in your cart can't be ordered as they are. No order was placed.";
    case "lines_required":
      return "Your cart is empty. Add a product before checking out. No order was placed.";
    case "too_many_lines":
      return "An online order takes at most 50 lines. Split your cart into two orders. No order was placed.";
    case "order_too_large":
      return "This order is too large to place online. Ask us for a quote. No order was placed.";
    case "guest_checkout_unavailable":
      return "Online checkout is not available right now. No order was placed.";
    case "idempotency_key_reused":
      // Not "no order was placed": an order from this cart already exists.
      return "An order from this cart was already placed with different details. Check your email for its confirmation, and contact us to change it. Submitting again places a new order.";
    case "idempotency_key_invalid":
      return "Checkout couldn't start. Reload the page and try again. No order was placed.";
    default:
      if (status === 429) {
        // A retry may be refused here after its first submit went through.
        return "Too many orders from this connection. Wait a minute and try again: if your order already went through, submitting the same cart again from this browser returns it.";
      }
      return `Order was rejected by the ERP (HTTP ${status}). No order was placed.`;
  }
}

/** Codes the ERP answers before writing anything, whatever the status. */
const REFUSAL_CODES = new Set([
  "invalid_lines",
  "lines_required",
  "too_many_lines",
  "order_too_large",
  "guest_checkout_unavailable",
  "idempotency_key_reused",
  "idempotency_key_invalid",
]);

function asAmount(value: unknown, fallback: string): string {
  return typeof value === "string" && value !== "" ? value : fallback;
}

function mapConfirmedLine(line: any): ConfirmedLine {
  return {
    variant: line?.variant != null ? String(line.variant) : null,
    sku: line?.sku ?? "",
    item_name: line?.item_name ?? line?.sku ?? "",
    quantity: Number(line?.quantity) || 0,
    unit_price: asAmount(line?.unit_price, "0.00"),
    line_total: asAmount(line?.line_total, "0.00"),
  };
}

const CHECKOUT_KEY_STORAGE = "golden_checkout_key";
/** The ERP replays a key for 24 hours (orders.guest_checkout.IDEMPOTENCY_WINDOW). */
const CHECKOUT_KEY_TTL_MS = 23 * 60 * 60 * 1000;

interface StoredCheckoutKey {
  fingerprint: string;
  key: string;
  at: number;
}

// When storage is unavailable (private mode, quota, SSR) the key still lives
// for this page, so a retry without a reload is still deduplicated.
let memoryCheckoutKey: StoredCheckoutKey | null = null;

function newCheckoutKey(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  if (c && typeof c.getRandomValues === "function") {
    const bytes = new Uint8Array(16);
    c.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  // No Web Crypto: send no key rather than a guessable one.
  return "";
}

function checkoutFingerprint(payload: CheckoutPayload): string {
  const lines = payload.lines
    .map((l: any) => `${l.variant ?? ""}|${l.product ?? ""}|${l.quantity}`)
    .sort();
  return JSON.stringify([payload.email.trim().toLowerCase(), lines]);
}

function readStoredKey(): StoredCheckoutKey | null {
  try {
    const raw = localStorage.getItem(CHECKOUT_KEY_STORAGE);
    if (raw) return JSON.parse(raw);
  } catch {
    // fall through to the in-page copy
  }
  return memoryCheckoutKey;
}

/**
 * The Idempotency-Key for this checkout (OP #3431). A retry of the same
 * email and cart from this browser within a day reuses the key, so an order
 * that committed before a timeout comes back instead of being placed twice.
 * A changed cart starts a new key; changed details (address, coupon, ...)
 * keep it, and the ERP refuses them with 409 instead of answering the old
 * order. Kept in localStorage so a reload or a closed tab doesn't lose it.
 */
export function checkoutKeyFor(payload: CheckoutPayload): string {
  const fingerprint = checkoutFingerprint(payload);
  const now = Date.now();
  const stored = readStoredKey();
  if (
    stored &&
    stored.fingerprint === fingerprint &&
    typeof stored.key === "string" &&
    stored.key &&
    typeof stored.at === "number" &&
    now - stored.at < CHECKOUT_KEY_TTL_MS
  ) {
    return stored.key;
  }
  const key = newCheckoutKey();
  if (!key) return "";
  memoryCheckoutKey = { fingerprint, key, at: now };
  try {
    localStorage.setItem(CHECKOUT_KEY_STORAGE, JSON.stringify(memoryCheckoutKey));
  } catch {
    // the in-page copy still covers a retry without a reload
  }
  return key;
}

/** Forget the key once its order is confirmed, so the next cart gets a new one. */
export function clearCheckoutKey(): void {
  memoryCheckoutKey = null;
  try {
    localStorage.removeItem(CHECKOUT_KEY_STORAGE);
  } catch {
    // ignore
  }
}

export async function submitOrder(
  payload: CheckoutPayload,
  idempotencyKey?: string,
): Promise<OrderConfirmation> {
  // Guest checkout (F-037): make sure we have a bearer token before POSTing.
  // The public storefront-token endpoint issues the least-privilege identity;
  // failure to obtain one is not fatal here — the POST below will then 401
  // and surface as an OrderSubmitError, which is the honest outcome.
  const token = await ensureStorefrontToken();

  let res: Response;
  try {
    // Never retried automatically. A shopper's retry sends the same
    // Idempotency-Key (checkoutKeyFor), so the ERP answers the order it
    // already made instead of a second one. The order collection lives at
    // /orders/ within the orders API include (the include root is DRF's
    // API root, which rejects POST).
    res = await apiFetch("/orders/orders/", {
      method: "POST",
      body: payload,
      timeoutMs: 5000,
      retry: false,
      token,
      headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
    });
  } catch (err) {
    if (err instanceof ApiError) {
      throw new OrderSubmitError(
        err.status === 401
          ? "Order was rejected: session is not authorized (HTTP 401). Sign in and try again."
          : `Order request failed (${err.message}).`,
        err.status,
      );
    }
    // A timeout or a dropped connection after the POST went out: the order
    // may exist. Never tell the shopper it doesn't.
    throw new OrderSubmitError(UNCONFIRMED_ORDER_MESSAGE, 0, undefined, [], true);
  }
  if (!res.ok) {
    if (res.status === 401) {
      throw new OrderSubmitError(
        "Order was rejected: session is not authorized (HTTP 401). Sign in and try again.",
        res.status,
      );
    }
    let body: any = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    const code = typeof body?.code === "string" ? body.code : undefined;
    const lineErrors: CheckoutLineError[] = Array.isArray(body?.lines)
      ? body.lines
          .filter((e: any) => typeof e?.index === "number" && typeof e?.code === "string")
          .map((e: any) => ({
            index: e.index,
            code: e.code,
            available: typeof e.available === "number" ? e.available : undefined,
            message: lineErrorMessage(e.code, typeof e.available === "number" ? e.available : undefined),
          }))
      : [];
    // Definite only for a real refusal: a 4xx with a body, or a known refusal
    // code. A 5xx or an empty answer leaves the outcome unknown.
    const refused =
      (res.status >= 400 && res.status < 500 && body != null) ||
      (code != null && REFUSAL_CODES.has(code));
    if (!refused) {
      throw new OrderSubmitError(UNCONFIRMED_ORDER_MESSAGE, res.status, code, [], true);
    }
    const message =
      !code && res.status === 400 && "guest_email" in body
        ? "Enter your email address so we can confirm the order. No order was placed."
        : checkoutErrorMessage(code, res.status);
    throw new OrderSubmitError(message, res.status, code, lineErrors);
  }
  let data: any;
  try {
    data = await res.json();
  } catch {
    // The ERP answered success: the order most likely exists.
    throw new OrderSubmitError(UNCONFIRMED_ORDER_MESSAGE, res.status, undefined, [], true);
  }
  if (!data || typeof data.order_number !== "string" || !data.order_number) {
    throw new OrderSubmitError(UNCONFIRMED_ORDER_MESSAGE, res.status, undefined, [], true);
  }
  const total = asAmount(data.total_amount ?? data.grand_total, "0.00");
  return {
    order_number: data.order_number,
    total_amount: total,
    currency: data.currency || "USD",
    status: data.status || "pending",
    payment_status: data.payment_status || "unpaid",
    created_at: data.created_at || new Date().toISOString(),
    lines: Array.isArray(data.lines) ? data.lines.map(mapConfirmedLine) : [],
    subtotal: asAmount(data.subtotal, total),
    tax_total: asAmount(data.tax_total, "0.00"),
    shipping_total: asAmount(data.shipping_total, "0.00"),
    discount_total: asAmount(data.discount_total, "0.00"),
  };
}

// --- Categories and brands (docs/storefront-seo.md) --------------------------

export interface CategoryLink {
  id: string;
  slug: string;
  title: string;
}

export interface Category {
  id: string;
  slug: string;
  title: string;
  parent_slug: string | null;
  description: string;
  short_description: string;
  /** ERP-approved long-form markdown; null renders no block. */
  body_copy: string | null;
  image_url: string | null;
  seo_title: string | null;
  seo_meta_description: string | null;
  seo_og_title: string | null;
  seo_og_description: string | null;
  seo_og_image_url: string | null;
  /** Set only when another URL is the preferred copy of this page. */
  seo_canonical_url: string | null;
  /** Root-first breadcrumb trail above this category. */
  ancestors: CategoryLink[];
  /** Visible sub-categories, in ERP sort order. */
  children: CategoryLink[];
}

export interface Brand {
  id: string;
  slug: string;
  title: string;
  description: string;
  website_url: string;
  logo_url: string | null;
  banner_url: string | null;
  seo_title: string | null;
  seo_meta_description: string | null;
  seo_og_title: string | null;
  seo_og_description: string | null;
  seo_og_image_url: string | null;
}

export interface ProductPage {
  products: Product[];
  /** Total matching products across every page. */
  count: number;
  page: number;
  hasNext: boolean;
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");
const strOrNull = (v: unknown): string | null =>
  typeof v === "string" && v.trim() ? v : null;

function mapLink(l: any): CategoryLink {
  return { id: String(l?.id ?? ""), slug: str(l?.slug), title: str(l?.title) };
}

export function mapApiCategory(c: any): Category {
  return {
    id: String(c.id),
    slug: str(c.slug),
    title: str(c.title),
    parent_slug: strOrNull(c.parent_slug),
    description: str(c.description),
    short_description: str(c.short_description),
    body_copy: strOrNull(c.body_copy),
    image_url: strOrNull(c.image_url),
    seo_title: strOrNull(c.seo_title),
    seo_meta_description: strOrNull(c.seo_meta_description),
    seo_og_title: strOrNull(c.seo_og_title),
    seo_og_description: strOrNull(c.seo_og_description),
    seo_og_image_url: strOrNull(c.seo_og_image_url),
    seo_canonical_url: strOrNull(c.seo_canonical_url),
    ancestors: Array.isArray(c.ancestors) ? c.ancestors.map(mapLink) : [],
    children: Array.isArray(c.children) ? c.children.map(mapLink) : [],
  };
}

export function mapApiBrand(b: any): Brand {
  return {
    id: String(b.id),
    slug: str(b.slug),
    title: str(b.title),
    description: str(b.description),
    website_url: str(b.website_url),
    logo_url: strOrNull(b.logo_url),
    banner_url: strOrNull(b.banner_url),
    seo_title: strOrNull(b.seo_title),
    seo_meta_description: strOrNull(b.seo_meta_description),
    seo_og_title: strOrNull(b.seo_og_title),
    seo_og_description: strOrNull(b.seo_og_description),
    seo_og_image_url: strOrNull(b.seo_og_image_url),
  };
}

function demoCategories(): Category[] {
  const names = Array.from(new Set(demoOrUnavailable().map((p) => p.category_name)));
  return names.map((name) =>
    mapApiCategory({ id: `demo-${slugify(name)}`, slug: slugify(name), title: name }),
  );
}

function demoBrands(): Brand[] {
  const names = Array.from(
    new Set(demoOrUnavailable().map((p) => p.brand_name).filter(Boolean)),
  );
  return names.map((name) =>
    mapApiBrand({ id: `demo-${slugify(name)}`, slug: slugify(name), title: name }),
  );
}

/** Every row of a paginated list (bounded, so a runaway API can't loop us). */
async function fetchAllPages(path: string, maxPages = 20): Promise<any[]> {
  const rows: any[] = [];
  for (let page = 1; page <= maxPages; page++) {
    const sep = path.includes("?") ? "&" : "?";
    const res = await apiFetch(`${path}${sep}page=${page}`);
    if (!res.ok) throw new ApiError(`Request failed (HTTP ${res.status}).`, res.status);
    const data = await res.json();
    if (Array.isArray(data)) return data;
    rows.push(...(data.results || []));
    if (!data.next) break;
  }
  return rows;
}

async function withDemoFallback<T>(live: () => Promise<T>, demo: () => T): Promise<T> {
  try {
    return await live();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) throw err;
    if (demoCatalogAllowed()) return demo();
    throw new ApiError("The catalog is unavailable right now.", 0);
  }
}

/** Every visible category (flat; rebuild the tree from parent_slug). */
export function fetchCategories(): Promise<Category[]> {
  return withDemoFallback(
    async () => (await fetchAllPages("/products/categories/")).map(mapApiCategory),
    demoCategories,
  );
}

/** One visible category by slug; undefined when it doesn't exist or is hidden. */
export function fetchCategoryBySlug(slug: string): Promise<Category | undefined> {
  return withDemoFallback(
    async () => {
      const rows = await fetchAllPages(
        `/products/categories/?slug=${encodeURIComponent(slug)}`,
        1,
      );
      return rows.length ? mapApiCategory(rows[0]) : undefined;
    },
    () => demoCategories().find((c) => c.slug === slug),
  );
}

/** Every visible brand, in ERP sort order. */
export function fetchBrands(): Promise<Brand[]> {
  return withDemoFallback(
    async () => (await fetchAllPages("/products/brands/")).map(mapApiBrand),
    demoBrands,
  );
}

/** One visible brand by slug; undefined when it doesn't exist or is hidden. */
export function fetchBrandBySlug(slug: string): Promise<Brand | undefined> {
  return withDemoFallback(
    async () => {
      const rows = await fetchAllPages(`/products/brands/?slug=${encodeURIComponent(slug)}`, 1);
      return rows.length ? mapApiBrand(rows[0]) : undefined;
    },
    () => demoBrands().find((b) => b.slug === slug),
  );
}

/**
 * One page of public products in a category (its whole subtree) or a brand.
 * Pages are the ERP's (25 rows); `page` is 1-based.
 */
export function fetchProductPage(filter: {
  categorySlug?: string;
  brandSlug?: string;
  page?: number;
}): Promise<ProductPage> {
  const page = Math.max(1, Math.floor(filter.page ?? 1));
  return withDemoFallback(
    async () => {
      const params = new URLSearchParams({ page: String(page) });
      if (filter.categorySlug) params.set("category_slug", filter.categorySlug);
      if (filter.brandSlug) params.set("brand_slug", filter.brandSlug);
      const res = await apiFetch(`${CATALOG_PATH}&${params.toString()}`);
      if (res.status === 404 && page > 1) {
        return { products: [], count: 0, page, hasNext: false };
      }
      if (!res.ok) throw new ApiError(`Catalog request failed (HTTP ${res.status}).`, res.status);
      const data = await res.json();
      const rows: any[] = Array.isArray(data) ? data : data.results || [];
      return {
        products: rows.map(mapApiProduct),
        count: Array.isArray(data) ? rows.length : Number(data.count ?? rows.length),
        page,
        hasNext: !Array.isArray(data) && Boolean(data.next),
      };
    },
    () => {
      const products = demoOrUnavailable().filter(
        (p) =>
          (!filter.categorySlug || p.category_slug === filter.categorySlug) &&
          (!filter.brandSlug || p.brand_slug === filter.brandSlug),
      );
      return { products, count: products.length, page: 1, hasNext: false };
    },
  );
}
