import { component$, useSignal } from "@builder.io/qwik";
import type { DocumentHead } from "@builder.io/qwik-city";
import { LandingShell } from "../../../components/landing/landing-shell";
import { DashboardMockup } from "../../../components/landing/saas/dashboard-mockup";
import { FeatureBento } from "../../../components/landing/saas/feature-bento";
import { DuotoneImage } from "../../../components/landing/duotone-image";

const display = "font-[family-name:var(--font-display)]";
const mono = "font-[family-name:var(--font-mono)]";

const STEPS = [
  {
    time: "06:00",
    title: "Forecasts refresh overnight",
    body: "Yesterday's sales, receipts and transfers land from your ERP. Every SKU-location pair gets a new 26-week forecast with a confidence band.",
  },
  {
    time: "06:04",
    title: "Risk gets ranked, not dumped",
    body: "Items are sorted by days of cover against real supplier lead time. Seven SKUs need a call this week; the other 4,193 do not show up.",
  },
  {
    time: "09:12",
    title: "A buyer approves the draft",
    body: "Quantities are already rounded to case packs and supplier minimums. Edit a line, or approve all four POs in one pass.",
  },
  {
    time: "09:13",
    title: "The PO leaves the building",
    body: "Approved orders sync back to your ERP and go to the supplier by email or EDI 850. Receipts close the loop and tune the next forecast.",
  },
];

const TIERS = [
  {
    name: "Team",
    monthly: 300,
    who: "One buyer, a couple of warehouses.",
    features: [
      "Up to 2,000 active SKUs",
      "2 locations",
      "Weekly forecast refresh",
      "CSV and SFTP import",
      "Email approvals",
    ],
  },
  {
    name: "Growth",
    monthly: 780,
    who: "Ops teams replacing the reorder spreadsheet.",
    features: [
      "Up to 15,000 active SKUs",
      "10 locations",
      "Daily forecast refresh",
      "Promo calendar and price events",
      "Transfer suggestions",
      "Two-way ERP sync",
      "Approval rules by value and category",
    ],
  },
  {
    name: "Scale",
    monthly: 1800,
    who: "Multi-region networks with EDI suppliers.",
    features: [
      "Unlimited SKUs and locations",
      "Hourly forecast refresh",
      "EDI 850 / 855 with suppliers",
      "SSO, SCIM and audit log",
      "Named forecast analyst",
    ],
  },
];

const FAQS = [
  {
    q: "How much sales history do you need?",
    a: "Twelve months by SKU and location lets us pick up seasonality. Thirteen weeks is the minimum to start. New items without history borrow a curve from similar SKUs you nominate, then switch to their own data after six weeks of sales.",
  },
  {
    q: "Does Northwind place orders on its own?",
    a: "Not unless you tell it to. Every purchase order is a draft until a person approves it. You can add auto-approve rules later, for example repeat items under $2,000 from a supplier with a 95% on-time record.",
  },
  {
    q: "Which systems do you connect to?",
    a: "Most mid-market ERPs and warehouse systems through REST connectors or webhooks, plus a nightly CSV drop over SFTP for anything else. EDI 850 and 855 with suppliers is included on Scale.",
  },
  {
    q: "How long does setup take?",
    a: "The median customer approves their first Northwind purchase order nine business days after sending us a data export. Most of that time is mapping units of measure and case packs, which we do with you on two calls.",
  },
  {
    q: "What counts as an active SKU?",
    a: "Any item with a sale, receipt or open PO in the last 90 days. Discontinued and dormant items stay in your history for free and do not count toward your plan.",
  },
  {
    q: "Can we change plans or cancel an annual contract?",
    a: "Move up a tier at any time and we prorate the difference. Annual plans can be canceled for a prorated refund within the first 60 days; after that they run to the end of the term.",
  },
];

const fmt = (n: number) => "$" + n.toLocaleString("en-US");

export default component$(() => {
  const annual = useSignal(true);
  const openFaq = useSignal<number>(0);
  const email = useSignal("");
  const emailError = useSignal("");
  const sentTo = useSignal("");

  return (
    <LandingShell
      brand="Northwind"
      mark="N"
      links={[
        { label: "Product", href: "#product" },
        { label: "Features", href: "#features" },
        { label: "Pricing", href: "#pricing" },
        { label: "FAQ", href: "#faq" },
      ]}
      cta={{ label: "Start free trial", href: "#pricing" }}
      footer={{
        Product: ["Reorder queue", "Forecasting", "Transfers", "Pricing"],
        Company: ["About", "Customers", "Careers", "Security"],
        Resources: ["Docs", "API reference", "Status", "Changelog"],
      }}
      tagline="Demand forecasting and reorder planning for operations teams running 500 to 50,000 SKUs."
    >
      {/* ── Hero ─────────────────────────────────────────────── */}
      <section
        aria-labelledby="saas-hero-title"
        class="relative overflow-hidden"
      >
        <div
          class="pointer-events-none absolute inset-x-0 top-0 h-[560px] bg-[radial-gradient(ellipse_60%_55%_at_70%_0%,var(--color-t-accent-glow),transparent_70%)]"
          aria-hidden="true"
        />
        <div
          class="pointer-events-none absolute inset-0 opacity-60 bg-[linear-gradient(to_right,var(--color-t-border)_1px,transparent_1px),linear-gradient(to_bottom,var(--color-t-border)_1px,transparent_1px)] bg-[size:56px_56px] [mask-image:linear-gradient(to_bottom,black,transparent_60%)]"
          aria-hidden="true"
        />
        <div class="relative max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pt-16 sm:pt-24 pb-16">
          <div class="grid lg:grid-cols-12 gap-10 items-end">
            <div class="lg:col-span-8 space-y-6">
              <a
                href="#product"
                class="inline-flex items-center gap-2 rounded-full border border-t-border bg-t-card pl-1.5 pr-3 py-1 text-xs text-t-text hover:border-t-border-active transition-colors"
              >
                <span class="rounded-full bg-t-accent text-t-on-accent px-2 py-0.5 font-semibold">
                  New
                </span>
                Transfer suggestions across locations
                <span aria-hidden="true">→</span>
              </a>
              <h1
                id="saas-hero-title"
                class={[
                  "text-4xl sm:text-6xl lg:text-7xl font-bold tracking-tight text-t-bright leading-[1.02]",
                  display,
                ]}
              >
                Reorder before the shelf tells you to.
              </h1>
              <p class="text-base sm:text-lg text-t-text leading-relaxed max-w-2xl">
                Northwind forecasts demand for every SKU at every location,
                checks it against each supplier's real lead time, and drafts the
                purchase order for a buyer to approve. Your reorder spreadsheet
                can retire.
              </p>
              <div class="flex flex-wrap gap-3">
                <a
                  href="#pricing"
                  class="inline-flex items-center px-5 py-3 rounded-lg bg-t-accent text-t-on-accent font-semibold hover:opacity-90 transition-opacity"
                >
                  Start a 14-day trial
                </a>
                <a
                  href="#demo"
                  class="inline-flex items-center gap-2 px-5 py-3 rounded-lg border border-t-border bg-t-card text-t-bright font-semibold hover:bg-t-hover transition-colors"
                >
                  Get a sample forecast
                </a>
              </div>
            </div>
            <dl class="lg:col-span-4 grid grid-cols-3 lg:grid-cols-1 gap-4 lg:gap-5 lg:border-l lg:border-t-border lg:pl-8">
              {[
                ["−38%", "stockouts in the first quarter, median customer"],
                ["9 days", "from data export to first approved PO"],
                ["4.2 h", "of buyer time saved per week, per buyer"],
              ].map(([v, k]) => (
                <div key={v}>
                  <dt class="sr-only">{k}</dt>
                  <dd
                    class={[
                      "text-2xl sm:text-3xl font-semibold text-t-bright tracking-tight",
                      mono,
                    ]}
                  >
                    {v}
                  </dd>
                  <dd class="text-xs sm:text-sm text-t-muted leading-snug mt-1">
                    {k}
                  </dd>
                </div>
              ))}
            </dl>
          </div>

          <div class="mt-14 sm:mt-16">
            <DashboardMockup />
            <p class="sr-only">
              Product screenshot: the Northwind reorder queue, showing fill rate
              of 98.4 percent, seven SKUs at stockout risk, a demand chart with
              a six-week forecast, and a drafted purchase order awaiting
              approval.
            </p>
          </div>
        </div>
      </section>

      {/* ── Trusted by ───────────────────────────────────────── */}
      <section
        aria-labelledby="saas-logos-title"
        class="border-y border-t-border bg-t-surface"
      >
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10 flex flex-col lg:flex-row lg:items-center gap-8">
          <h2
            id="saas-logos-title"
            class="text-sm text-t-muted lg:w-52 shrink-0 leading-snug"
          >
            Planning 1.9 million SKUs a night for 340 operations teams,
            including
          </h2>
          <ul class="flex-1 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-x-6 gap-y-6 items-center text-t-text">
            <li class="text-center font-serif text-xl tracking-[0.25em] uppercase">
              Halvard
            </li>
            <li class="text-center text-xl italic font-light lowercase">
              kettle<span class="text-t-accent-ink font-semibold">&amp;</span>co
            </li>
            <li class="flex items-center justify-center gap-1.5 font-bold text-lg tracking-tight">
              <span class="w-4 h-4 bg-current rotate-45" aria-hidden="true" />
              Ferro
            </li>
            <li
              class={[
                "text-center text-lg font-black uppercase tracking-tighter scale-y-110",
                display,
              ]}
            >
              Brightline
            </li>
            <li class="text-center font-serif italic text-2xl">Oakmere</li>
            <li class={["text-center text-base", mono]}>
              parcel<span class="text-t-muted">/</span>nine
            </li>
          </ul>
        </div>
      </section>

      {/* ── Product: a day in the queue ─────────────────────── */}
      <section
        id="product"
        aria-labelledby="saas-product-title"
        class="scroll-mt-20"
      >
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 sm:py-28 grid lg:grid-cols-12 gap-12">
          <div class="lg:col-span-5 space-y-5">
            <p
              class={[
                "text-xs font-semibold uppercase tracking-wider text-t-accent-ink",
              ]}
            >
              How it works
            </p>
            <h2
              id="saas-product-title"
              class={[
                "text-3xl sm:text-5xl font-bold tracking-tight text-t-bright leading-[1.05]",
                display,
              ]}
            >
              One morning in the reorder queue.
            </h2>
            <p class="text-t-text leading-relaxed max-w-md">
              Northwind is not a dashboard you check. It is a short list of
              decisions waiting for a buyer, with the arithmetic already done:
              forecast, safety stock, lead time, case pack, supplier minimum.
            </p>
            <figure class="max-w-md pt-3">
              <div class="relative">
                <DuotoneImage
                  src="/landing/saas/warehouse.webp"
                  alt="A Halvard Supply buyer in an orange vest checks the reorder queue on a tablet, standing in a long aisle of racked cartons."
                  width={1024}
                  height={768}
                  position="35% 40%"
                  class="aspect-[4/3] rounded-2xl border border-t-border"
                />
                <span
                  class={[
                    "absolute top-3 left-3 rounded-md bg-t-deep border border-t-border px-2 py-1 text-[11px] text-t-text",
                    mono,
                  ]}
                >
                  Reno DC · aisle 14 · 07:48
                </span>
              </div>
              <figcaption class="relative -mt-14 mx-3 sm:mx-5 rounded-xl bg-t-deep shadow-xl">
                <div class="rounded-xl border border-t-border bg-t-card p-5">
                  <blockquote class="text-sm text-t-text leading-relaxed">
                    “We used to rebuild the reorder sheet every Monday. Now Omar
                    approves about 30 POs from the aisle before his first coffee
                    and spends the rest of the week on suppliers.”
                  </blockquote>
                  <div class="flex items-center gap-3 mt-4">
                    <span class="w-9 h-9 rounded-full bg-t-accent text-t-on-accent grid place-items-center text-xs font-bold">
                      DK
                    </span>
                    <div class="text-xs">
                      <div class="text-t-bright font-semibold">
                        Dana Kowalczyk
                      </div>
                      <div class="text-t-muted">
                        VP Operations, Halvard Supply
                      </div>
                    </div>
                  </div>
                </div>
              </figcaption>
            </figure>
          </div>

          <ol class="lg:col-span-7">
            {STEPS.map((s, i) => (
              <li key={s.time} class="flex gap-4 sm:gap-6">
                <span
                  class={[
                    "w-12 shrink-0 text-right pt-1 text-sm text-t-muted",
                    mono,
                  ]}
                >
                  {s.time}
                </span>
                <div
                  class={[
                    "relative flex-1 border-l border-t-border pl-6 sm:pl-8 space-y-2",
                    i === STEPS.length - 1 ? "pb-0" : "pb-12",
                  ]}
                >
                  <span
                    class={[
                      "absolute -left-[7px] top-2 w-3 h-3 rounded-full ring-4 ring-t-deep",
                      i === STEPS.length - 1
                        ? "bg-t-accent"
                        : "bg-t-hover border border-t-border-active",
                    ]}
                    aria-hidden="true"
                  />
                  <h3
                    class={[
                      "text-xl sm:text-2xl font-semibold text-t-bright tracking-tight",
                      display,
                    ]}
                  >
                    {s.title}
                  </h3>
                  <p class="text-t-text leading-relaxed max-w-lg">{s.body}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      {/* ── Features ─────────────────────────────────────────── */}
      <section
        id="features"
        aria-labelledby="saas-features-title"
        class="scroll-mt-20 bg-t-surface border-y border-t-border"
      >
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 sm:py-28 space-y-12">
          <div class="flex flex-col md:flex-row md:items-end justify-between gap-6">
            <h2
              id="saas-features-title"
              class={[
                "text-3xl sm:text-5xl font-bold tracking-tight text-t-bright max-w-2xl leading-[1.05]",
                display,
              ]}
            >
              The parts of planning a spreadsheet gets wrong.
            </h2>
            <p class="text-t-muted max-w-sm leading-relaxed">
              Each one is a reason a buyer overrides the sheet by hand.
              Northwind handles them in the model instead.
            </p>
          </div>
          <FeatureBento />
        </div>
      </section>

      {/* ── Pricing ──────────────────────────────────────────── */}
      <section
        id="pricing"
        aria-labelledby="saas-pricing-title"
        class="scroll-mt-20"
      >
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 sm:py-28 space-y-12">
          <div class="text-center space-y-5 max-w-2xl mx-auto">
            <h2
              id="saas-pricing-title"
              class={[
                "text-3xl sm:text-5xl font-bold tracking-tight text-t-bright",
                display,
              ]}
            >
              Priced by SKUs, not seats.
            </h2>
            <p class="text-t-text leading-relaxed">
              Invite your whole ops team. Every plan starts with a 14-day trial
              on your own data.
            </p>
            <div
              role="group"
              aria-label="Billing period"
              class="inline-flex items-center gap-1 rounded-full border border-t-border bg-t-card p-1 text-sm"
            >
              <button
                type="button"
                aria-pressed={!annual.value}
                onClick$={() => (annual.value = false)}
                class={[
                  "px-4 py-2 rounded-full font-semibold transition-colors",
                  !annual.value
                    ? "bg-t-accent text-t-on-accent"
                    : "text-t-text hover:text-t-bright",
                ]}
              >
                Monthly
              </button>
              <button
                type="button"
                aria-pressed={annual.value}
                onClick$={() => (annual.value = true)}
                class={[
                  "px-4 py-2 rounded-full font-semibold transition-colors inline-flex items-center gap-2",
                  annual.value
                    ? "bg-t-accent text-t-on-accent"
                    : "text-t-text hover:text-t-bright",
                ]}
              >
                Annual
                <span
                  class={[
                    "text-[11px] font-semibold",
                    annual.value ? "" : "text-t-accent-ink",
                  ]}
                >
                  2 months free
                </span>
              </button>
            </div>
          </div>

          <div class="grid md:grid-cols-3 gap-5 lg:gap-0 items-stretch max-w-6xl mx-auto">
            {TIERS.map((t, i) => {
              const featured = i === 1;
              const perMonth = annual.value ? (t.monthly * 10) / 12 : t.monthly;
              return (
                <article
                  key={t.name}
                  aria-labelledby={`saas-tier-${t.name}`}
                  class={[
                    "relative rounded-2xl border p-7 flex flex-col gap-6",
                    featured
                      ? "border-t-border-active bg-t-card shadow-2xl lg:-my-4 lg:py-11 z-10 ring-1 ring-t-accent/40"
                      : "border-t-border bg-t-surface",
                    i === 0 ? "lg:rounded-r-none lg:border-r-0" : "",
                    i === 2 ? "lg:rounded-l-none lg:border-l-0" : "",
                  ]}
                >
                  {featured && (
                    <span class="absolute -top-3 left-7 rounded-full bg-t-accent text-t-on-accent px-3 py-1 text-[11px] font-semibold uppercase tracking-wider">
                      Most teams pick this
                    </span>
                  )}
                  <div class="space-y-1">
                    <h3
                      id={`saas-tier-${t.name}`}
                      class={["text-xl font-bold text-t-bright", display]}
                    >
                      {t.name}
                    </h3>
                    <p class="text-sm text-t-muted">{t.who}</p>
                  </div>
                  <div>
                    <div class="flex items-baseline gap-1.5">
                      <span
                        class={[
                          "text-5xl font-bold tracking-tight text-t-bright tabular-nums",
                          display,
                        ]}
                      >
                        {fmt(perMonth)}
                      </span>
                      <span class="text-sm text-t-muted">/ month</span>
                    </div>
                    <p class="text-xs text-t-muted mt-2 h-4" aria-live="polite">
                      {annual.value
                        ? `Billed ${fmt(t.monthly * 10)} a year — you save ${fmt(t.monthly * 2)}`
                        : `Billed monthly · ${fmt(t.monthly * 12)} a year`}
                    </p>
                  </div>
                  <a
                    href="#demo"
                    class={[
                      "text-center px-4 py-2.5 rounded-lg font-semibold text-sm transition-opacity hover:opacity-90",
                      featured
                        ? "bg-t-accent text-t-on-accent"
                        : "border border-t-border bg-t-card text-t-bright hover:bg-t-hover",
                    ]}
                  >
                    Start {t.name} trial
                  </a>
                  <ul class="space-y-2.5 text-sm">
                    {t.features.map((f) => (
                      <li key={f} class="flex gap-2.5 text-t-text">
                        <svg
                          class="w-4 h-4 mt-0.5 shrink-0 text-t-accent-ink"
                          viewBox="0 0 16 16"
                          fill="none"
                          aria-hidden="true"
                        >
                          <path
                            d="m3.5 8.5 3 3 6-7"
                            stroke="currentColor"
                            stroke-width="1.8"
                            stroke-linecap="round"
                            stroke-linejoin="round"
                          />
                        </svg>
                        {f}
                      </li>
                    ))}
                  </ul>
                </article>
              );
            })}
          </div>
          <p class="text-center text-sm text-t-muted">
            Over 50,000 SKUs or a 3PL with client-level billing?{" "}
            <a
              href="#demo"
              class="text-t-bright underline underline-offset-4 decoration-t-border-active"
            >
              Talk to us about a custom plan
            </a>
            .
          </p>
        </div>
      </section>

      {/* ── FAQ ──────────────────────────────────────────────── */}
      <section
        id="faq"
        aria-labelledby="saas-faq-title"
        class="scroll-mt-20 border-t border-t-border"
      >
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 sm:py-28 grid lg:grid-cols-12 gap-10">
          <div class="lg:col-span-4 space-y-4">
            <h2
              id="saas-faq-title"
              class={[
                "text-3xl sm:text-4xl font-bold tracking-tight text-t-bright",
                display,
              ]}
            >
              Questions buyers ask on the first call
            </h2>
            <p class="text-t-muted leading-relaxed">
              Something else? Our planning team answers at{" "}
              <span class={["text-t-text", mono]}>ops@northwind.example</span>
            </p>
          </div>
          <div class="lg:col-span-8 divide-y divide-t-border border-y border-t-border">
            {FAQS.map((f, i) => {
              const open = openFaq.value === i;
              return (
                <div key={f.q}>
                  <h3>
                    <button
                      type="button"
                      id={`saas-faq-q-${i}`}
                      aria-expanded={open}
                      aria-controls={`saas-faq-a-${i}`}
                      onClick$={() => (openFaq.value = open ? -1 : i)}
                      class="w-full flex items-center justify-between gap-6 py-5 text-left text-base sm:text-lg font-semibold text-t-bright hover:text-t-accent-ink transition-colors"
                    >
                      {f.q}
                      <span
                        class={[
                          "grid place-items-center w-7 h-7 shrink-0 rounded-full border border-t-border text-t-text transition-transform",
                          open ? "rotate-45" : "",
                        ]}
                        aria-hidden="true"
                      >
                        +
                      </span>
                    </button>
                  </h3>
                  <div
                    id={`saas-faq-a-${i}`}
                    role="region"
                    aria-labelledby={`saas-faq-q-${i}`}
                    hidden={!open}
                    class="pb-6 pr-12 text-t-text leading-relaxed"
                  >
                    {f.a}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </section>

      {/* ── Closing CTA ──────────────────────────────────────── */}
      <section
        id="demo"
        aria-labelledby="saas-cta-title"
        class="scroll-mt-20 px-4 sm:px-6 lg:px-8 pb-20"
      >
        <div class="relative overflow-hidden max-w-7xl mx-auto rounded-3xl bg-t-accent text-t-on-accent px-6 sm:px-12 py-14 sm:py-16 grid lg:grid-cols-2 gap-10 items-center">
          <div
            class="pointer-events-none absolute -right-24 -top-24 w-96 h-96 rounded-full border-[40px] border-current opacity-10"
            aria-hidden="true"
          />
          <div class="relative space-y-4">
            <h2
              id="saas-cta-title"
              class={[
                "text-3xl sm:text-5xl font-bold tracking-tight leading-[1.05]",
                display,
              ]}
              // Theme skins colour h1-h3 directly, which beats utility classes.
              style={{ color: "var(--color-t-on-accent)" }}
            >
              Send one CSV. See next month's stockouts.
            </h2>
            <p class="text-base sm:text-lg leading-relaxed max-w-lg">
              Export 13 weeks of sales and on-hand. Within two business days we
              send back a forecast and the reorder list Northwind would have
              drafted.
            </p>
          </div>
          <div class="relative">
            {sentTo.value ? (
              <div
                role="status"
                class="rounded-2xl bg-t-deep text-t-text p-6 space-y-2"
              >
                <p class="text-lg font-semibold text-t-bright">
                  Request received.
                </p>
                <p class="text-sm leading-relaxed">
                  We'll email{" "}
                  <span class="text-t-bright font-semibold">
                    {sentTo.value}
                  </span>{" "}
                  a secure upload link within one business day.
                </p>
              </div>
            ) : (
              <form
                preventdefault:submit
                noValidate
                onSubmit$={() => {
                  const v = email.value.trim();
                  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) {
                    emailError.value =
                      "Enter a work email like you@company.com";
                    return;
                  }
                  emailError.value = "";
                  sentTo.value = v;
                }}
                class="rounded-2xl bg-t-deep p-5 sm:p-6 space-y-3"
              >
                <label
                  for="saas-cta-email"
                  class="block text-sm font-semibold text-t-bright"
                >
                  Work email
                </label>
                <div class="flex flex-col sm:flex-row gap-2">
                  <input
                    id="saas-cta-email"
                    type="email"
                    autoComplete="email"
                    placeholder="you@company.com"
                    bind:value={email}
                    aria-invalid={emailError.value ? "true" : undefined}
                    aria-describedby="saas-cta-email-msg"
                    class="flex-1 min-w-0 rounded-lg border border-t-border bg-t-card px-4 py-3 text-t-bright placeholder:text-t-muted focus:outline-none focus:border-t-border-active"
                  />
                  <button
                    type="submit"
                    class="px-5 py-3 rounded-lg bg-t-accent text-t-on-accent font-semibold hover:opacity-90 transition-opacity whitespace-nowrap"
                  >
                    Get a sample forecast
                  </button>
                </div>
                <p
                  id="saas-cta-email-msg"
                  class={[
                    "text-xs",
                    emailError.value ? "text-t-error" : "text-t-muted",
                  ]}
                >
                  {emailError.value ||
                    "No card, no sales sequence. One reply from a planner."}
                </p>
              </form>
            )}
          </div>
        </div>
      </section>
    </LandingShell>
  );
});

export const head: DocumentHead = {
  title: "Northwind — SaaS Product template",
  meta: [
    {
      name: "description",
      content:
        "Northwind forecasts demand for every SKU at every location and drafts purchase orders for a buyer to approve. A Fincas storefront landing template.",
    },
  ],
};
