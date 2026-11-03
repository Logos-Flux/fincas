import { $, component$, sync$, useSignal } from "@builder.io/qwik";
import type { DocumentHead } from "@builder.io/qwik-city";
import { LandingShell } from "../../../components/landing/landing-shell";
import { DuotoneImage } from "../../../components/landing/duotone-image";
import { Phone, type ScreenId } from "../../../components/landing/app/phone";

/* ---------- content ---------- */

const STEPS = [
  {
    n: "01",
    title: "Pin your spots",
    body: "Search 2,300 beaches, breaks and lake entries. Tidepool links each one to its nearest tide gauge and offshore buoy, usually within 6 km.",
  },
  {
    n: "02",
    title: "Set your rules",
    body: "Water above 15°, swell under a meter, rising tide, no rain advisory. Tidepool shades the hours that match all of them in green.",
  },
  {
    n: "03",
    title: "Check in, check out",
    body: "Tap in at the water’s edge. If you haven’t tapped out by your set time, your buddy gets a text with your last location.",
  },
];

const FEATURES: {
  id: ScreenId;
  name: string;
  title: string;
  body: string;
  stat: string;
}[] = [
  {
    id: "tide",
    name: "Tide curve",
    title: "The whole day’s water, in one swipe",
    body: "Drag across the curve to see height, direction and water temperature at any minute. Your swim window is marked right on it.",
    stat: "Gauge data refreshed every 6 minutes",
  },
  {
    id: "swell",
    name: "Swell & wind",
    title: "Know if it’s glassy before you load the car",
    body: "Hour-by-hour swell height, period and wind direction from the nearest buoy, plus a crowd estimate built from other people’s check-ins.",
    stat: "418 offshore buoys, 9 countries",
  },
  {
    id: "quality",
    name: "Water quality",
    title: "Bacteria counts, not guesswork",
    body: "We read county health sampling every morning and flag any spot that had heavy rain in the last 48 hours, before the official advisory lands.",
    stat: "Average 3 h ahead of posted advisories",
  },
  {
    id: "buddy",
    name: "Buddy check-in",
    title: "Someone always knows you went in",
    body: "Start a session at the water’s edge. If the timer runs out, the people you chose get a text and a map pin. No reply needed from them to set it up.",
    stat: "61,000 sessions logged last month",
  },
];

const REVIEWS = [
  {
    i: "RA",
    name: "Rosa Alcántara",
    who: "Year-round ocean swimmer",
    stars: 5,
    text: "I used to check three websites and a webcam before my 6 am swim. Now I open one app, see green, and go. The water-quality flag after the March storms kept me out of the harbor twice.",
  },
  {
    i: "TB",
    name: "Tomasz B.",
    who: "Longboarder",
    stars: 5,
    text: "Crowd estimate is scarily accurate.",
  },
  {
    i: "NL",
    name: "Niamh Lacey",
    who: "Triathlete, open-water coach",
    stars: 5,
    text: "I run a Saturday group of 14. Everyone checks in through Tidepool now and I can see who’s still out from the beach. It replaced a whiteboard and a lot of shouting.",
  },
  {
    i: "JK",
    name: "Jae-won Kim",
    who: "Lake swimmer",
    stars: 4,
    text: "Lake support is newer and a couple of my spots have no temperature sensor yet, but the buddy timer alone is worth the Plus price.",
  },
  {
    i: "MP",
    name: "Marcus Pell",
    who: "Dad of two, beach regular",
    stars: 5,
    text: "We plan rock-pool mornings around the low tide now. My kids think I’m a wizard.",
  },
  {
    i: "SO",
    name: "Sade Okafor",
    who: "Cold-water dipper",
    stars: 5,
    text: "The 17.4° in the app was 17.4° on my thermometer. That’s the review.",
  },
];

const DISTRIBUTION = [
  [5, 84],
  [4, 10],
  [3, 3],
  [2, 1],
  [1, 2],
] as const;

/* ---------- small pieces ---------- */

const Stars = ({ n, size = "w-4 h-4" }: { n: number; size?: string }) => (
  <span
    class="inline-flex gap-0.5"
    role="img"
    aria-label={`${n} out of 5 stars`}
  >
    {[1, 2, 3, 4, 5].map((k) => (
      <svg
        key={k}
        viewBox="0 0 20 20"
        class={[size, k <= n ? "text-t-warning" : "text-t-border-active"]}
        fill="currentColor"
        aria-hidden="true"
      >
        <path d="M10 1.5l2.6 5.4 5.9.8-4.3 4.1 1 5.9L10 14.9l-5.2 2.8 1-5.9L1.5 7.7l5.9-.8z" />
      </svg>
    ))}
  </span>
);

const StoreButtons = ({ center = false }: { center?: boolean }) => (
  <div class={["flex flex-wrap gap-2.5 sm:gap-3", center && "justify-center"]}>
    <a
      href="#download"
      class="inline-flex items-center gap-3 rounded-2xl bg-t-bright px-4 sm:px-5 py-2.5 sm:py-3 text-t-deep hover:opacity-90 transition-opacity"
    >
      <svg
        viewBox="0 0 24 24"
        class="w-6 h-6 sm:w-7 sm:h-7"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        aria-hidden="true"
      >
        <rect x="6" y="2" width="12" height="20" rx="3" />
        <path d="M10.5 18.5h3" stroke-linecap="round" />
      </svg>
      <span class="text-left leading-tight">
        <span class="block text-[11px]">Download on the</span>
        <span class="block text-base sm:text-lg font-semibold -mt-0.5 whitespace-nowrap">
          App Store
        </span>
      </span>
    </a>
    <a
      href="#download"
      class="inline-flex items-center gap-3 rounded-2xl bg-t-bright px-4 sm:px-5 py-2.5 sm:py-3 text-t-deep hover:opacity-90 transition-opacity"
    >
      <svg
        viewBox="0 0 24 24"
        class="w-6 h-6 sm:w-7 sm:h-7"
        fill="currentColor"
        aria-hidden="true"
      >
        <path d="M5 3.2v17.6c0 .8.9 1.3 1.6.9l14.6-8.8c.6-.4.6-1.3 0-1.7L6.6 2.3C5.9 1.9 5 2.4 5 3.2z" />
      </svg>
      <span class="text-left leading-tight">
        <span class="block text-[11px]">Get it on</span>
        <span class="block text-base sm:text-lg font-semibold -mt-0.5 whitespace-nowrap">
          Play Store
        </span>
      </span>
    </a>
  </div>
);

/** A wave edge between bands; fill picks up the next band's token colour. */
const Wave = ({ tone, flip = false }: { tone: string; flip?: boolean }) => (
  <svg
    viewBox="0 0 1440 80"
    preserveAspectRatio="none"
    class={["block w-full h-10 sm:h-16", tone, flip && "rotate-180"]}
    fill="currentColor"
    aria-hidden="true"
  >
    <path d="M0 48c120-26 240-40 360-26s240 52 360 52 240-44 360-52 240 18 360 30v28H0z" />
  </svg>
);

/* ---------- page ---------- */

export default component$(() => {
  const active = useSignal<ScreenId>("tide");
  const phone = useSignal("");
  const phoneError = useSignal("");
  const sent = useSignal(false);

  const current = FEATURES.find((f) => f.id === active.value) ?? FEATURES[0];

  const onTabKey = $((e: KeyboardEvent) => {
    const idx = FEATURES.findIndex((f) => f.id === active.value);
    let next = idx;
    if (e.key === "ArrowDown" || e.key === "ArrowRight")
      next = (idx + 1) % FEATURES.length;
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft")
      next = (idx - 1 + FEATURES.length) % FEATURES.length;
    else return;
    active.value = FEATURES[next].id;
    document.getElementById(`tab-${FEATURES[next].id}`)?.focus();
  });

  const submit = $(() => {
    const digits = phone.value.replace(/\D/g, "");
    if (digits.length < 10 || digits.length > 15) {
      phoneError.value = "Enter a mobile number with 10 to 15 digits.";
      return;
    }
    phoneError.value = "";
    sent.value = true;
  });

  return (
    <LandingShell
      brand="Tidepool"
      mark="T"
      links={[
        { label: "How it works", href: "#how" },
        { label: "Features", href: "#features" },
        { label: "Reviews", href: "#reviews" },
        { label: "Download", href: "#download" },
      ]}
      cta={{ label: "Get the app", href: "#download" }}
      footer={{
        App: ["Tide curve", "Swell & wind", "Water quality", "Buddy check-in"],
        Tidepool: ["Our data sources", "Spot requests", "Press kit"],
        Help: ["Support", "Privacy", "Status"],
      }}
      tagline="Tide, swell, temperature and water quality for 2,300 swim and surf spots, plus a check-in timer so someone always knows you went in."
    >
      {/* ---------- hero ---------- */}
      <section
        aria-labelledby="hero-title"
        class="relative overflow-hidden bg-[radial-gradient(60%_55%_at_78%_45%,var(--color-t-accent-glow),transparent_70%)]"
      >
        <div class="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 pt-14 pb-20 lg:pt-20 lg:pb-28 grid lg:grid-cols-[1.1fr_1fr] gap-14 items-center">
          <div>
            <p class="inline-flex items-center gap-2 rounded-full border border-t-border bg-t-card px-3 py-1 text-xs font-semibold text-t-text">
              <span class="w-1.5 h-1.5 rounded-full bg-t-success" />
              Now covering 112 lakes, too
            </p>
            <h1
              id="hero-title"
              class="mt-6 text-[2.75rem] sm:text-6xl lg:text-[4.1rem] font-bold leading-[0.98] tracking-tight text-t-bright font-[family-name:var(--font-display)]"
            >
              Know the water
              <br />
              <span class="text-t-accent-ink">before you’re in it.</span>
            </h1>
            <p class="mt-6 max-w-lg text-lg leading-relaxed text-t-text">
              Tidepool puts tide height, swell, water temperature and this
              morning’s bacteria sampling for your beach on one screen, and
              texts your swim buddy if you don’t check out.
            </p>
            <div class="mt-8">
              <StoreButtons />
            </div>
            <div class="mt-6 flex flex-wrap items-center gap-x-5 gap-y-2 text-sm text-t-muted">
              <span class="inline-flex items-center gap-2">
                <Stars n={5} />
                <span>
                  <strong class="text-t-bright">4.8</strong> from 12,416 ratings
                </span>
              </span>
              <span>Free · Plus is $3.99/mo</span>
            </div>
          </div>

          <div class="relative flex justify-center lg:justify-end">
            <figure class="hidden sm:block absolute left-1/2 lg:left-auto lg:right-[12rem] top-1/2 -translate-x-[95%] lg:translate-x-0 -translate-y-[54%] w-[280px] lg:w-[260px] -rotate-[4deg]">
              <DuotoneImage
                src="/landing/app/hero-swimmer.webp"
                alt="A swimmer in a white cap wading out from dark rocks into a calm, misty sea at dawn"
                width={768}
                height={1024}
                loading="eager"
                position="40% 50%"
                class="aspect-[3/4] rounded-[2rem] border border-t-border shadow-xl"
              />
            </figure>
            <div class="relative lg:rotate-[3deg] lg:mr-10">
              <Phone
                screen="tide"
                label="Tidepool app showing a rising tide of 1.42 meters at Cove Rock, water 17.4 degrees, and a green swim window from 3:40 to 5:10 pm"
              />
              <div class="hidden sm:flex absolute -left-44 top-[15rem] items-center gap-2 rounded-2xl border border-t-border bg-t-deep px-3.5 py-2.5 shadow-lg -rotate-[3deg]">
                <span class="grid place-items-center w-8 h-8 rounded-full bg-t-accent text-t-on-accent text-xs font-bold">
                  17°
                </span>
                <span class="text-xs leading-tight">
                  <span class="block font-bold text-t-bright">
                    Warmest this year
                  </span>
                  <span class="text-t-muted">Cove Rock, 7:10 am</span>
                </span>
              </div>
              <div class="hidden sm:flex absolute -right-28 bottom-14 items-center gap-2 rounded-2xl border border-t-border bg-t-deep px-3.5 py-2.5 shadow-lg -rotate-[3deg]">
                <span class="w-2.5 h-2.5 rounded-full bg-t-success" />
                <span class="text-xs leading-tight">
                  <span class="block font-bold text-t-bright">
                    Maya checked out
                  </span>
                  <span class="text-t-muted">42 min swim · 1.6 km</span>
                </span>
              </div>
            </div>
          </div>
        </div>
        <Wave tone="text-t-surface" />
      </section>

      {/* ---------- how it works ---------- */}
      <section
        id="how"
        aria-labelledby="how-title"
        class="bg-t-surface scroll-mt-16"
      >
        <div class="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-16 lg:py-24">
          <div class="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
            <h2
              id="how-title"
              class="text-3xl sm:text-4xl font-bold tracking-tight text-t-bright font-[family-name:var(--font-display)] max-w-md"
            >
              Three taps between you and the water.
            </h2>
            <p class="max-w-sm text-t-muted">
              Setup takes about two minutes. After that, opening the app is the
              whole routine.
            </p>
          </div>

          <div class="relative mt-14">
            <svg
              viewBox="0 0 1000 200"
              preserveAspectRatio="none"
              class="hidden md:block absolute inset-x-0 top-6 h-56 w-full text-t-border-active"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-dasharray="6 8"
              aria-hidden="true"
            >
              <path d="M60 20 C 220 20, 260 110, 420 110 S 640 200, 860 200" />
            </svg>
            <ol class="relative grid gap-6 md:grid-cols-3">
              {STEPS.map((s, i) => (
                <li
                  key={s.n}
                  class={[
                    "rounded-3xl border border-t-border bg-t-card p-6",
                    i === 1 && "md:mt-20",
                    i === 2 && "md:mt-40",
                  ]}
                >
                  <span class="grid place-items-center w-12 h-12 rounded-2xl bg-t-accent text-t-on-accent text-lg font-bold font-[family-name:var(--font-mono)]">
                    {s.n}
                  </span>
                  <h3 class="mt-5 text-xl font-bold text-t-bright">
                    {s.title}
                  </h3>
                  <p class="mt-2 leading-relaxed text-t-text">{s.body}</p>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </section>

      {/* ---------- photo band ---------- */}
      <figure class="relative h-[420px] sm:h-[500px] bg-t-deep">
        <div class="absolute inset-0">
          <DuotoneImage
            src="/landing/app/surf-lineup.webp"
            alt="Four surfers sitting on their boards in the lineup, facing the sunrise"
            width={1440}
            height={800}
            position="50% 55%"
            class="h-full w-full"
          />
        </div>
        <div
          aria-hidden="true"
          class="absolute inset-x-0 top-0 h-24 bg-gradient-to-b from-t-surface to-transparent"
        />
        <div
          aria-hidden="true"
          class="absolute inset-x-0 bottom-0 h-40 bg-gradient-to-t from-t-deep to-transparent"
        />
        <figcaption class="absolute inset-x-0 bottom-6 sm:bottom-10">
          <div class="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="max-w-sm rounded-2xl border border-t-border bg-t-deep p-5 shadow-xl">
              <p class="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.14em] text-t-muted">
                <span class="w-2 h-2 rounded-full bg-t-success" />
                Pelican Point · 6:42 am
              </p>
              <p class="mt-2 text-t-bright font-semibold leading-snug">
                1.1 m at 12 s, light offshore, four out. Tidepool called it at
                5:30 the night before.
              </p>
            </div>
          </div>
        </figcaption>
      </figure>

      {/* ---------- feature tour ---------- */}
      <section
        id="features"
        aria-labelledby="features-title"
        class="scroll-mt-16"
      >
        <div class="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-16 lg:py-24 grid lg:grid-cols-[1fr_auto] gap-12 lg:gap-20 items-center">
          <div>
            <p class="text-sm font-semibold uppercase tracking-[0.18em] text-t-accent-ink">
              Feature tour
            </p>
            <h2
              id="features-title"
              class="mt-3 text-3xl sm:text-4xl font-bold tracking-tight text-t-bright font-[family-name:var(--font-display)]"
            >
              Four screens. Everything you check before a swim.
            </h2>

            <div
              role="tablist"
              aria-label="Tidepool features"
              aria-orientation="vertical"
              class="mt-8 space-y-2"
            >
              {FEATURES.map((f) => {
                const selected = f.id === active.value;
                return (
                  <button
                    key={f.id}
                    id={`tab-${f.id}`}
                    type="button"
                    role="tab"
                    aria-selected={selected}
                    aria-controls="feature-panel"
                    tabIndex={selected ? 0 : -1}
                    onClick$={() => (active.value = f.id)}
                    onKeyDown$={[
                      // $ handlers run async, too late to stop the page scrolling.
                      sync$((e: KeyboardEvent) => {
                        if (e.key.startsWith("Arrow")) e.preventDefault();
                      }),
                      onTabKey,
                    ]}
                    class={[
                      "w-full text-left rounded-2xl border px-5 py-4 transition-colors",
                      selected
                        ? "border-t-border-active bg-t-card"
                        : "border-transparent hover:bg-t-surface",
                    ]}
                  >
                    <span class="flex items-center justify-between gap-4">
                      <span
                        class={[
                          "text-lg font-bold",
                          selected ? "text-t-bright" : "text-t-text",
                        ]}
                      >
                        {f.name}
                      </span>
                      <span
                        aria-hidden="true"
                        class={[
                          "h-2 rounded-full transition-all",
                          selected
                            ? "w-8 bg-t-accent"
                            : "w-2 bg-t-border-active",
                        ]}
                      />
                    </span>
                    {selected && (
                      <span class="block mt-2">
                        <span class="block font-semibold text-t-bright">
                          {f.title}
                        </span>
                        <span class="block mt-1 leading-relaxed text-t-text">
                          {f.body}
                        </span>
                        <span class="block mt-3 text-sm font-semibold text-t-accent-ink">
                          {f.stat}
                        </span>
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>

          <div
            id="feature-panel"
            role="tabpanel"
            aria-labelledby={`tab-${current.id}`}
            class="relative flex justify-center"
          >
            <div
              aria-hidden="true"
              class="absolute inset-x-6 top-16 bottom-16 rounded-[3rem] bg-t-accent/15 -rotate-6"
            />
            <div class="relative">
              <Phone
                screen={current.id}
                label={`Tidepool ${current.name} screen: ${current.title}`}
              />
            </div>
          </div>
        </div>
      </section>

      {/* ---------- reviews ---------- */}
      <section
        id="reviews"
        aria-labelledby="reviews-title"
        class="bg-t-surface border-y border-t-border scroll-mt-16"
      >
        <div class="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-16 lg:py-24 grid lg:grid-cols-[300px_1fr] gap-12">
          <div class="lg:sticky lg:top-24 self-start">
            <h2
              id="reviews-title"
              class="text-3xl font-bold tracking-tight text-t-bright font-[family-name:var(--font-display)]"
            >
              Swimmers keep it on their home screen.
            </h2>
            <div class="mt-8 flex items-end gap-4">
              <span class="text-7xl font-bold leading-none text-t-bright font-[family-name:var(--font-display)]">
                4.8
              </span>
              <span class="pb-1.5">
                <Stars n={5} size="w-5 h-5" />
                <span class="block mt-1 text-sm text-t-muted">
                  12,416 ratings
                </span>
              </span>
            </div>
            <dl class="mt-6 space-y-1.5">
              {DISTRIBUTION.map(([star, pct]) => (
                <div key={star} class="flex items-center gap-3 text-sm">
                  <dt class="w-10 text-t-muted">{star} star</dt>
                  <dd class="flex-1 flex items-center gap-3">
                    <span class="flex-1 h-2 rounded-full bg-t-hover overflow-hidden">
                      <span
                        class="block h-full rounded-full bg-t-accent"
                        style={{ width: `${pct}%` }}
                      />
                    </span>
                    <span class="w-9 text-right tabular-nums text-t-text">
                      {pct}%
                    </span>
                  </dd>
                </div>
              ))}
            </dl>
            <p class="mt-6 text-sm text-t-muted">
              Featured in Outdoors &amp; Fitness, May 2026.
            </p>
          </div>

          <ul class="columns-1 sm:columns-2 gap-5 [&>li]:mb-5">
            {REVIEWS.map((r, i) => (
              <li
                key={r.name}
                class={[
                  "break-inside-avoid rounded-3xl border border-t-border p-6",
                  i % 3 === 0 ? "bg-t-card" : "bg-t-deep",
                ]}
              >
                <Stars n={r.stars} />
                <p
                  class={[
                    "mt-3 leading-relaxed text-t-bright",
                    r.text.length < 60
                      ? "text-2xl font-bold font-[family-name:var(--font-display)]"
                      : "text-base",
                  ]}
                >
                  “{r.text}”
                </p>
                <div class="mt-5 flex items-center gap-3">
                  <span class="grid place-items-center w-10 h-10 rounded-full bg-t-accent/20 text-sm font-bold text-t-bright">
                    {r.i}
                  </span>
                  <span class="leading-tight">
                    <span class="block font-semibold text-t-bright">
                      {r.name}
                    </span>
                    <span class="text-sm text-t-muted">{r.who}</span>
                  </span>
                </div>
              </li>
            ))}
          </ul>
        </div>
      </section>

      {/* ---------- download ---------- */}
      <section
        id="download"
        aria-labelledby="download-title"
        class="scroll-mt-16"
      >
        <div class="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-16 lg:py-24">
          <div class="relative isolate overflow-hidden rounded-[2.5rem] bg-t-card border border-t-border px-6 py-12 sm:px-12 lg:px-16 lg:py-16">
            <div class="absolute inset-0 -z-10">
              <DuotoneImage
                src="/landing/app/lake-dock.webp"
                alt=""
                width={1440}
                height={800}
                position="50% 60%"
                class="h-full w-full"
              />
            </div>
            <div
              aria-hidden="true"
              class="absolute inset-0 -z-10 bg-t-deep/90 lg:bg-transparent lg:bg-gradient-to-r lg:from-t-deep lg:via-t-deep/90 lg:to-t-deep/20"
            />
            <div class="grid lg:grid-cols-[1.3fr_1fr] gap-10 items-center">
              <div>
                <h2
                  id="download-title"
                  class="text-4xl sm:text-5xl font-bold tracking-tight leading-[1.02] text-t-bright font-[family-name:var(--font-display)]"
                >
                  High tide is at 4:52 today.
                  <br />
                  <span class="text-t-accent-ink">Be there for it.</span>
                </h2>
                <p class="mt-5 max-w-md text-lg text-t-text">
                  Free on iPhone and Android. Plus adds buddy check-in, lake
                  sensors and a 10-day forecast for $3.99 a month.
                </p>
                <div class="mt-8">
                  <StoreButtons />
                </div>
              </div>

              <div class="rounded-3xl bg-t-deep border border-t-border p-6">
                {sent.value ? (
                  <div role="status" class="text-center py-4">
                    <span class="mx-auto grid place-items-center w-12 h-12 rounded-full bg-t-success/15 text-t-success">
                      <svg
                        viewBox="0 0 20 20"
                        class="w-6 h-6"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="2.2"
                        aria-hidden="true"
                      >
                        <path
                          d="M4 10.5l4 4 8-9"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                        />
                      </svg>
                    </span>
                    <p class="mt-4 text-lg font-bold text-t-bright">
                      Link on its way
                    </p>
                    <p class="mt-1 text-sm text-t-muted">
                      Check your messages on {phone.value}. Open it on your
                      phone to install.
                    </p>
                    <button
                      type="button"
                      onClick$={() => {
                        sent.value = false;
                        phone.value = "";
                      }}
                      class="mt-4 text-sm font-semibold text-t-accent-ink hover:underline"
                    >
                      Send to another number
                    </button>
                  </div>
                ) : (
                  <form preventdefault:submit onSubmit$={submit} noValidate>
                    <p class="font-bold text-t-bright">On a laptop?</p>
                    <p class="mt-1 text-sm text-t-muted">
                      We’ll text you a download link. One message, no list.
                    </p>
                    <label
                      for="tp-phone"
                      class="mt-5 block text-sm font-semibold text-t-text"
                    >
                      Mobile number
                    </label>
                    <div class="mt-2 flex flex-col sm:flex-row gap-2">
                      <input
                        id="tp-phone"
                        type="tel"
                        autocomplete="tel"
                        inputMode="tel"
                        placeholder="(555) 014-2290"
                        value={phone.value}
                        onInput$={(_, el) => (phone.value = el.value)}
                        aria-invalid={phoneError.value ? "true" : undefined}
                        aria-describedby={
                          phoneError.value ? "tp-phone-err" : undefined
                        }
                        class="flex-1 min-w-0 rounded-xl border border-t-border bg-t-card px-4 py-3 text-t-bright placeholder:text-t-muted focus:outline-none focus:border-t-border-active"
                      />
                      <button
                        type="submit"
                        class="rounded-xl bg-t-accent px-5 py-3 font-semibold text-t-on-accent hover:opacity-90 transition-opacity"
                      >
                        Text me
                      </button>
                    </div>
                    {phoneError.value && (
                      <p id="tp-phone-err" class="mt-2 text-sm text-t-error">
                        {phoneError.value}
                      </p>
                    )}
                  </form>
                )}
              </div>
            </div>
          </div>
        </div>
      </section>
    </LandingShell>
  );
});

export const head: DocumentHead = {
  title: "Tidepool — Mobile App Launch template",
  meta: [
    {
      name: "description",
      content:
        "Tidepool: tide, swell, water temperature and water-quality readings for 2,300 swim and surf spots, with a buddy check-in timer. A Fincas storefront landing template.",
    },
  ],
};
