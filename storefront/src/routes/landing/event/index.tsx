import { component$ } from "@builder.io/qwik";
import type { DocumentHead } from "@builder.io/qwik-city";
import { LandingShell } from "../../../components/landing/landing-shell";
import { Countdown } from "../../../components/landing/event/countdown";
import { Speakers } from "../../../components/landing/event/speakers";
import { Schedule } from "../../../components/landing/event/schedule";
import { Tickets } from "../../../components/landing/event/tickets";
import { SectionHead } from "../../../components/landing/event/section-head";
import { DuotoneImage } from "../../../components/landing/duotone-image";

const TOPICS = [
  "On-call that sleeps",
  "Postgres in anger",
  "Edge networking",
  "Supply-chain security",
  "Platform teams",
  "Cost per request",
  "Chaos drills",
  "Tracing at scale",
];

const STATS = [
  { value: "2", label: "days" },
  { value: "3", label: "stages + 1 lab" },
  { value: "41", label: "talks" },
  { value: "1,800", label: "engineers" },
];

/** Square-wave + sine trace across the hero — the "signal" in the name. */
const Waveform = () => (
  <svg
    aria-hidden="true"
    viewBox="0 0 1200 120"
    preserveAspectRatio="none"
    class="w-full h-16 sm:h-24 text-t-accent"
    fill="none"
    stroke="currentColor"
  >
    <path
      stroke-width="3"
      d="M0 60 H120 V20 H180 V100 H240 V20 H300 V100 H360 V60 C 400 60 410 10 450 10 S 500 110 540 110 S 590 10 630 10 S 680 110 720 110 S 770 60 800 60 H860 V30 H900 V90 H940 V30 H980 V90 H1020 V60 H1200"
    />
    <path
      class="text-t-border-active"
      stroke="currentColor"
      stroke-width="1"
      stroke-dasharray="4 8"
      d="M0 60 H1200"
    />
  </svg>
);

const VenueMap = () => (
  <svg
    aria-hidden="true"
    viewBox="0 0 480 360"
    class="w-full h-auto text-t-muted"
  >
    {/* streets */}
    <g stroke="currentColor" stroke-opacity="0.35" stroke-width="2">
      <path d="M0 40 H480 M0 100 H480 M0 300 H480 M0 340 H480" />
      <path d="M60 0 V360 M150 0 V140 M150 250 V360 M260 0 V140 M330 250 V360 M420 0 V360" />
    </g>
    {/* river */}
    <path
      class="text-t-accent"
      fill="currentColor"
      fill-opacity="0.16"
      d="M0 150 C 120 130 200 190 300 170 S 420 140 480 160 V 240 C 420 222 360 250 300 248 S 120 228 0 236 Z"
    />
    <path
      class="text-t-accent"
      stroke="currentColor"
      stroke-opacity="0.5"
      stroke-width="1.5"
      fill="none"
      d="M0 193 C 120 180 200 220 300 210 S 420 192 480 200"
      stroke-dasharray="6 6"
    />
    {/* bridge */}
    <path
      class="text-t-text"
      stroke="currentColor"
      stroke-width="5"
      d="M210 120 L 250 270"
    />
    {/* metro line */}
    <path
      class="text-t-info"
      stroke="currentColor"
      stroke-width="4"
      fill="none"
      d="M60 0 V 100 H 260 V 120 L 300 270 H 420 V 360"
    />
    <circle cx="60" cy="40" r="8" class="text-t-info" fill="currentColor" />
    <circle cx="420" cy="300" r="8" class="text-t-info" fill="currentColor" />
    {/* venue */}
    <rect
      x="340"
      y="262"
      width="56"
      height="30"
      rx="4"
      class="text-t-accent"
      fill="currentColor"
    />
    <circle
      cx="368"
      cy="277"
      r="34"
      class="text-t-accent"
      stroke="currentColor"
      stroke-width="2"
      fill="none"
      stroke-dasharray="3 5"
    />
    <g
      class="text-t-bright"
      fill="currentColor"
      font-size="13"
      font-weight="700"
      font-family="var(--font-mono)"
    >
      <text x="74" y="30">
        CENTRAAL
      </text>
      <text x="270" y="330">
        RIJNHAVEN M
      </text>
      <text x="300" y="250">
        HALVE MAAN WERF
      </text>
      <text x="24" y="210" font-weight="400" fill-opacity="0.8">
        NIEUWE MAAS
      </text>
    </g>
  </svg>
);

const TRAVEL = [
  {
    mode: "Train",
    note: "Rotterdam Centraal is 27 minutes from Schiphol and 2h 40m from Paris by high-speed rail. From there, metro line D to Rijnhaven: 6 minutes, then a 4-minute walk along the quay.",
  },
  {
    mode: "Air",
    note: "Rotterdam The Hague airport has 31 direct routes; bus 33 + metro gets you to the door in 35 minutes. Most attendees fly into Schiphol.",
  },
  {
    mode: "Stay",
    note: "We hold 420 rooms across three hotels within 900 m at €139–€189 a night until 1 March. The code arrives with your ticket.",
  },
  {
    mode: "Access",
    note: "Step-free throughout, induction loops in all three stages, live captions on every talk, and a quiet room on the mezzanine.",
  },
];

export default component$(() => {
  return (
    <LandingShell
      brand="Signal Summit"
      mark="S/"
      links={[
        { label: "Speakers", href: "#speakers" },
        { label: "Schedule", href: "#schedule" },
        { label: "Venue", href: "#venue" },
        { label: "Tickets", href: "#tickets" },
      ]}
      cta={{ label: "Get tickets", href: "#tickets" }}
      tagline="Two days of infrastructure talks from the people who carry the pager. Rotterdam, 14–15 April 2027."
      footer={{
        Conference: ["Speakers", "Schedule", "Workshops", "Code of conduct"],
        Attend: ["Tickets", "Travel", "Hotels", "Diversity scholarships"],
        Organizers: ["Call for papers", "Sponsor", "Volunteer", "Contact"],
      }}
    >
      {/* ── Hero ─────────────────────────────────────────────── */}
      <section
        aria-labelledby="hero-title"
        class="relative overflow-hidden border-b border-t-border"
      >
        <div
          aria-hidden="true"
          class="absolute inset-0 opacity-60"
          style={{
            backgroundImage:
              "linear-gradient(var(--color-t-border) 1px, transparent 1px), linear-gradient(90deg, var(--color-t-border) 1px, transparent 1px)",
            backgroundSize: "48px 48px",
          }}
        />
        <div
          aria-hidden="true"
          class="absolute -top-40 -right-40 w-[42rem] h-[42rem] rounded-full bg-[radial-gradient(closest-side,var(--color-t-accent-glow),transparent)]"
        />

        <div class="relative max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pt-12 sm:pt-20 pb-8">
          <div class="flex flex-wrap items-center gap-x-4 gap-y-2 font-[family-name:var(--font-mono)] text-xs sm:text-sm font-bold uppercase tracking-[0.2em]">
            <span class="inline-flex items-center gap-2 rounded-full bg-t-accent text-t-on-accent px-3 py-1">
              <span
                aria-hidden="true"
                class="w-2 h-2 rounded-full bg-t-on-accent motion-safe:animate-pulse"
              />
              Edition 04
            </span>
            <span class="text-t-text">14–15 April 2027</span>
            <span aria-hidden="true" class="text-t-muted">
              /
            </span>
            <span class="text-t-text">Rotterdam, NL</span>
          </div>

          <div class="mt-8 grid gap-10 lg:grid-cols-[1.35fr_1fr] lg:items-end">
            <div>
              <h1
                id="hero-title"
                class="font-[family-name:var(--font-display)] font-black uppercase leading-[0.82] tracking-tighter text-t-bright text-[clamp(3.4rem,13vw,9.5rem)]"
              >
                Signal
                <br />
                Summit
                <span
                  class="ml-3 align-top text-[0.45em] text-transparent"
                  style={{ WebkitTextStroke: "2px var(--color-t-accent)" }}
                >
                  ’27
                </span>
              </h1>
              <p class="mt-8 max-w-xl text-lg sm:text-xl leading-relaxed text-t-text">
                Two days, 41 talks, and not one of them a product demo. Infra,
                platform, security and data engineers on what broke, what they
                rebuilt, and the numbers that proved it.
              </p>
              <div class="mt-8 flex flex-wrap gap-3">
                <a
                  href="#tickets"
                  class="inline-flex items-center gap-2 rounded-xl bg-t-accent px-6 py-4 text-sm sm:text-base font-black uppercase tracking-wider text-t-on-accent hover:opacity-90 transition-opacity"
                >
                  Get tickets from €549
                  <span aria-hidden="true">→</span>
                </a>
                <a
                  href="#schedule"
                  class="inline-flex items-center rounded-xl border-2 border-t-border-active px-6 py-4 text-sm sm:text-base font-bold uppercase tracking-wider text-t-bright hover:bg-t-hover transition-colors"
                >
                  See the schedule
                </a>
              </div>
            </div>

            <div class="rounded-2xl border border-t-border bg-t-card/90 backdrop-blur p-5 sm:p-6 shadow-[0_30px_80px_-30px_var(--color-t-accent-glow)]">
              <div class="flex items-center justify-between gap-3">
                <p class="text-xs font-bold uppercase tracking-[0.2em] text-t-accent-ink">
                  Doors open in
                </p>
                <p class="font-[family-name:var(--font-mono)] text-xs text-t-muted">
                  09:00 CEST
                </p>
              </div>
              <div class="mt-4">
                <Countdown />
              </div>
              <dl class="mt-6 grid grid-cols-2 gap-4 border-t border-t-border pt-5 text-sm">
                <div>
                  <dt class="text-t-muted">Venue</dt>
                  <dd class="font-semibold text-t-bright">Halve Maan Werf</dd>
                  <dd class="text-t-text">Maashaven, Rotterdam</dd>
                </div>
                <div>
                  <dt class="text-t-muted">Seats left</dt>
                  <dd class="font-semibold text-t-bright">586 of 1,800</dd>
                  <dd class="mt-2 h-1.5 rounded-full bg-t-hover overflow-hidden">
                    <span class="block h-full w-[67%] bg-t-accent" />
                  </dd>
                </div>
              </dl>
            </div>
          </div>
        </div>

        <div class="relative">
          <Waveform />
        </div>

        {/* Stage photo: a wide cinematic strip that fades into the canvas. */}
        <figure class="relative mt-2">
          <DuotoneImage
            src="/landing/event/stage.webp"
            alt="Last year's opening keynote: a speaker alone on stage under a waveform on the big screen, seen from the back of a full hall"
            width={1440}
            height={800}
            loading="eager"
            position="50% 45%"
            class="h-[18rem] sm:h-[26rem] lg:h-[30rem]"
          />
          <div
            aria-hidden="true"
            class="absolute inset-0 bg-gradient-to-b from-t-deep via-transparent to-t-deep"
          />
          <div
            aria-hidden="true"
            class="absolute inset-0 bg-gradient-to-r from-t-deep/90 via-t-deep/20 to-transparent"
          />
          <figcaption class="absolute inset-x-0 bottom-10 sm:bottom-14">
            <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
              <p class="inline-block rounded-lg bg-t-deep/85 backdrop-blur px-3 py-2 font-[family-name:var(--font-mono)] text-xs sm:text-sm text-t-text">
                <span class="font-bold text-t-bright">Edition 03</span> · Main
                Hall · 1,100 seats, standing room only
              </p>
            </div>
          </figcaption>
        </figure>

        <div class="relative bg-t-accent text-t-on-accent -rotate-1 scale-[1.03] origin-center my-6 py-3 overflow-hidden">
          <ul
            aria-label="Topics"
            class="flex gap-8 whitespace-nowrap font-[family-name:var(--font-display)] text-lg sm:text-2xl font-black uppercase tracking-tight px-4"
          >
            {[...TOPICS, ...TOPICS].map((t, i) => (
              <li
                key={i}
                aria-hidden={i >= TOPICS.length ? "true" : undefined}
                class="flex items-center gap-8"
              >
                {t}
                <span aria-hidden="true">✦</span>
              </li>
            ))}
          </ul>
        </div>

        <div class="relative max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10">
          <dl class="grid grid-cols-2 lg:grid-cols-4 gap-px bg-t-border border border-t-border rounded-2xl overflow-hidden">
            {STATS.map((s) => (
              <div
                key={s.label}
                class="bg-t-deep p-5 sm:p-6 flex flex-col-reverse"
              >
                <dt class="text-sm font-semibold uppercase tracking-wider text-t-muted">
                  {s.label}
                </dt>
                <dd class="text-5xl sm:text-6xl font-black text-t-bright font-[family-name:var(--font-display)] tabular-nums">
                  {s.value}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      </section>

      <Speakers />
      <Schedule />

      {/* ── Venue ────────────────────────────────────────────── */}
      <section
        id="venue"
        aria-labelledby="venue-title"
        class="scroll-mt-20 py-20 sm:py-28"
      >
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <SectionHead
            index="03"
            id="venue-title"
            title="Venue"
            kicker="A 1920s shipyard hall on the river"
            note="Halve Maan Werf, Maashaven Zuidzijde 12, 3081 AE Rotterdam. The main hall seats 1,100 under the original crane rails."
          />

          <figure class="mt-12 relative overflow-hidden rounded-2xl border border-t-border">
            <DuotoneImage
              src="/landing/event/venue.webp"
              alt="Inside Halve Maan Werf: a converted shipyard hall with long tables under tall industrial windows"
              width={1440}
              height={800}
              class="aspect-[4/3] sm:aspect-[21/9]"
            />
            <div
              aria-hidden="true"
              class="absolute inset-0 bg-gradient-to-t from-t-deep via-t-deep/40 to-transparent"
            />
            <figcaption class="absolute inset-x-0 bottom-0 p-5 sm:p-8 flex flex-wrap items-end justify-between gap-x-8 gap-y-3">
              <p class="max-w-lg text-lg sm:text-2xl font-black uppercase leading-tight text-t-bright font-[family-name:var(--font-display)]">
                Lunch for 1,800 at the long tables of the old fitting shop
              </p>
              <dl class="flex gap-6 font-[family-name:var(--font-mono)] text-xs sm:text-sm">
                <div>
                  <dt class="text-t-text">Built</dt>
                  <dd class="font-bold text-t-bright">1921</dd>
                </div>
                <div>
                  <dt class="text-t-text">Floor</dt>
                  <dd class="font-bold text-t-bright">6,400 m²</dd>
                </div>
                <div>
                  <dt class="text-t-text">Ceiling</dt>
                  <dd class="font-bold text-t-bright">22 m</dd>
                </div>
              </dl>
            </figcaption>
          </figure>

          <div class="mt-8 grid gap-8 lg:grid-cols-[1.1fr_1fr]">
            <figure class="rounded-2xl border border-t-border bg-t-surface p-4 sm:p-6">
              <VenueMap />
              <figcaption class="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-t-muted">
                <span class="inline-flex items-center gap-2">
                  <span
                    aria-hidden="true"
                    class="w-3 h-3 rounded-sm bg-t-accent"
                  />
                  Halve Maan Werf
                </span>
                <span class="inline-flex items-center gap-2">
                  <span
                    aria-hidden="true"
                    class="w-3 h-1 rounded-full bg-t-info"
                  />
                  Metro line D
                </span>
                <span>Schematic, not to scale</span>
              </figcaption>
            </figure>

            <ul class="grid gap-px bg-t-border border border-t-border rounded-2xl overflow-hidden self-start">
              {TRAVEL.map((t) => (
                <li
                  key={t.mode}
                  class="bg-t-deep p-5 sm:p-6 grid grid-cols-[5.5rem_1fr] gap-4"
                >
                  <h3 class="font-[family-name:var(--font-mono)] text-sm font-bold uppercase tracking-wider text-t-accent-ink pt-0.5">
                    {t.mode}
                  </h3>
                  <p class="text-sm leading-relaxed text-t-text">{t.note}</p>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      <Tickets />
    </LandingShell>
  );
});

export const head: DocumentHead = {
  title: "Signal Summit — Conference / Event template",
  meta: [
    {
      name: "description",
      content:
        "Signal Summit 2027: two days of infrastructure and platform engineering talks in Rotterdam, 14–15 April. Speakers, schedule, venue and tickets.",
    },
  ],
};
