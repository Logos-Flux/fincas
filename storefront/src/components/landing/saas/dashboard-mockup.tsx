import { component$ } from "@builder.io/qwik";

/* Weekly unit demand for one SKU: 12 weeks of actuals, then 6 forecast. */
const ACTUAL = [42, 48, 45, 53, 50, 58, 61, 57, 64, 70, 66, 73];
const FORECAST = [76, 80, 78, 85, 90, 94];
const SPREAD = [3, 5, 6, 8, 10, 12];

const W = 600;
const H = 190;
const STEP = W / (ACTUAL.length + FORECAST.length - 1);
const y = (v: number) => H - 12 - (v - 30) * 2.2;
const pt = (i: number, v: number) =>
  `${(i * STEP).toFixed(1)},${y(v).toFixed(1)}`;

const actualPath = "M" + ACTUAL.map((v, i) => pt(i, v)).join(" L");
const lastIdx = ACTUAL.length - 1;
const forecastPath =
  "M" +
  [
    pt(lastIdx, ACTUAL[lastIdx]),
    ...FORECAST.map((v, i) => pt(lastIdx + 1 + i, v)),
  ].join(" L");
const bandPath =
  "M" +
  pt(lastIdx, ACTUAL[lastIdx]) +
  " L" +
  FORECAST.map((v, i) => pt(lastIdx + 1 + i, v + SPREAD[i])).join(" L") +
  " L" +
  FORECAST.map((v, i) => pt(lastIdx + 1 + i, v - SPREAD[i]))
    .reverse()
    .join(" L") +
  " Z";
const areaPath = actualPath + ` L${(lastIdx * STEP).toFixed(1)},${H} L0,${H} Z`;

const ROWS = [
  {
    sku: "NW-2210",
    item: "Corrugated mailer 12×9",
    loc: "Reno DC",
    onHand: "1,840",
    cover: "6 days",
    status: "Reorder now",
    tone: "bg-t-error",
  },
  {
    sku: "NW-0418",
    item: "Thermal labels 4×6",
    loc: "Columbus",
    onHand: "22 rolls",
    cover: "11 days",
    status: "Reorder Thu",
    tone: "bg-t-warning",
  },
  {
    sku: "NW-1175",
    item: "Stretch wrap 18in",
    loc: "Reno DC",
    onHand: "96",
    cover: "34 days",
    status: "On track",
    tone: "bg-t-success",
  },
  {
    sku: "NW-3302",
    item: "Poly bag 10×13",
    loc: "Columbus",
    onHand: "12,400",
    cover: "81 days",
    status: "Overstock",
    tone: "bg-t-info",
  },
];

const NAV = [
  { label: "Reorder queue", count: "7", active: true },
  { label: "Forecasts", count: "" },
  { label: "Suppliers", count: "38" },
  { label: "Transfers", count: "2" },
  { label: "Approvals", count: "3" },
  { label: "Promo calendar", count: "" },
];

/**
 * The hero's product screenshot, drawn entirely with theme tokens so it
 * reskins with every theme. Purely illustrative, so it is hidden from
 * assistive tech and summarised by the caption beside it instead.
 */
export const DashboardMockup = component$(() => {
  return (
    <div class="relative" aria-hidden="true">
      <div class="rounded-xl border border-t-border bg-t-surface shadow-2xl overflow-hidden">
        {/* window chrome */}
        <div class="flex items-center gap-3 px-4 h-10 border-b border-t-border bg-t-card">
          <div class="flex gap-1.5">
            <span class="w-2.5 h-2.5 rounded-full bg-t-hover" />
            <span class="w-2.5 h-2.5 rounded-full bg-t-hover" />
            <span class="w-2.5 h-2.5 rounded-full bg-t-hover" />
          </div>
          <div class="flex-1 max-w-sm mx-auto h-6 rounded-md bg-t-deep border border-t-border text-[11px] text-t-muted flex items-center px-3 font-[family-name:var(--font-mono)] truncate">
            app.northwind.example/queue
          </div>
        </div>

        <div class="flex">
          {/* sidebar */}
          <div class="hidden md:flex flex-col w-48 shrink-0 border-r border-t-border p-3 gap-0.5 text-[13px] bg-t-deep">
            <div class="flex items-center gap-2 px-2 py-2 mb-2">
              <span class="w-5 h-5 rounded bg-t-accent" />
              <span class="font-semibold text-t-bright">Halvard Supply</span>
            </div>
            {NAV.map((n) => (
              <div
                key={n.label}
                class={[
                  "flex items-center justify-between px-2 py-1.5 rounded-md",
                  n.active ? "bg-t-hover text-t-bright" : "text-t-muted",
                ]}
              >
                <span>{n.label}</span>
                {n.count && (
                  <span class="text-[11px] font-[family-name:var(--font-mono)]">
                    {n.count}
                  </span>
                )}
              </div>
            ))}
            <div class="mt-auto pt-6 px-2 text-[11px] text-t-muted leading-relaxed">
              Forecast refreshed
              <br />
              <span class="font-[family-name:var(--font-mono)] text-t-text">
                Today 06:04
              </span>
            </div>
          </div>

          {/* main */}
          <div class="flex-1 min-w-0 p-4 sm:p-5 space-y-4">
            <div class="flex items-center justify-between gap-3">
              <div>
                <div class="text-[11px] uppercase tracking-wider text-t-muted">
                  Reorder queue · 2 locations
                </div>
                <div class="text-base sm:text-lg font-semibold text-t-bright">
                  7 SKUs need a decision this week
                </div>
              </div>
              <span class="hidden sm:inline-flex items-center px-3 py-1.5 rounded-md bg-t-accent text-t-on-accent text-xs font-semibold">
                Draft 4 POs
              </span>
            </div>

            {/* KPIs */}
            <div class="grid grid-cols-3 gap-2 sm:gap-3">
              {[
                { k: "Fill rate", v: "98.4%", d: "+2.1 pts" },
                { k: "Stockout risk", v: "7 SKUs", d: "−5 vs last wk" },
                { k: "Open PO value", v: "$184,210", d: "12 POs" },
              ].map((s) => (
                <div
                  key={s.k}
                  class="rounded-lg border border-t-border bg-t-card p-2.5 sm:p-3"
                >
                  <div class="text-[10px] sm:text-[11px] text-t-muted truncate">
                    {s.k}
                  </div>
                  <div class="text-sm sm:text-xl font-semibold text-t-bright font-[family-name:var(--font-mono)] tracking-tight">
                    {s.v}
                  </div>
                  <div class="text-[10px] sm:text-[11px] text-t-muted truncate">
                    {s.d}
                  </div>
                </div>
              ))}
            </div>

            {/* chart */}
            <div class="rounded-lg border border-t-border bg-t-card p-3 sm:p-4">
              <div class="flex flex-wrap items-center justify-between gap-2 mb-2">
                <div class="text-xs text-t-text">
                  <span class="font-semibold text-t-bright">NW-2210</span>{" "}
                  weekly demand, Reno DC
                </div>
                <div class="flex items-center gap-3 text-[11px] text-t-muted">
                  <span class="flex items-center gap-1.5">
                    <span class="w-3 h-0.5 bg-t-accent" /> Actual
                  </span>
                  <span class="flex items-center gap-1.5">
                    <span class="w-3 h-0.5 border-t-2 border-dashed border-t-accent-alt" />{" "}
                    Forecast
                  </span>
                </div>
              </div>
              <svg
                viewBox={`0 0 ${W} ${H}`}
                class="w-full h-28 sm:h-40"
                preserveAspectRatio="none"
              >
                {[0.25, 0.5, 0.75].map((f) => (
                  <line
                    key={f}
                    x1="0"
                    x2={W}
                    y1={H * f}
                    y2={H * f}
                    stroke="currentColor"
                    class="text-t-border"
                    stroke-width="1"
                    vector-effect="non-scaling-stroke"
                  />
                ))}
                <path
                  d={areaPath}
                  fill="currentColor"
                  fill-opacity="0.12"
                  class="text-t-accent"
                />
                <path
                  d={bandPath}
                  fill="currentColor"
                  fill-opacity="0.18"
                  class="text-t-accent-alt"
                />
                <line
                  x1={lastIdx * STEP}
                  x2={lastIdx * STEP}
                  y1="0"
                  y2={H}
                  stroke="currentColor"
                  class="text-t-muted"
                  stroke-dasharray="3 4"
                  vector-effect="non-scaling-stroke"
                />
                <path
                  d={actualPath}
                  fill="none"
                  stroke="currentColor"
                  class="text-t-accent"
                  stroke-width="2.5"
                  vector-effect="non-scaling-stroke"
                  stroke-linejoin="round"
                />
                <path
                  d={forecastPath}
                  fill="none"
                  stroke="currentColor"
                  class="text-t-accent-alt"
                  stroke-width="2.5"
                  stroke-dasharray="6 5"
                  vector-effect="non-scaling-stroke"
                  stroke-linejoin="round"
                />
              </svg>
              <div class="flex justify-between text-[10px] text-t-muted font-[family-name:var(--font-mono)] mt-1">
                <span>Jul 7</span>
                <span>Aug 18</span>
                <span class="text-t-text">Today</span>
                <span>Nov 3</span>
              </div>
            </div>

            {/* table */}
            <div class="rounded-lg border border-t-border overflow-hidden text-xs">
              <div class="grid grid-cols-[1fr_auto] sm:grid-cols-[5rem_1fr_6rem_5rem_6.5rem] gap-3 px-3 py-2 bg-t-card text-[10px] uppercase tracking-wider text-t-muted">
                <span class="hidden sm:block">SKU</span>
                <span>Item</span>
                <span class="hidden sm:block">Location</span>
                <span class="hidden sm:block text-right">Cover</span>
                <span>Status</span>
              </div>
              {ROWS.map((r) => (
                <div
                  key={r.sku}
                  class="grid grid-cols-[1fr_auto] sm:grid-cols-[5rem_1fr_6rem_5rem_6.5rem] gap-3 px-3 py-2 border-t border-t-border items-center"
                >
                  <span class="hidden sm:block font-[family-name:var(--font-mono)] text-t-muted">
                    {r.sku}
                  </span>
                  <span class="text-t-bright truncate">{r.item}</span>
                  <span class="hidden sm:block text-t-muted">{r.loc}</span>
                  <span class="hidden sm:block text-right font-[family-name:var(--font-mono)] text-t-text">
                    {r.cover}
                  </span>
                  <span class="inline-flex items-center gap-1.5 text-t-text whitespace-nowrap">
                    <span class={["w-1.5 h-1.5 rounded-full", r.tone]} />
                    {r.status}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* floating PO card */}
      <div class="hidden lg:block absolute -left-10 top-[21.5rem] w-60 rounded-xl border border-t-border-active bg-t-deep [background-image:linear-gradient(var(--color-t-card),var(--color-t-card))] shadow-2xl p-4 space-y-3">
        <div class="flex items-center justify-between text-[11px]">
          <span class="font-[family-name:var(--font-mono)] text-t-muted">
            PO-4471
          </span>
          <span class="text-t-accent-ink font-semibold whitespace-nowrap">
            Needs approval
          </span>
        </div>
        <div>
          <div class="text-sm font-semibold text-t-bright">
            Kestrel Packaging
          </div>
          <div class="text-xs text-t-muted">24,000 × NW-2210 · $0.41 ea</div>
        </div>
        <div class="flex justify-between text-xs border-t border-t-border pt-3">
          <span class="text-t-muted">Arrives</span>
          <span class="text-t-bright font-[family-name:var(--font-mono)]">
            Oct 14 (lead 9d)
          </span>
        </div>
        <div class="flex gap-2">
          <span class="flex-1 text-center py-1.5 rounded-md bg-t-accent text-t-on-accent text-xs font-semibold">
            Approve $9,840
          </span>
          <span class="px-3 py-1.5 rounded-md border border-t-border text-xs text-t-text">
            Edit
          </span>
        </div>
      </div>
    </div>
  );
});
