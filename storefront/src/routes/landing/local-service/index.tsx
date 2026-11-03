import { component$, useSignal } from "@builder.io/qwik";
import type { DocumentHead } from "@builder.io/qwik-city";
import { LandingShell } from "../../../components/landing/landing-shell";
import { QuoteForm } from "../../../components/landing/local-service/quote-form";
import { DuotoneImage } from "../../../components/landing/duotone-image";
import { SavingsEstimator } from "../../../components/landing/local-service/savings-estimator";

const TRUST = [
  { value: "Lic. #EC-40172", label: "Licensed, bonded & insured" },
  { value: "14 yrs", label: "Installing since 2012" },
  { value: "2,318", label: "Homes installed" },
  { value: "25-yr", label: "Workmanship warranty" },
  { value: "4.9 ★", label: "612 verified reviews" },
];

const SERVICES = [
  {
    title: "Rooftop solar",
    price: "Most homes 6–10 kW",
    body: "Tier-1 410 W panels, microinverters on every panel, and our own crew on your roof. We don't subcontract the install.",
    points: [
      "Structural check included",
      "Permits + utility paperwork",
      "1–2 day install",
    ],
    icon: "sun",
  },
  {
    title: "Battery backup",
    price: "13.5 kWh per unit",
    body: "Keeps the fridge, furnace fan, Wi-Fi and well pump running when the grid drops. Add it now or retrofit later.",
    points: [
      "Whole-home or critical loads",
      "Automatic switchover",
      "Works with existing solar",
    ],
    icon: "battery",
  },
  {
    title: "EV charger install",
    price: "Level 2, 48 A",
    body: "Garage or driveway, with a load calculation first so we don't overload your panel. Panel upgrades handled in-house.",
    points: [
      "Same-week scheduling",
      "Any charger brand",
      "Inspection booked for you",
    ],
    icon: "plug",
  },
  {
    title: "Cleaning & monitoring",
    price: "From $149 / visit",
    body: "Pollen and bird droppings cost 5–8% output. We clean, inspect wiring, and watch your production for dips.",
    points: [
      "Soft-water rinse, no scrubbing",
      "Alerts if output drops",
      "Systems we didn't install too",
    ],
    icon: "drop",
  },
] as const;

const STEPS = [
  {
    when: "Day 1",
    title: "Free roof check",
    body: "A 30-minute visit (or video call) to measure the roof, look at your panel and read your bills.",
  },
  {
    when: "Day 3",
    title: "Written quote",
    body: "Panel layout, itemized price, production estimate and payment options. Same price on day 3 or day 30.",
  },
  {
    when: "Weeks 2–5",
    title: "Permits & approval",
    body: "We file with the town and the utility and handle every inspection. You don't have to be home.",
  },
  {
    when: "1–2 days",
    title: "Install & switch-on",
    body: "One crew, one or two days. After the utility's final sign-off we walk you through the app and flip it on.",
  },
];

const REVIEWS = [
  {
    name: "Maria T.",
    town: "Linden Falls",
    stars: 5,
    system: "8.2 kW + battery",
    text: "Our first bill after switch-on was $11. During the March ice storm we were the only house on the street with lights, and the battery still had 40% in the morning.",
  },
  {
    name: "Greg H.",
    town: "Ashford",
    stars: 5,
    system: "6.6 kW",
    text: "Three companies quoted. Brightline was the only one that came up on the roof instead of using a satellite photo, and they found a rotten section of decking the others missed. They fixed it at cost.",
  },
  {
    name: "Priya K.",
    town: "Millbrook",
    stars: 5,
    system: "EV charger",
    text: "Electrician showed up at 8:00 like they said, ran 40 feet of conduit through a finished basement and you'd never know. Inspection passed first try.",
  },
  {
    name: "Dennis O.",
    town: "Westover",
    stars: 4,
    system: "9.8 kW",
    text: "Utility approval took a week longer than quoted, which wasn't their fault, but they called me every Friday with an update. System is beating the production estimate by about 6%.",
  },
  {
    name: "Alicia R.",
    town: "Pine Ridge",
    stars: 5,
    system: "Cleaning plan",
    text: "Another company installed our panels and then went out of business. Brightline took over monitoring, spotted a dead microinverter, and replaced it under the manufacturer warranty.",
  },
];

const TOWNS = [
  { name: "Millbrook", note: "Office" },
  { name: "Ashford" },
  { name: "Linden Falls" },
  { name: "Harlow" },
  { name: "Pine Ridge" },
  { name: "Westover" },
  { name: "Clearwater" },
  { name: "Brookhaven" },
  { name: "Oak Hollow" },
  { name: "Fairmont" },
  { name: "Sutton Mills" },
  { name: "Granby" },
  { name: "East Carver" },
  { name: "Ridgeway" },
];

/* Approximate town positions on the stylized region map (0–100 grid). */
const PINS: [number, number, string][] = [
  [48, 50, "Millbrook"],
  [30, 34, "Ashford"],
  [66, 30, "Linden Falls"],
  [22, 60, "Harlow"],
  [72, 62, "Pine Ridge"],
  [40, 76, "Westover"],
  [58, 18, "Clearwater"],
  [82, 44, "Brookhaven"],
  [14, 40, "Oak Hollow"],
  [60, 80, "Fairmont"],
  [36, 20, "Sutton Mills"],
  [84, 74, "Granby"],
  [18, 78, "East Carver"],
  [52, 64, "Ridgeway"],
];

const FAQ = [
  {
    q: "How much does a typical system cost?",
    a: "Most homes we install land between 6 and 10 kW, which runs about $18,900 to $30,300 before any incentives. Your quote is itemized: panels, inverters, racking, labor, permits and any electrical upgrades are separate lines.",
  },
  {
    q: "Will my roof need to be replaced first?",
    a: "Only if it has less than about 10 years left. We check shingles, decking and rafters on the free visit. If it's close, we'll tell you plainly and can coordinate with a local roofer so panels go on once.",
  },
  {
    q: "What happens when the power goes out?",
    a: "Grid-tied solar without a battery shuts off during an outage for line-worker safety. With a battery, your chosen circuits switch over automatically in under a second and solar keeps recharging it during the day.",
  },
  {
    q: "Do you offer financing?",
    a: "Yes: cash, a 10- or 20-year loan through a regional credit union, or a battery-only payment plan. We don't do leases or PPAs, so the system and its savings are always yours.",
  },
  {
    q: "What does the 25-year warranty actually cover?",
    a: "Our workmanship warranty covers roof penetrations, leaks, wiring and racking for 25 years, and it transfers if you sell the house. Panels and microinverters carry their own 25-year manufacturer warranties, and we handle those claims for you.",
  },
  {
    q: "I'm just outside your service area. Can you still help?",
    a: "Often, yes. We regularly go up to 40 minutes past the towns listed. Put your ZIP in the quote form and we'll tell you on the first call.",
  },
];

function Icon({ name }: { name: string }) {
  const common = {
    class: "w-6 h-6",
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": 1.8,
    "stroke-linecap": "round" as const,
    "stroke-linejoin": "round" as const,
    "aria-hidden": "true" as const,
  };
  if (name === "sun")
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
      </svg>
    );
  if (name === "battery")
    return (
      <svg {...common}>
        <rect x="6" y="4" width="12" height="17" rx="2" />
        <path d="M10 2h4M12.5 8.5l-2 3.5h3l-2 3.5" />
      </svg>
    );
  if (name === "plug")
    return (
      <svg {...common}>
        <path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0V8zM12 17v4" />
      </svg>
    );
  return (
    <svg {...common}>
      <path d="M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z" />
      <path d="M9.5 14.5a2.5 2.5 0 0 0 2.5 2.5" />
    </svg>
  );
}

function Stars({ n }: { n: number }) {
  return (
    <span
      class="flex gap-0.5 text-t-accent-alt"
      role="img"
      aria-label={`${n} out of 5 stars`}
    >
      {[1, 2, 3, 4, 5].map((i) => (
        <svg
          key={i}
          class={["w-4 h-4", i > n ? "text-t-border" : ""]}
          viewBox="0 0 20 20"
          fill="currentColor"
          aria-hidden="true"
        >
          <path d="M10 1.5l2.6 5.5 6 .7-4.4 4.1 1.2 5.9L10 14.8l-5.4 2.9 1.2-5.9L1.4 7.7l6-.7z" />
        </svg>
      ))}
    </span>
  );
}

function SectionHead({
  id,
  kicker,
  title,
  sub,
}: {
  id: string;
  kicker: string;
  title: string;
  sub?: string;
}) {
  return (
    <div class="max-w-2xl">
      <p class="text-xs font-bold uppercase tracking-[0.14em] text-t-accent-ink">
        {kicker}
      </p>
      <h2
        id={id}
        class="mt-2 text-2xl sm:text-3xl font-bold tracking-tight text-t-bright font-[family-name:var(--font-display)]"
      >
        {title}
      </h2>
      {sub && <p class="mt-3 text-t-text leading-relaxed">{sub}</p>}
    </div>
  );
}

export default component$(() => {
  const openFaq = useSignal<number>(0);

  return (
    <LandingShell
      brand="Brightline Solar"
      mark="B"
      links={[
        { label: "Services", href: "#services" },
        { label: "Savings", href: "#savings" },
        { label: "Reviews", href: "#reviews" },
      ]}
      cta={{ label: "Get a quote", href: "#quote" }}
      tagline="Residential solar, batteries and EV chargers for the Cedar Valley. Family-run out of Millbrook since 2012."
      footer={{
        Services: [
          "Rooftop solar",
          "Battery backup",
          "EV chargers",
          "Cleaning plans",
        ],
        Company: ["About the crew", "Warranty", "Careers", "Referral program"],
        Contact: [
          "(555) 014-7700",
          "Mon–Sat 7am–6pm",
          "212 Mill St, Millbrook",
        ],
      }}
    >
      {/* ── Hero + quote form ─────────────────────────────── */}
      <section
        aria-labelledby="hero-title"
        class="relative overflow-hidden border-b border-t-border"
      >
        {/* Roof photo as the desktop backdrop, faded into the page on the
            left so the headline sits on solid canvas. */}
        <div
          aria-hidden="true"
          class="hidden lg:block absolute inset-y-0 right-0 w-[64%]"
        >
          <DuotoneImage
            src="/landing/local-service/roof.webp"
            alt=""
            width={1440}
            height={800}
            loading="eager"
            position="55% 60%"
            class="h-full w-full"
          />
          <div class="absolute inset-0 bg-gradient-to-r from-t-deep via-t-deep/80 via-40% to-t-deep/5" />
          <div class="absolute inset-x-0 bottom-0 h-28 bg-gradient-to-t from-t-deep to-transparent" />
        </div>

        <div class="relative max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pt-10 pb-12 sm:pt-14 sm:pb-16 lg:pt-16 lg:pb-24 grid lg:grid-cols-[minmax(0,1fr)_minmax(0,27rem)] gap-10 lg:gap-14 items-start">
          <div class="lg:pt-4">
            <p class="inline-flex items-center gap-2 rounded-full border border-t-border bg-t-card px-3 py-1 text-xs text-t-text">
              <span
                class="w-2 h-2 rounded-full bg-t-success"
                aria-hidden="true"
              />
              Booking roof checks for the week of Oct 13
            </p>
            <h1
              id="hero-title"
              class="mt-5 text-4xl sm:text-5xl lg:text-[3.4rem] leading-[1.05] font-black tracking-tight text-t-bright font-[family-name:var(--font-display)]"
            >
              Solar for Cedar Valley homes, installed by the people who answer
              the phone.
            </h1>
            <p class="mt-5 text-lg text-t-text leading-relaxed max-w-xl">
              A Millbrook crew of 31 installers and electricians. We size the
              system to your actual bills, quote it in writing, and stay on the
              hook for 25 years.
            </p>

            <ul class="mt-7 grid sm:grid-cols-2 gap-x-6 gap-y-3 text-sm text-t-text max-w-xl">
              {[
                "Free on-site roof check",
                "No leases, no PPAs",
                "In-house electricians, no subs",
                "Average install: 1.5 days",
              ].map((t) => (
                <li key={t} class="flex items-center gap-2.5">
                  <svg
                    class="w-4 h-4 shrink-0 text-t-success"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                    aria-hidden="true"
                  >
                    <path d="M8.1 14.3l-4-4 1.4-1.4 2.6 2.6 6.4-6.4 1.4 1.4z" />
                  </svg>
                  {t}
                </li>
              ))}
            </ul>

            <div class="mt-8 flex flex-wrap items-center gap-x-6 gap-y-3">
              <a
                href="tel:5550147700"
                class="inline-flex items-center gap-3 rounded-lg border border-t-border-active bg-t-card px-4 py-3 hover:bg-t-hover transition-colors"
              >
                <span class="grid place-items-center w-9 h-9 rounded-full bg-t-accent/15 text-t-accent-ink">
                  <svg
                    class="w-4 h-4"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="2"
                    aria-hidden="true"
                  >
                    <path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2" />
                  </svg>
                </span>
                <span>
                  <span class="block text-xs text-t-muted">
                    Prefer to talk? Mon–Sat, 7am–6pm
                  </span>
                  <span class="block font-bold text-t-bright tabular-nums">
                    (555) 014-7700
                  </span>
                </span>
              </a>
              <div class="flex items-center gap-3">
                <div class="flex -space-x-2" aria-hidden="true">
                  {["MT", "GH", "PK", "DO"].map((i) => (
                    <span
                      key={i}
                      class="grid place-items-center w-8 h-8 rounded-full border-2 border-t-deep bg-t-hover text-[10px] font-bold text-t-bright"
                    >
                      {i}
                    </span>
                  ))}
                </div>
                <p class="text-sm text-t-text leading-tight">
                  <span class="font-bold text-t-bright">4.9 out of 5</span>
                  <br />
                  <span class="text-t-muted">from 612 neighbors</span>
                </p>
              </div>
            </div>
          </div>

          {/* Solid canvas under the card: some skins use a translucent card
              colour, and the form must never sit on the photo. */}
          <div id="quote" class="scroll-mt-24 rounded-xl bg-t-deep">
            <QuoteForm />
          </div>

          <DuotoneImage
            src="/landing/local-service/roof.webp"
            alt="A two-storey home with a Brightline rooftop solar array on its south-facing roof"
            width={1440}
            height={800}
            position="55% 60%"
            class="lg:hidden aspect-[16/9] rounded-xl border border-t-border"
          />
        </div>
      </section>

      {/* ── Trust row ─────────────────────────────────────── */}
      <section aria-labelledby="trust-title" class="bg-t-surface">
        <h2 id="trust-title" class="sr-only">
          Why homeowners trust Brightline
        </h2>
        <ul class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 grid grid-cols-2 md:grid-cols-5 divide-t-border md:divide-x">
          {TRUST.map((t, i) => (
            <li
              key={t.label}
              class={[
                "py-6 md:py-8 md:px-5 first:md:pl-0",
                i === 4 ? "col-span-2 md:col-span-1" : "",
              ]}
            >
              <p class="text-xl sm:text-2xl font-black text-t-bright tabular-nums font-[family-name:var(--font-display)]">
                {t.value}
              </p>
              <p class="mt-1 text-xs sm:text-sm text-t-muted">{t.label}</p>
            </li>
          ))}
        </ul>
      </section>

      {/* ── Services ──────────────────────────────────────── */}
      <section
        id="services"
        aria-labelledby="services-title"
        class="scroll-mt-20 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-16 sm:py-20"
      >
        <div class="flex flex-col lg:flex-row lg:items-end lg:justify-between gap-4">
          <SectionHead
            id="services-title"
            kicker="What we install"
            title="Four services, one crew that knows your house."
          />
          <p class="text-sm text-t-muted max-w-sm">
            Every job is permitted, inspected and backed by the same 25-year
            workmanship warranty.
          </p>
        </div>

        <div class="mt-10 grid md:grid-cols-2 border-t border-l border-t-border">
          {SERVICES.map((s) => (
            <article
              key={s.title}
              class="border-r border-b border-t-border p-6 sm:p-8 flex gap-5 hover:bg-t-card transition-colors"
            >
              <span class="shrink-0 grid place-items-center w-12 h-12 rounded-lg bg-t-accent/12 text-t-accent-ink">
                <Icon name={s.icon} />
              </span>
              <div class="min-w-0">
                <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <h3 class="text-lg font-bold text-t-bright">{s.title}</h3>
                  <span class="text-xs font-[family-name:var(--font-mono)] text-t-muted">
                    {s.price}
                  </span>
                </div>
                <p class="mt-2 text-sm text-t-text leading-relaxed">{s.body}</p>
                <ul class="mt-4 flex flex-wrap gap-2">
                  {s.points.map((p) => (
                    <li
                      key={p}
                      class="text-xs rounded-md bg-t-surface border border-t-border px-2.5 py-1 text-t-text"
                    >
                      {p}
                    </li>
                  ))}
                </ul>
              </div>
            </article>
          ))}
        </div>

        <article
          aria-labelledby="battery-feature-title"
          class="mt-8 grid md:grid-cols-[minmax(0,5fr)_minmax(0,6fr)] rounded-2xl border border-t-border bg-t-card overflow-hidden"
        >
          <DuotoneImage
            src="/landing/local-service/battery.webp"
            alt="A wall-mounted home battery beside a Level 2 EV charger in a customer's garage"
            width={1024}
            height={768}
            class="aspect-[4/3] md:aspect-auto md:h-full md:min-h-80"
          />
          <div class="p-6 sm:p-8 lg:p-10 flex flex-col justify-center">
            <p class="text-xs font-bold uppercase tracking-[0.14em] text-t-accent-ink">
              Battery backup, up close
            </p>
            <h3
              id="battery-feature-title"
              class="mt-2 text-xl sm:text-2xl font-bold tracking-tight text-t-bright font-[family-name:var(--font-display)]"
            >
              When the grid drops, the lights don't blink.
            </h3>
            <p class="mt-3 text-sm sm:text-base text-t-text leading-relaxed">
              A typical garage install: one 13.5 kWh battery on the wall, the EV
              charger next to it, and a critical-loads panel so the battery only
              powers what matters during an outage.
            </p>
            <dl class="mt-6 grid grid-cols-2 gap-x-6 gap-y-4 text-sm">
              {[
                ["Switchover", "Under 1 second"],
                ["Typical runtime", "18–24 hrs on essentials"],
                ["Recharges from", "Solar, the grid, or both"],
                ["Warranty", "10 years, 70% capacity"],
              ].map(([k, v]) => (
                <div key={k}>
                  <dt class="text-xs text-t-muted">{k}</dt>
                  <dd class="mt-0.5 font-semibold text-t-bright">{v}</dd>
                </div>
              ))}
            </dl>
          </div>
        </article>
      </section>

      {/* ── Savings estimator ─────────────────────────────── */}
      <section
        id="savings"
        aria-labelledby="savings-title"
        class="scroll-mt-20 bg-t-surface border-y border-t-border"
      >
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-16 sm:py-20">
          <SectionHead
            id="savings-title"
            kicker="Savings estimator"
            title="Move the slider to your bill. See a ballpark in seconds."
            sub="Plain arithmetic, no email required. Your written quote replaces these numbers with ones based on your actual roof."
          />
          <div class="mt-10">
            <SavingsEstimator />
          </div>
        </div>
      </section>

      {/* ── How an install works ──────────────────────────── */}
      <section
        aria-labelledby="process-title"
        class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-16 sm:py-20"
      >
        <div class="grid lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)] gap-10 lg:gap-14 items-center">
          <div>
            <SectionHead
              id="process-title"
              kicker="How an install works"
              title="From first call to switch-on in about six weeks."
              sub="The people who measure your roof are the people who bolt the panels to it. Every crew is three installers and a licensed master electrician, all on our payroll."
            />
            <dl class="mt-8 grid grid-cols-3 gap-4 max-w-md">
              {[
                ["31", "on staff"],
                ["8", "crews"],
                ["0", "subcontractors"],
              ].map(([n, l]) => (
                <div key={l} class="border-l-2 border-t-accent pl-3">
                  <dt class="sr-only">{l}</dt>
                  <dd class="text-2xl font-black text-t-bright tabular-nums font-[family-name:var(--font-display)]">
                    {n}
                  </dd>
                  <dd class="text-xs text-t-muted">{l}</dd>
                </div>
              ))}
            </dl>
          </div>
          <figure class="relative">
            <DuotoneImage
              src="/landing/local-service/crew.webp"
              alt="Two Brightline installers in harnesses lifting a solar panel onto roof rails"
              width={1024}
              height={768}
              class="aspect-[4/3] rounded-2xl border border-t-border"
            />
            <figcaption class="absolute left-4 bottom-4 right-4 sm:right-auto rounded-lg bg-t-deep shadow-lg">
              {/* Solid canvas first: some skins use a translucent card. */}
              <span class="block rounded-lg border border-t-border bg-t-card px-4 py-3">
                <span class="block text-sm font-semibold text-t-bright">
                  Day 2 of a 9.8 kW install in Westover
                </span>
                <span class="block text-xs text-t-muted">
                  Crew lead: Marcus D., 9 years with Brightline
                </span>
              </span>
            </figcaption>
          </figure>
        </div>
        <ol class="mt-14 grid md:grid-cols-4 gap-8 md:gap-0 relative">
          <span
            aria-hidden="true"
            class="hidden md:block absolute top-5 left-5 right-[12%] h-0.5 bg-t-border"
          />
          <span
            aria-hidden="true"
            class="md:hidden absolute top-2 bottom-2 left-5 w-0.5 bg-t-border"
          />
          {STEPS.map((s, i) => (
            <li key={s.title} class="relative flex md:block gap-5 md:pr-8">
              <span class="relative z-10 shrink-0 grid place-items-center w-10 h-10 rounded-full bg-t-accent text-t-on-accent font-black ring-4 ring-t-deep">
                {i + 1}
              </span>
              <div class="md:mt-5">
                <p class="text-xs font-[family-name:var(--font-mono)] text-t-accent-ink">
                  {s.when}
                </p>
                <h3 class="mt-1 text-lg font-bold text-t-bright">{s.title}</h3>
                <p class="mt-2 text-sm text-t-text leading-relaxed">{s.body}</p>
              </div>
            </li>
          ))}
        </ol>
      </section>

      {/* ── Reviews ───────────────────────────────────────── */}
      <section
        id="reviews"
        aria-labelledby="reviews-title"
        class="scroll-mt-20 bg-t-surface border-y border-t-border"
      >
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-16 sm:py-20 grid lg:grid-cols-[minmax(0,18rem)_minmax(0,1fr)] gap-10">
          <div>
            <SectionHead
              id="reviews-title"
              kicker="Reviews"
              title="What the neighbors say."
            />
            <div class="mt-6 rounded-xl border border-t-border bg-t-card p-5">
              <p class="text-5xl font-black text-t-bright font-[family-name:var(--font-display)]">
                4.9
              </p>
              <Stars n={5} />
              <p class="mt-2 text-sm text-t-muted">612 reviews, 2014–2026</p>
              <dl class="mt-4 space-y-1.5 text-xs">
                {[
                  [5, 91],
                  [4, 7],
                  [3, 1],
                  [2, 1],
                ].map(([star, pct]) => (
                  <div key={star} class="flex items-center gap-2">
                    <dt class="w-6 text-t-muted tabular-nums">{star}★</dt>
                    <dd class="flex-1 h-1.5 rounded-full bg-t-hover overflow-hidden">
                      <span
                        class="block h-full bg-t-accent-alt"
                        style={{ width: `${pct}%` }}
                      />
                    </dd>
                    <dd class="w-8 text-right text-t-muted tabular-nums">
                      {pct}%
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          </div>

          <div class="columns-1 md:columns-2 gap-5 [&>*]:break-inside-avoid">
            {REVIEWS.map((r) => (
              <figure
                key={r.name}
                class="mb-5 rounded-xl border border-t-border bg-t-card p-5 sm:p-6"
              >
                <div class="flex items-center justify-between gap-3">
                  <Stars n={r.stars} />
                  <span class="text-[11px] rounded-full border border-t-border px-2 py-0.5 text-t-muted">
                    {r.system}
                  </span>
                </div>
                <blockquote class="mt-3 text-sm text-t-text leading-relaxed">
                  “{r.text}”
                </blockquote>
                <figcaption class="mt-4 flex items-center gap-3">
                  <span
                    aria-hidden="true"
                    class="grid place-items-center w-9 h-9 rounded-full bg-t-accent/15 text-t-accent-ink text-xs font-bold"
                  >
                    {r.name
                      .split(" ")
                      .map((p) => p[0])
                      .join("")}
                  </span>
                  <span class="text-sm">
                    <span class="block font-semibold text-t-bright">
                      {r.name}
                    </span>
                    <span class="block text-xs text-t-muted">{r.town}</span>
                  </span>
                </figcaption>
              </figure>
            ))}
          </div>
        </div>
      </section>

      {/* ── Service area ──────────────────────────────────── */}
      <section
        id="area"
        aria-labelledby="area-title"
        class="scroll-mt-20 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-16 sm:py-20 grid lg:grid-cols-2 gap-10 lg:gap-14 items-center"
      >
        <div>
          <SectionHead
            id="area-title"
            kicker="Service area"
            title="Fourteen towns, all within 35 minutes of our shop."
            sub="Staying close means we can be back on your roof in a day if anything needs attention. Don't see your town? Ask anyway."
          />
          <ul class="mt-8 grid grid-cols-2 sm:grid-cols-3 gap-x-6 gap-y-2.5 text-sm">
            {TOWNS.map((t) => (
              <li key={t.name} class="flex items-center gap-2 text-t-text">
                <span
                  aria-hidden="true"
                  class={[
                    "w-1.5 h-1.5 rounded-full",
                    t.note ? "bg-t-accent-alt" : "bg-t-accent",
                  ]}
                />
                {t.name}
                {t.note && (
                  <span class="text-[10px] uppercase tracking-wide font-bold text-t-muted">
                    {t.note}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>

        <div class="rounded-xl border border-t-border bg-t-card p-3 sm:p-4">
          <svg
            viewBox="0 0 100 100"
            class="w-full h-auto"
            role="img"
            aria-label="Map of the Cedar Valley service area, centered on Millbrook"
          >
            <rect width="100" height="100" rx="3" class="fill-t-surface" />
            <path
              d="M8 22 C20 10 40 8 55 9 S88 14 93 30 S96 70 86 86 S50 96 30 92 S5 75 6 55 S4 30 8 22z"
              class="fill-t-accent/10 stroke-t-accent/50"
              stroke-width="0.5"
              stroke-dasharray="1.5 1"
            />
            <path
              d="M0 58 C18 54 26 66 42 60 S70 44 100 52"
              fill="none"
              class="stroke-t-info/50"
              stroke-width="1.4"
            />
            <path
              d="M48 0 L48 100 M0 46 L100 40"
              class="stroke-t-border"
              stroke-width="0.7"
            />
            <circle
              cx="48"
              cy="50"
              r="34"
              fill="none"
              class="stroke-t-muted/40"
              stroke-width="0.3"
              stroke-dasharray="1 1.2"
            />
            {PINS.map(([x, y, label]) => {
              const hq = label === "Millbrook";
              return (
                <g key={label}>
                  {hq && (
                    <circle
                      cx={x}
                      cy={y}
                      r="4.5"
                      class="fill-t-accent-alt/25"
                    />
                  )}
                  <circle
                    cx={x}
                    cy={y}
                    r={hq ? 2.2 : 1.3}
                    class={hq ? "fill-t-accent-alt" : "fill-t-accent"}
                  />
                  <text
                    x={x}
                    y={y - 3}
                    text-anchor="middle"
                    class={[
                      "text-[3px]",
                      hq ? "fill-t-bright font-bold" : "fill-t-text",
                    ]}
                  >
                    {label}
                  </text>
                </g>
              );
            })}
            <text
              x="4"
              y="6"
              text-anchor="start"
              class="fill-t-muted text-[2.6px]"
            >
              Dashed ring ≈ 35 min drive
            </text>
          </svg>
        </div>
      </section>

      {/* ── FAQ ───────────────────────────────────────────── */}
      <section
        aria-labelledby="faq-title"
        class="bg-t-surface border-t border-t-border"
      >
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-16 sm:py-20 grid lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-10">
          <div>
            <SectionHead
              id="faq-title"
              kicker="FAQ"
              title="Straight answers before you call."
            />
            <a
              href="#quote"
              class="mt-6 inline-flex items-center rounded-lg bg-t-accent px-5 py-3 text-sm font-bold text-t-on-accent hover:opacity-90 transition-opacity"
            >
              Get a free quote
            </a>
          </div>
          <div class="divide-y divide-t-border border-y border-t-border">
            {FAQ.map((f, i) => {
              const open = openFaq.value === i;
              return (
                <div key={f.q}>
                  <h3>
                    <button
                      type="button"
                      id={`faq-btn-${i}`}
                      aria-expanded={open}
                      aria-controls={`faq-panel-${i}`}
                      onClick$={() => (openFaq.value = open ? -1 : i)}
                      class="w-full flex items-center justify-between gap-4 py-5 text-left font-semibold text-t-bright hover:text-t-accent-ink transition-colors"
                    >
                      {f.q}
                      <span
                        aria-hidden="true"
                        class={[
                          "shrink-0 grid place-items-center w-7 h-7 rounded-full border border-t-border text-t-muted transition-transform",
                          open ? "rotate-45" : "",
                        ]}
                      >
                        +
                      </span>
                    </button>
                  </h3>
                  <div
                    id={`faq-panel-${i}`}
                    role="region"
                    aria-labelledby={`faq-btn-${i}`}
                    hidden={!open}
                    class="pb-5 pr-10 text-sm text-t-text leading-relaxed"
                  >
                    {f.a}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </section>
    </LandingShell>
  );
});

export const head: DocumentHead = {
  title: "Brightline Solar — Local Service Business template",
  meta: [
    {
      name: "description",
      content:
        "Lead-generation landing template for a residential solar and battery installer: quote form above the fold, trust badges, savings estimator, install timeline, reviews and service area.",
    },
  ],
};
