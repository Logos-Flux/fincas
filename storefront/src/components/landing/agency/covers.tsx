import { component$ } from "@builder.io/qwik";

/**
 * Case-study "covers" for the Fieldwork Studio template. Each one is a small
 * piece of artwork built from divs and inline SVG, coloured only with theme
 * tokens, so every skin repaints it. All covers are decorative: the project
 * name and details are given as real text next to them.
 */

export type CoverId = "furniture" | "clinic" | "press";

/** Parcel & Pine: a browser frame with a product grid. */
const Furniture = component$(() => (
  <div class="absolute inset-0 bg-t-surface overflow-hidden p-[7%] flex items-end">
    <div class="w-full h-[86%] rounded-t-xl border border-t-border bg-t-card shadow-2xl translate-y-[7%] overflow-hidden">
      <div class="h-7 border-b border-t-border flex items-center gap-1.5 px-3">
        <span class="w-2 h-2 rounded-full bg-t-hover" />
        <span class="w-2 h-2 rounded-full bg-t-hover" />
        <span class="w-2 h-2 rounded-full bg-t-hover" />
        <span class="ml-3 h-2.5 w-1/3 rounded-full bg-t-hover" />
      </div>
      <div class="p-4 grid grid-cols-5 gap-3 h-[calc(100%-1.75rem)]">
        <div class="col-span-2 flex flex-col justify-between">
          <div class="space-y-2">
            <div class="h-1.5 w-10 bg-t-accent rounded-full" />
            <p class="font-[family-name:var(--font-display)] text-t-bright text-xl sm:text-2xl font-bold leading-none tracking-tight">
              Parcel
              <br />& Pine
            </p>
          </div>
          <div class="space-y-1.5">
            <div class="h-1.5 w-full rounded-full bg-t-hover" />
            <div class="h-1.5 w-4/5 rounded-full bg-t-hover" />
            <div class="mt-3 h-6 w-20 rounded-md bg-t-accent" />
          </div>
        </div>
        <div class="col-span-3 grid grid-cols-2 gap-2">
          <div class="rounded-md bg-t-accent/25 flex items-end justify-center pb-2">
            <div class="w-2/3 h-1/2 rounded-t-full bg-t-accent" />
          </div>
          <div class="rounded-md bg-t-hover flex items-end justify-center pb-2">
            <div class="w-3/4 h-1/3 rounded-sm bg-t-muted/60" />
          </div>
          <div class="rounded-md bg-t-hover flex items-center justify-center">
            <div class="w-1/2 aspect-square rounded-full border-4 border-t-accent" />
          </div>
          <div class="rounded-md bg-t-accent-alt/30" />
        </div>
      </div>
    </div>
  </div>
));

/** Ostrander Clinic: a booking app in a phone frame. */
const Clinic = component$(() => (
  <div class="absolute inset-0 bg-t-accent-alt/20 overflow-hidden">
    <div class="absolute inset-0 bg-[radial-gradient(circle_at_80%_20%,var(--color-t-accent-glow),transparent_60%)]" />
    <div class="absolute left-1/2 top-[12%] -translate-x-1/2 w-[46%] max-w-[190px] aspect-[9/17] rounded-[1.75rem] border-[6px] border-t-bright bg-t-deep p-3 shadow-2xl">
      <div class="mx-auto h-1.5 w-10 rounded-full bg-t-hover" />
      <p class="mt-4 text-[10px] uppercase tracking-[0.2em] text-t-muted">
        Tue 14 Oct
      </p>
      <p class="mt-1 font-[family-name:var(--font-display)] text-t-bright text-base font-bold leading-tight">
        Pick a time
      </p>
      <div class="mt-3 grid grid-cols-2 gap-1.5 text-[10px] font-semibold">
        {["9:00", "9:40", "10:20", "11:00", "1:30", "2:10"].map((t, i) => (
          <span
            key={t}
            class={[
              "rounded-md py-1.5 text-center",
              i === 2
                ? "bg-t-accent text-t-on-accent"
                : "bg-t-card text-t-text",
            ]}
          >
            {t}
          </span>
        ))}
      </div>
      <div class="mt-3 h-6 rounded-md bg-t-bright/90" />
    </div>
  </div>
));

/** Quarry Press: a stack of book spines. */
const Press = component$(() => (
  <div class="absolute inset-0 bg-t-surface overflow-hidden flex items-end justify-center gap-[2%] px-[10%] pt-[12%]">
    {[
      { h: "h-[78%]", c: "bg-t-accent text-t-on-accent", t: "Salt Year" },
      { h: "h-[66%]", c: "bg-t-bright text-t-deep", t: "Field Notes" },
      { h: "h-[88%]", c: "bg-t-card text-t-bright", t: "Low Light" },
      // No theme defines an on-colour for accent-alt, so this label rides a chip.
      {
        h: "h-[72%]",
        c: "bg-t-accent-alt",
        t: "Quarry",
        l: "bg-t-deep text-t-bright px-0.5 rounded-sm",
      },
      { h: "h-[60%]", c: "bg-t-hover text-t-bright", t: "Index" },
    ].map((b) => (
      <div
        key={b.t}
        class={[
          "w-[16%] border border-t-border rounded-t-sm flex items-start justify-center pt-3",
          b.h,
          b.c,
        ]}
      >
        <span
          class={[
            "[writing-mode:vertical-rl] rotate-180 font-[family-name:var(--font-display)] text-xs sm:text-sm font-bold tracking-wide whitespace-nowrap",
            b.l,
          ]}
        >
          {b.t}
        </span>
      </div>
    ))}
  </div>
));

export const Cover = component$<{ id: CoverId }>(({ id }) => (
  <div aria-hidden="true" class="absolute inset-0">
    {id === "furniture" && <Furniture />}
    {id === "clinic" && <Clinic />}
    {id === "press" && <Press />}
  </div>
));
