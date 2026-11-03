import { component$ } from "@builder.io/qwik";

interface SectionHeadProps {
  index: string;
  id: string;
  title: string;
  kicker: string;
  note?: string;
}

/** Oversized index number + title, the page's recurring section signature. */
export const SectionHead = component$<SectionHeadProps>((p) => {
  return (
    <div class="grid gap-6 lg:grid-cols-[auto_1fr] lg:items-end">
      <div class="flex items-end gap-4">
        <span
          aria-hidden="true"
          class="font-[family-name:var(--font-mono)] font-bold text-6xl sm:text-8xl leading-[0.8] text-t-accent-ink"
        >
          {p.index}
        </span>
        <div>
          <p class="text-xs font-bold uppercase tracking-[0.25em] text-t-accent-ink">
            {p.kicker}
          </p>
          <h2
            id={p.id}
            class="mt-1 text-4xl sm:text-6xl font-black uppercase tracking-tight leading-[0.9] text-t-bright font-[family-name:var(--font-display)]"
          >
            {p.title}
          </h2>
        </div>
      </div>
      {p.note && (
        <p class="text-sm sm:text-base text-t-muted max-w-md lg:justify-self-end lg:text-right">
          {p.note}
        </p>
      )}
    </div>
  );
});
