import { component$ } from "@builder.io/qwik";

const PROMO = [
  { w: "W40", base: 38, lift: 0 },
  { w: "W41", base: 40, lift: 0 },
  { w: "W42", base: 41, lift: 26 },
  { w: "W43", base: 39, lift: 31 },
  { w: "W44", base: 42, lift: 9 },
  { w: "W45", base: 44, lift: 0 },
  { w: "W46", base: 52, lift: 0 },
  { w: "W47", base: 61, lift: 18 },
];

const LEAD = [
  { s: "Kestrel Packaging", quoted: 7, actual: 9 },
  { s: "Brava Labelworks", quoted: 5, actual: 5 },
  { s: "Oakline Poly", quoted: 10, actual: 16 },
];

const SOURCES = [
  "ERP over REST",
  "WMS webhooks",
  "Nightly SFTP CSV",
  "EDI 850 / 855",
  "Storefront orders",
  "Sheets export",
];

/** Bento grid of features — each cell carries its own small illustration. */
export const FeatureBento = component$(() => {
  return (
    <div class="grid grid-cols-1 md:grid-cols-6 gap-4">
      {/* 1 — promo-aware forecasts (large) */}
      <article class="md:col-span-4 md:row-span-2 rounded-2xl border border-t-border bg-t-card p-6 sm:p-8 flex flex-col gap-6">
        <div class="max-w-md space-y-2">
          <p class="text-xs font-semibold uppercase tracking-wider text-t-accent-ink">
            Forecasting
          </p>
          <h3 class="text-xl sm:text-2xl font-bold text-t-bright tracking-tight font-[family-name:var(--font-display)]">
            Forecasts that read your promo calendar
          </h3>
          <p class="text-sm text-t-text leading-relaxed">
            Northwind models every SKU at every location separately, then layers
            planned promotions, price changes and holiday weeks on top. A
            buy-one-get-one in week 42 shows up as demand in week 42, not as a
            surprise stockout in week 43.
          </p>
        </div>
        <div class="mt-auto" aria-hidden="true">
          <div class="flex items-end gap-2 sm:gap-3 h-44">
            {PROMO.map((p) => (
              <div
                key={p.w}
                class="flex-1 flex flex-col items-center gap-2 h-full justify-end"
              >
                <div class="w-full flex flex-col justify-end h-full">
                  {p.lift > 0 && (
                    <div
                      class="w-full rounded-t-md bg-t-accent-alt/35 border border-dashed border-t-accent-alt"
                      style={{ height: `${p.lift * 1.2}%` }}
                    />
                  )}
                  <div
                    class={[
                      "w-full bg-t-accent",
                      p.lift > 0 ? "" : "rounded-t-md",
                    ]}
                    style={{ height: `${p.base * 1.2}%` }}
                  />
                </div>
                <span class="text-[10px] font-[family-name:var(--font-mono)] text-t-muted">
                  {p.w}
                </span>
              </div>
            ))}
          </div>
          <div class="flex flex-wrap gap-4 mt-4 text-xs text-t-muted">
            <span class="flex items-center gap-1.5">
              <span class="w-2.5 h-2.5 rounded-sm bg-t-accent" /> Baseline
              demand
            </span>
            <span class="flex items-center gap-1.5">
              <span class="w-2.5 h-2.5 rounded-sm bg-t-accent-alt/35 border border-dashed border-t-accent-alt" />{" "}
              Promo lift (planned)
            </span>
          </div>
        </div>
      </article>

      {/* 2 — lead times */}
      <article class="md:col-span-2 rounded-2xl border border-t-border bg-t-card p-6 space-y-4">
        <h3 class="text-lg font-bold text-t-bright tracking-tight font-[family-name:var(--font-display)]">
          Lead times measured, not quoted
        </h3>
        <p class="text-sm text-t-text leading-relaxed">
          We time every receipt against its PO, so reorder points use the 9 days
          a supplier actually takes.
        </p>
        <ul class="space-y-3 text-xs">
          {LEAD.map((l) => (
            <li key={l.s} class="space-y-1">
              <div class="flex justify-between gap-2">
                <span class="text-t-text truncate">{l.s}</span>
                <span class="font-[family-name:var(--font-mono)] text-t-muted whitespace-nowrap">
                  {l.quoted}d quoted ·{" "}
                  <span class="text-t-bright">{l.actual}d real</span>
                </span>
              </div>
              <div
                class="relative h-1.5 rounded-full bg-t-hover"
                aria-hidden="true"
              >
                <div
                  class="absolute inset-y-0 left-0 rounded-full bg-t-accent"
                  style={{ width: `${(l.actual / 16) * 100}%` }}
                />
                <div
                  class="absolute -top-1 w-0.5 h-3.5 bg-t-bright"
                  style={{ left: `${(l.quoted / 16) * 100}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
      </article>

      {/* 3 — approval rules */}
      <article class="md:col-span-2 rounded-2xl border border-t-border bg-t-card p-6 space-y-4">
        <h3 class="text-lg font-bold text-t-bright tracking-tight font-[family-name:var(--font-display)]">
          Approval rules in plain terms
        </h3>
        <div class="space-y-2 text-xs font-[family-name:var(--font-mono)]">
          <div class="rounded-lg border border-t-border bg-t-deep px-3 py-2 text-t-text">
            <span class="text-t-accent-ink">if</span> total &lt; $2,000{" "}
            <span class="text-t-accent-ink">and</span> repeat item
            <div class="text-t-muted">→ auto-approve</div>
          </div>
          <div class="rounded-lg border border-t-border bg-t-deep px-3 py-2 text-t-text">
            <span class="text-t-accent-ink">if</span> total &gt; $25,000
            <div class="text-t-muted">→ ops director, then finance</div>
          </div>
        </div>
        <div class="flex items-center -space-x-2" aria-hidden="true">
          {["MR", "JT", "AO"].map((i, n) => (
            <span
              key={i}
              class={[
                "w-8 h-8 rounded-full grid place-items-center text-[11px] font-bold border-2 border-t-card",
                n === 0
                  ? "bg-t-accent text-t-on-accent"
                  : "bg-t-hover text-t-bright",
              ]}
            >
              {i}
            </span>
          ))}
          <span class="pl-4 text-xs text-t-muted">3 approvers on PO-4471</span>
        </div>
      </article>

      {/* 4 — transfers */}
      <article class="md:col-span-3 rounded-2xl border border-t-border bg-t-surface p-6 space-y-5">
        <div class="space-y-2">
          <h3 class="text-lg font-bold text-t-bright tracking-tight font-[family-name:var(--font-display)]">
            Transfer before you buy
          </h3>
          <p class="text-sm text-t-text leading-relaxed">
            When one warehouse is short and another is sitting on 80 days of
            cover, Northwind suggests a transfer first. Customers move about 11%
            of reorder volume this way.
          </p>
        </div>
        <div class="flex items-center gap-3 text-xs" aria-hidden="true">
          <div class="flex-1 rounded-lg border border-t-border bg-t-card p-3">
            <div class="text-t-muted">Columbus</div>
            <div class="font-[family-name:var(--font-mono)] text-t-bright">
              81d cover
            </div>
          </div>
          <div class="flex flex-col items-center text-t-accent-ink shrink-0">
            <span class="font-[family-name:var(--font-mono)] font-semibold">
              640 u
            </span>
            <svg class="w-12 h-3" viewBox="0 0 48 12" fill="none">
              <path
                d="M0 6h44m0 0-5-5m5 5-5 5"
                stroke="currentColor"
                stroke-width="1.5"
              />
            </svg>
          </div>
          <div class="flex-1 rounded-lg border border-t-border-active bg-t-card p-3">
            <div class="text-t-muted">Reno DC</div>
            <div class="font-[family-name:var(--font-mono)] text-t-bright">
              6d → 19d
            </div>
          </div>
        </div>
      </article>

      {/* 5 — integrations */}
      <article class="md:col-span-3 rounded-2xl border border-t-border bg-t-surface p-6 space-y-5">
        <div class="space-y-2">
          <h3 class="text-lg font-bold text-t-bright tracking-tight font-[family-name:var(--font-display)]">
            Reads from what you already run
          </h3>
          <p class="text-sm text-t-text leading-relaxed">
            Sales, receipts and on-hand come in nightly or in real time.
            Approved POs go back out to your ERP or straight to the supplier.
          </p>
        </div>
        <ul class="flex flex-wrap gap-2">
          {SOURCES.map((s) => (
            <li
              key={s}
              class="px-3 py-1.5 rounded-full border border-t-border bg-t-card text-xs text-t-text font-[family-name:var(--font-mono)]"
            >
              {s}
            </li>
          ))}
        </ul>
      </article>
    </div>
  );
});
