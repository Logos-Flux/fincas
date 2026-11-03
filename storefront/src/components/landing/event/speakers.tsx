import { component$ } from "@builder.io/qwik";
import { SPEAKERS, TRACK_DOT } from "./data";
import { SectionHead } from "./section-head";

/**
 * Speaker wall. Each card flips to its talk title on hover or keyboard focus;
 * on touch screens (no hover) the talk is shown underneath instead, so it is
 * never hidden from anyone.
 */
export const Speakers = component$(() => {
  return (
    <section
      id="speakers"
      aria-labelledby="speakers-title"
      class="scroll-mt-20 py-20 sm:py-28"
    >
      <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <SectionHead
          index="01"
          id="speakers-title"
          title="Speakers"
          kicker="41 talks, zero vendor pitches"
          note="Every speaker below ran the system they are talking about. Hover or tab onto a card for the talk."
        />

        <ul class="mt-12 grid grid-cols-2 lg:grid-cols-4 gap-px bg-t-border border border-t-border rounded-2xl overflow-hidden">
          {SPEAKERS.map((s, i) => (
            <li key={s.name} class="bg-t-deep">
              <article
                tabIndex={0}
                aria-label={`${s.name}, ${s.role} at ${s.company}. Talk: ${s.talk}`}
                class="group relative h-full min-h-[13rem] sm:min-h-[15rem] p-4 sm:p-6 flex flex-col outline-none focus-visible:ring-2 focus-visible:ring-t-accent focus-visible:ring-inset overflow-hidden"
              >
                <div class="flex items-start justify-between">
                  <span
                    aria-hidden="true"
                    class={[
                      "grid place-items-center w-12 h-12 sm:w-16 sm:h-16 rounded-full text-base sm:text-xl font-black font-[family-name:var(--font-display)]",
                      i % 3 === 0
                        ? "bg-t-accent text-t-on-accent"
                        : i % 3 === 1
                          ? "bg-t-card text-t-bright border-2 border-t-accent"
                          : "bg-t-hover text-t-bright",
                    ]}
                  >
                    {s.initials}
                  </span>
                  <span class="font-[family-name:var(--font-mono)] text-xs text-t-muted">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                </div>
                <div class="mt-auto pt-6">
                  <h3 class="text-base sm:text-lg font-bold leading-tight text-t-bright font-[family-name:var(--font-display)]">
                    {s.name}
                  </h3>
                  <p class="mt-1 text-sm text-t-text">{s.role}</p>
                  <p class="text-sm text-t-muted">{s.company}</p>
                  <p class="mt-3 text-sm text-t-text leading-snug [@media(hover:hover)]:hidden">
                    “{s.talk}”
                  </p>
                </div>

                {/* Talk reveal (pointer devices). */}
                <div
                  aria-hidden="true"
                  class="hidden [@media(hover:hover)]:flex absolute inset-0 flex-col justify-between p-6 bg-t-accent text-t-on-accent translate-y-full group-hover:translate-y-0 group-focus-visible:translate-y-0 transition-transform duration-300 ease-out motion-reduce:transition-none"
                >
                  <span class="inline-flex items-center gap-2 text-[11px] font-bold uppercase tracking-[0.2em]">
                    <span
                      class={[
                        "w-2 h-2 rounded-full ring-2 ring-current",
                        TRACK_DOT[s.track],
                      ]}
                    />
                    {s.track} track
                  </span>
                  <p class="text-xl font-black leading-tight font-[family-name:var(--font-display)]">
                    “{s.talk}”
                  </p>
                  <span class="text-xs font-semibold">
                    {s.name} · {s.company}
                  </span>
                </div>
              </article>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
});
