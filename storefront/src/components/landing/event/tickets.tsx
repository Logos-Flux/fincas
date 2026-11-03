import { component$, useSignal } from "@builder.io/qwik";
import { SectionHead } from "./section-head";

const TEAM_PACK = 2195;
const eur = (n: number) => `€${n.toLocaleString("en-GB")}`;

const SHARED = [
  "Both days, all three stages",
  "Lunch, coffee and the harbor social",
  "Talk recordings within 72 hours",
];

/** Three tiers; the team tier has a pack stepper that drives its total. */
export const Tickets = component$(() => {
  const packs = useSignal(1);

  return (
    <section
      id="tickets"
      aria-labelledby="tickets-title"
      class="scroll-mt-20 py-20 sm:py-28 bg-t-surface border-t border-t-border"
    >
      <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <SectionHead
          index="04"
          id="tickets-title"
          title="Tickets"
          kicker="1,800 seats · 1,214 gone"
          note="Prices in euros, excluding 21% Dutch VAT. Full refund until 1 March 2027, name changes free any time."
        />

        <div class="mt-12 grid gap-5 lg:grid-cols-3 lg:items-stretch">
          {/* Early bird — sold out */}
          <article class="relative rounded-2xl border border-t-border bg-t-card p-7 flex flex-col">
            <div class="flex items-center justify-between">
              <h3 class="text-xl font-black uppercase font-[family-name:var(--font-display)] text-t-bright">
                Early bird
              </h3>
              <span class="rounded-full border border-t-border px-2.5 py-1 text-[11px] font-bold uppercase tracking-wider text-t-muted">
                Sold out
              </span>
            </div>
            <p class="mt-6 flex items-baseline gap-2">
              <span class="text-5xl font-black text-t-muted line-through decoration-2 font-[family-name:var(--font-display)]">
                €349
              </span>
              <span class="text-sm text-t-muted">per person</span>
            </p>
            <p class="mt-3 text-sm text-t-muted">
              All 400 went in 9 days. Join the alumni list and you get first
              call for 2028.
            </p>
            <ul class="mt-6 space-y-2.5 text-sm text-t-muted flex-1">
              {SHARED.map((f) => (
                <li key={f} class="flex gap-2.5">
                  <span aria-hidden="true">—</span>
                  {f}
                </li>
              ))}
            </ul>
            <button
              type="button"
              disabled
              class="mt-8 w-full rounded-xl border border-t-border py-3 text-sm font-semibold text-t-muted cursor-not-allowed"
            >
              Sold out
            </button>
          </article>

          {/* Standard */}
          <article class="relative rounded-2xl border-2 border-t-accent bg-t-card p-7 flex flex-col lg:-my-4 shadow-[0_0_0_6px_var(--color-t-accent-glow)]">
            <div class="flex items-center justify-between">
              <h3 class="text-xl font-black uppercase font-[family-name:var(--font-display)] text-t-bright">
                Standard
              </h3>
              <span class="rounded-full bg-t-accent px-2.5 py-1 text-[11px] font-bold uppercase tracking-wider text-t-on-accent">
                586 left
              </span>
            </div>
            <p class="mt-6 flex items-baseline gap-2">
              <span class="text-6xl font-black text-t-bright font-[family-name:var(--font-display)]">
                €549
              </span>
              <span class="text-sm text-t-muted">per person</span>
            </p>
            <p class="mt-3 text-sm text-t-text">
              Rises to €649 on 1 February 2027. Includes a seat at one workshop
              of your choice.
            </p>
            <ul class="mt-6 space-y-2.5 text-sm text-t-text flex-1">
              {[...SHARED, "One reserved workshop seat"].map((f) => (
                <li key={f} class="flex gap-2.5">
                  <span aria-hidden="true" class="text-t-accent-ink font-bold">
                    +
                  </span>
                  {f}
                </li>
              ))}
            </ul>
            <a
              href="#tickets"
              class="mt-8 block w-full rounded-xl bg-t-accent py-3.5 text-center text-sm font-bold uppercase tracking-wider text-t-on-accent hover:opacity-90 transition-opacity"
            >
              Get a standard pass
            </a>
          </article>

          {/* Team of 5 — with stepper */}
          <article class="relative rounded-2xl border border-t-border bg-t-card p-7 flex flex-col">
            <div class="flex items-center justify-between">
              <h3 class="text-xl font-black uppercase font-[family-name:var(--font-display)] text-t-bright">
                Team of 5
              </h3>
              <span class="rounded-full border border-t-border px-2.5 py-1 text-[11px] font-bold uppercase tracking-wider text-t-text">
                Save 20%
              </span>
            </div>
            <p class="mt-6 flex items-baseline gap-2">
              <span class="text-5xl font-black text-t-bright font-[family-name:var(--font-display)]">
                {eur(TEAM_PACK)}
              </span>
              <span class="text-sm text-t-muted">per pack</span>
            </p>
            <p class="mt-3 text-sm text-t-text">
              Five transferable passes, {eur(TEAM_PACK / 5)} each. Names can be
              added up to the morning of day one.
            </p>

            <div class="mt-6 rounded-xl border border-t-border bg-t-deep p-4 flex-1">
              <div class="flex items-center justify-between gap-3">
                <span
                  id="team-packs-label"
                  class="text-sm font-semibold text-t-bright"
                >
                  Packs
                </span>
                <div
                  role="group"
                  aria-labelledby="team-packs-label"
                  class="inline-flex items-center rounded-lg border border-t-border"
                >
                  <button
                    type="button"
                    aria-label="Remove a pack"
                    disabled={packs.value <= 1}
                    onClick$={() =>
                      (packs.value = Math.max(1, packs.value - 1))
                    }
                    class="w-10 h-10 grid place-items-center text-lg font-bold text-t-bright hover:bg-t-hover rounded-l-lg disabled:text-t-muted disabled:hover:bg-transparent"
                  >
                    −
                  </button>
                  <output
                    aria-live="polite"
                    class="w-10 text-center font-[family-name:var(--font-mono)] font-bold tabular-nums text-t-bright"
                  >
                    {packs.value}
                  </output>
                  <button
                    type="button"
                    aria-label="Add a pack"
                    disabled={packs.value >= 10}
                    onClick$={() =>
                      (packs.value = Math.min(10, packs.value + 1))
                    }
                    class="w-10 h-10 grid place-items-center text-lg font-bold text-t-bright hover:bg-t-hover rounded-r-lg disabled:text-t-muted disabled:hover:bg-transparent"
                  >
                    +
                  </button>
                </div>
              </div>
              <dl class="mt-4 space-y-1.5 text-sm">
                <div class="flex justify-between">
                  <dt class="text-t-muted">Passes</dt>
                  <dd class="font-[family-name:var(--font-mono)] text-t-text">
                    {packs.value * 5}
                  </dd>
                </div>
                <div class="flex justify-between">
                  <dt class="text-t-muted">vs. standard</dt>
                  <dd class="font-[family-name:var(--font-mono)] text-t-text">
                    −{eur(packs.value * (549 * 5 - TEAM_PACK))}
                  </dd>
                </div>
                <div class="flex justify-between items-baseline border-t border-t-border pt-2 mt-2">
                  <dt class="font-semibold text-t-bright">Total</dt>
                  <dd
                    aria-live="polite"
                    class="text-2xl font-black text-t-bright font-[family-name:var(--font-display)] tabular-nums"
                  >
                    {eur(packs.value * TEAM_PACK)}
                  </dd>
                </div>
              </dl>
            </div>
            <a
              href="#tickets"
              class="mt-6 block w-full rounded-xl border-2 border-t-bright py-3 text-center text-sm font-bold uppercase tracking-wider text-t-bright hover:bg-t-hover transition-colors"
            >
              Book {packs.value * 5} passes
            </a>
          </article>
        </div>
      </div>
    </section>
  );
});
