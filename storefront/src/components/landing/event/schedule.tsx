import { $, component$, sync$, useSignal } from "@builder.io/qwik";
import { SCHEDULE, TRACK_DOT } from "./data";
import { SectionHead } from "./section-head";

/** Two-day agenda as WAI-ARIA tabs (arrow keys, Home/End) over a timeline. */
export const Schedule = component$(() => {
  const active = useSignal(0);

  const onKey = $((e: KeyboardEvent) => {
    const n = SCHEDULE.length;
    let next = active.value;
    if (e.key === "ArrowRight") next = (active.value + 1) % n;
    else if (e.key === "ArrowLeft") next = (active.value - 1 + n) % n;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = n - 1;
    else return;
    active.value = next;
    document.getElementById(`day-tab-${next}`)?.focus();
  });

  return (
    <section
      id="schedule"
      aria-labelledby="schedule-title"
      class="scroll-mt-20 py-20 sm:py-28 bg-t-surface border-y border-t-border"
    >
      <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <SectionHead
          index="02"
          id="schedule-title"
          title="Schedule"
          kicker="Three stages, one lab"
          note="Talks run 40 minutes with 15 for questions. Workshops are capped at 60 seats and fill first."
        />

        <div class="mt-12 grid gap-10 lg:grid-cols-[18rem_1fr]">
          <div class="space-y-6">
            <div
              role="tablist"
              aria-label="Conference day"
              onKeyDown$={[
                // $ handlers run async, too late to stop the page scrolling.
                sync$((e: KeyboardEvent) => {
                  if (
                    ["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)
                  )
                    e.preventDefault();
                }),
                onKey,
              ]}
              class="grid grid-cols-2 lg:grid-cols-1 gap-2"
            >
              {SCHEDULE.map((d, i) => {
                const on = active.value === i;
                return (
                  <button
                    key={d.label}
                    id={`day-tab-${i}`}
                    type="button"
                    role="tab"
                    aria-selected={on}
                    aria-controls={`day-panel-${i}`}
                    tabIndex={on ? 0 : -1}
                    onClick$={() => (active.value = i)}
                    class={[
                      "text-left rounded-xl border-2 px-5 py-4 transition-colors",
                      on
                        ? "bg-t-accent border-t-accent text-t-on-accent"
                        : "bg-t-card border-t-border text-t-text hover:border-t-border-active",
                    ]}
                  >
                    <span class="block text-2xl sm:text-3xl font-black uppercase leading-none font-[family-name:var(--font-display)]">
                      {d.label}
                    </span>
                    <span
                      class={[
                        "block mt-1.5 text-sm font-semibold",
                        on ? "text-t-on-accent" : "text-t-muted",
                      ]}
                    >
                      {d.date}
                    </span>
                  </button>
                );
              })}
            </div>

            <ul
              class="hidden lg:grid gap-2 text-sm text-t-text"
              aria-label="Track legend"
            >
              {(["Infra", "Platform", "Security", "Data"] as const).map((t) => (
                <li key={t} class="flex items-center gap-2.5">
                  <span
                    aria-hidden="true"
                    class={["w-2.5 h-2.5 rounded-full", TRACK_DOT[t]]}
                  />
                  {t}
                </li>
              ))}
            </ul>
          </div>

          {SCHEDULE.map((d, i) => (
            <div
              key={d.label}
              id={`day-panel-${i}`}
              role="tabpanel"
              aria-labelledby={`day-tab-${i}`}
              hidden={active.value !== i}
              tabIndex={0}
              class="outline-none focus-visible:ring-2 focus-visible:ring-t-accent rounded-xl"
            >
              <ol class="relative">
                <span
                  aria-hidden="true"
                  class="absolute left-[4.25rem] sm:left-[5.75rem] top-2 bottom-2 w-0.5 bg-t-border"
                />
                {d.sessions.map((s) => (
                  <li
                    key={s.time}
                    class="relative grid grid-cols-[4.25rem_1fr] sm:grid-cols-[5.75rem_1fr] group"
                  >
                    <time class="pt-5 pr-3 font-[family-name:var(--font-mono)] text-sm sm:text-lg font-bold text-t-bright tabular-nums">
                      {s.time}
                    </time>
                    <span
                      aria-hidden="true"
                      class={[
                        "absolute left-[4.25rem] sm:left-[5.75rem] top-7 -translate-x-1/2 w-3 h-3 rounded-full ring-4 ring-t-surface",
                        TRACK_DOT[s.track],
                      ]}
                    />
                    <div class="ml-5 sm:ml-8 my-1.5 rounded-xl border border-t-border bg-t-card px-4 sm:px-5 py-4 group-hover:border-t-border-active transition-colors">
                      <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                        <span class="inline-flex items-center gap-1.5 rounded-full border border-t-border px-2 py-0.5 font-semibold text-t-text">
                          <span
                            aria-hidden="true"
                            class={[
                              "w-1.5 h-1.5 rounded-full",
                              TRACK_DOT[s.track],
                            ]}
                          />
                          {s.track}
                        </span>
                        <span class="font-[family-name:var(--font-mono)] uppercase tracking-wider text-t-muted">
                          {s.room}
                        </span>
                      </div>
                      <h3 class="mt-2 text-base sm:text-lg font-bold leading-snug text-t-bright">
                        {s.title}
                      </h3>
                      <p class="text-sm text-t-muted">{s.who}</p>
                    </div>
                  </li>
                ))}
              </ol>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
});
