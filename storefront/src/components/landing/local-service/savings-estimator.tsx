import { component$, useSignal } from "@builder.io/qwik";

/* Assumptions — kept deliberately simple and shown to the visitor. */
const RATE = 0.17; // $ per kWh, regional residential average
const YIELD = 1350; // kWh produced per installed kW per year
const OFFSET = 0.9; // share of usage the system is sized to cover
const PRICE_PER_W = 2.85; // installed $ per watt, before incentives
const FIXED = 1800; // permits, interconnection, design — same for any size
const ESCALATION = 0.03; // utility price rise per year
const DEGRADATION = 0.005; // panel output loss per year

export interface Estimate {
  kw: number;
  cost: number;
  yearOne: number;
  savings25: number;
  payback: number;
  /** Cumulative spend at years 5, 10, 15, 20, 25. */
  without: number[];
  withSolar: number[];
}

export function estimate(bill: number): Estimate {
  const annualKwh = (bill * 12) / RATE;
  const kw = Math.round(((annualKwh * OFFSET) / YIELD) * 10) / 10;
  const cost = Math.round((kw * 1000 * PRICE_PER_W + FIXED) / 100) * 100;

  let cumWithout = 0;
  let cumWith = cost;
  let cumSavings = 0;
  let payback = 0;
  const without: number[] = [];
  const withSolar: number[] = [];

  for (let y = 1; y <= 25; y++) {
    const utility = bill * 12 * Math.pow(1 + ESCALATION, y - 1);
    const saved = utility * OFFSET * Math.pow(1 - DEGRADATION, y - 1);
    cumWithout += utility;
    cumWith += utility - saved;
    if (!payback && cumSavings + saved >= cost) {
      payback = y - 1 + (cost - cumSavings) / saved;
    }
    cumSavings += saved;
    if (y % 5 === 0) {
      without.push(cumWithout);
      withSolar.push(cumWith);
    }
  }

  return {
    kw,
    cost,
    yearOne: bill * 12 * OFFSET,
    savings25: cumSavings - cost,
    payback: payback || 25,
    without,
    withSolar,
  };
}

const usd = (n: number) =>
  "$" + Math.round(n).toLocaleString("en-US", { maximumFractionDigits: 0 });
const short = (n: number) =>
  n >= 1000 ? `$${Math.round(n / 1000)}k` : `$${Math.round(n)}`;

export const SavingsEstimator = component$(() => {
  const bill = useSignal(165);
  const e = estimate(bill.value);

  // chart geometry
  const W = 520;
  const H = 240;
  const padL = 44;
  const padB = 28;
  const padT = 14;
  const max = Math.max(...e.without) * 1.08;
  const plotH = H - padB - padT;
  const groupW = (W - padL) / 5;
  const barW = Math.min(30, groupW / 3);
  const y = (v: number) => padT + plotH - (v / max) * plotH;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * max);

  return (
    <div class="grid lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] gap-6 lg:gap-8 items-stretch">
      <div class="rounded-xl border border-t-border bg-t-card p-5 sm:p-7 flex flex-col">
        <label
          for="bill-slider"
          class="text-sm font-semibold text-t-bright flex items-baseline justify-between gap-3"
        >
          Your average monthly electric bill
          <output
            for="bill-slider"
            class="text-3xl font-black text-t-bright tabular-nums font-[family-name:var(--font-display)]"
          >
            ${bill.value}
          </output>
        </label>
        <input
          id="bill-slider"
          type="range"
          min={60}
          max={450}
          step={5}
          value={bill.value}
          onInput$={(_, el) => (bill.value = Number(el.value))}
          aria-valuetext={`$${bill.value} per month`}
          class="mt-4 w-full accent-[var(--color-t-accent)] cursor-pointer"
        />
        <div class="mt-1 flex justify-between text-xs text-t-muted tabular-nums">
          <span>$60</span>
          <span>$450</span>
        </div>

        <dl class="mt-6 grid grid-cols-2 rounded-lg overflow-hidden border border-t-border">
          <div class="p-4 border-r border-t-border">
            <dt class="text-xs text-t-muted">System size</dt>
            <dd class="mt-1 text-2xl font-bold text-t-bright tabular-nums">
              {e.kw.toFixed(1)} kW
            </dd>
            <dd class="text-xs text-t-muted">
              ≈ {Math.ceil((e.kw * 1000) / 410)} panels
            </dd>
          </div>
          <div class="p-4">
            <dt class="text-xs text-t-muted">Payback</dt>
            <dd class="mt-1 text-2xl font-bold text-t-bright tabular-nums">
              {e.payback.toFixed(1)} yrs
            </dd>
            <dd class="text-xs text-t-muted">installed ≈ {usd(e.cost)}</dd>
          </div>
          <div class="p-4 col-span-2 border-t border-t-border">
            <dt class="text-xs text-t-muted">
              Estimated 25-year savings, after paying for the system
            </dt>
            <dd class="mt-1 text-3xl sm:text-4xl font-black text-t-success tabular-nums font-[family-name:var(--font-display)]">
              {usd(e.savings25)}
            </dd>
            <dd class="text-xs text-t-muted">
              Year one: about {usd(e.yearOne)} off your bills
            </dd>
          </div>
        </dl>

        <p class="mt-5 text-xs text-t-muted leading-relaxed">
          <span class="font-semibold text-t-text">This is an estimate.</span> It
          assumes ${RATE.toFixed(2)}/kWh power, {YIELD.toLocaleString()} kWh per
          kW per year, a system sized to cover {OFFSET * 100}% of use, $
          {PRICE_PER_W.toFixed(2)}/W installed plus ${FIXED.toLocaleString()}{" "}
          for permits and hookup, before any incentives, {ESCALATION * 100}%
          yearly rate increases and {DEGRADATION * 100}% yearly panel wear. Your
          roof's pitch, shade and your utility's buy-back rules will change the
          real number.
        </p>
      </div>

      <figure class="rounded-xl border border-t-border bg-t-card p-5 sm:p-7 flex flex-col">
        <figcaption class="flex flex-wrap items-end justify-between gap-3">
          <div>
            <p class="text-sm font-semibold text-t-bright">
              Total paid for electricity over time
            </p>
            <p class="text-xs text-t-muted">
              Cumulative, including the system cost up front
            </p>
          </div>
          <div class="flex gap-4 text-xs text-t-text">
            <span class="flex items-center gap-1.5">
              <span
                class="w-3 h-3 rounded-sm bg-t-muted/50"
                aria-hidden="true"
              />
              Utility only
            </span>
            <span class="flex items-center gap-1.5">
              <span class="w-3 h-3 rounded-sm bg-t-accent" aria-hidden="true" />
              With Brightline solar
            </span>
          </div>
        </figcaption>

        <svg
          viewBox={`0 0 ${W} ${H}`}
          class="mt-5 w-full h-auto flex-1"
          role="img"
          aria-label={`Bar chart. After 25 years: ${usd(e.without[4])} with the utility only, ${usd(e.withSolar[4])} with solar.`}
        >
          {ticks.map((t) => (
            <g key={t} class="text-t-border">
              <line
                x1={padL}
                x2={W}
                y1={y(t)}
                y2={y(t)}
                stroke="currentColor"
                stroke-dasharray={t === 0 ? undefined : "3 4"}
              />
              <text
                x={padL - 8}
                y={y(t) + 4}
                text-anchor="end"
                class="fill-t-muted text-[11px] tabular-nums"
              >
                {short(t)}
              </text>
            </g>
          ))}
          {e.without.map((v, i) => {
            const gx = padL + groupW * i + groupW / 2;
            const s = e.withSolar[i];
            return (
              <g key={i}>
                <rect
                  x={gx - barW - 2}
                  y={y(v)}
                  width={barW}
                  height={y(0) - y(v)}
                  rx={3}
                  class="fill-t-muted/50"
                />
                <rect
                  x={gx + 2}
                  y={y(s)}
                  width={barW}
                  height={y(0) - y(s)}
                  rx={3}
                  class="fill-t-accent"
                />
                <text
                  x={gx}
                  y={H - 8}
                  text-anchor="middle"
                  class="fill-t-muted text-[11px]"
                >
                  Year {(i + 1) * 5}
                </text>
              </g>
            );
          })}
        </svg>
      </figure>
    </div>
  );
});
