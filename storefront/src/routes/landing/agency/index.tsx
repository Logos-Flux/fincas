import { component$, useSignal, useStore } from "@builder.io/qwik";
import type { DocumentHead } from "@builder.io/qwik-city";
import { LandingShell } from "../../../components/landing/landing-shell";
import { Cover, type CoverId } from "../../../components/landing/agency/covers";
import { DuotoneImage } from "../../../components/landing/duotone-image";

type Discipline = "Brand" | "Digital" | "Print";

interface Photo {
  src: string;
  alt: string;
  /** CSS object-position for the crop. */
  position?: string;
}

interface Project {
  id: string;
  /** Either a token-built cover or a photograph (1024x768), never both. */
  cover?: CoverId;
  photo?: Photo;
  client: string;
  title: string;
  discipline: Discipline;
  year: number;
}

const PROJECTS: Project[] = [
  {
    id: "cidery",
    photo: {
      src: "/landing/agency/cidery.webp",
      alt: "Hollow Creek cider bottles with fresh blank labels, standing on a wooden orchard crate",
      position: "50% 60%",
    },
    client: "Hollow Creek Cidery",
    title: "A ninety-year-old orchard gets a label it can grow into",
    discipline: "Brand",
    year: 2026,
  },
  {
    id: "furniture",
    cover: "furniture",
    client: "Parcel & Pine",
    title: "Selling flat-pack oak without the flat-pack website",
    discipline: "Digital",
    year: 2025,
  },
  {
    id: "ferry",
    photo: {
      src: "/landing/agency/ferry.webp",
      alt: "A small Meridian ferry crossing a gray harbor",
    },
    client: "Meridian Ferry Co.",
    title: "A timetable you can read from the far end of the pier",
    discipline: "Print",
    year: 2025,
  },
  {
    id: "clinic",
    cover: "clinic",
    client: "Ostrander Clinic",
    title: "Booking a physio in three taps, not three phone calls",
    discipline: "Digital",
    year: 2024,
  },
  {
    id: "press",
    cover: "press",
    client: "Quarry Press",
    title: "Twelve poetry titles, one spine system",
    discipline: "Print",
    year: 2024,
  },
  {
    id: "swim",
    photo: {
      src: "/landing/agency/swim-club.webp",
      alt: "Overhead view of an ocean tidal pool with two swimmers, the club's home water",
    },
    client: "Lowtide Swim Club",
    title: "An identity for people who swim in the sea at 6am",
    discipline: "Brand",
    year: 2023,
  },
];

const FILTERS = ["All", "Brand", "Digital", "Print"] as const;
type Filter = (typeof FILTERS)[number];

/**
 * Column spans and cover shapes for the case-study grid, by position in the
 * *filtered* list, so the asymmetric rhythm holds at any filter.
 */
const LAYOUT = [
  { span: "md:col-span-7", aspect: "aspect-[4/3]", offset: "" },
  { span: "md:col-span-5", aspect: "aspect-[4/5]", offset: "md:mt-28" },
  { span: "md:col-span-5", aspect: "aspect-[4/5]", offset: "" },
  { span: "md:col-span-7", aspect: "aspect-[4/3]", offset: "md:mt-20" },
  { span: "md:col-span-8", aspect: "aspect-[16/10]", offset: "" },
  { span: "md:col-span-4", aspect: "aspect-[3/4]", offset: "md:mt-16" },
];

const SERVICES = [
  {
    name: "Brand identity",
    summary: "Names, marks, type and the rules that hold them together.",
    detail:
      "We start with the people who sell your product every day, not a mood board. Out of that comes a mark, a type pairing, a color system and a voice — tested on the unglamorous stuff first: invoices, van livery, the 404 page.",
    deliverables: [
      "Naming & verbal identity",
      "Logo & mark system",
      "Type & color",
      "Brand guidelines",
    ],
    timeline: "8–12 weeks",
    from: "$38k",
  },
  {
    name: "Websites & digital product",
    summary: "Marketing sites, storefronts and the occasional app.",
    detail:
      "Designed in the browser and built by the same people who designed it. We hand over a site your team can edit on a Tuesday afternoon without calling us, and a component library that matches the Figma file line for line.",
    deliverables: [
      "UX & content structure",
      "Interface design",
      "Front-end build",
      "Component library",
    ],
    timeline: "10–16 weeks",
    from: "$54k",
  },
  {
    name: "Editorial & print",
    summary: "Books, reports, timetables, menus — things with paper weights.",
    detail:
      "We still love a print run. Grids built for long reading, proofs on the actual stock, and press checks at 7am at the printer so the color on the shelf is the color we signed off.",
    deliverables: [
      "Book & report design",
      "Wayfinding & signage",
      "Print production",
      "Press checks",
    ],
    timeline: "4–10 weeks",
    from: "$16k",
  },
  {
    name: "Packaging",
    summary: "Labels, boxes and shelf presence for independent makers.",
    detail:
      "We design at shelf distance and at arm's length, mock up every SKU before anything goes to the converter, and plan the range so the twelfth flavor fits as well as the first.",
    deliverables: [
      "Range architecture",
      "Label & box design",
      "Dielines & mock-ups",
      "Supplier hand-off",
    ],
    timeline: "6–10 weeks",
    from: "$24k",
  },
];

const PROCESS = [
  {
    name: "Fieldwork",
    weeks: "Weeks 1–2",
    body: "We visit. Factory floors, shop counters, support inboxes. Interviews with eight to twelve people who aren't on the brief.",
    output: "A 20-page findings deck and one-sentence brief",
  },
  {
    name: "Sketch",
    weeks: "Weeks 3–5",
    body: "Three distinct directions, drawn rough and shown early. You pick one, or we argue about it until the right one is obvious.",
    output: "Three routes, one chosen",
  },
  {
    name: "Build",
    weeks: "Weeks 6–11",
    body: "The chosen route, made real across every touchpoint on the list. Weekly reviews on Thursdays, a shared board you can comment on any time.",
    output: "Final artwork, code and files",
  },
  {
    name: "Hand-off",
    weeks: "Week 12",
    body: "Guidelines, source files and a half-day training for whoever will look after it next. Then a check-in at 90 days, on us.",
    output: "Guidelines, training, 90-day review",
  },
];

const BUDGETS = [
  "Under $25k",
  "$25k – $60k",
  "$60k – $120k",
  "$120k and up",
  "Not sure yet",
];

const FACTS = [
  { k: "Founded", v: "2014" },
  { k: "Team", v: "14 people" },
  { k: "Studio", v: "Providence, RI" },
  { k: "Shipped", v: "212 projects" },
];

export default component$(() => {
  const filter = useSignal<Filter>("All");
  const openService = useSignal<number>(0);
  const form = useStore({
    name: "",
    email: "",
    budget: "",
    message: "",
    errors: {} as Partial<
      Record<"name" | "email" | "budget" | "message", string>
    >,
    sent: false,
  });

  const shown = PROJECTS.filter(
    (p) => filter.value === "All" || p.discipline === filter.value,
  );

  const label =
    "block font-[family-name:var(--font-mono)] text-[11px] uppercase tracking-[0.2em] text-t-muted mb-2";
  const field =
    "w-full bg-transparent border-0 border-b-2 border-t-border px-0 py-3 text-lg text-t-bright placeholder:text-t-muted focus:outline-none focus:border-t-accent transition-colors";

  return (
    <LandingShell
      brand="Fieldwork Studio"
      mark="Fw"
      links={[
        { label: "Work", href: "#work" },
        { label: "Services", href: "#services" },
        { label: "Process", href: "#process" },
        { label: "Contact", href: "#contact" },
      ]}
      cta={{ label: "Start a project", href: "#contact" }}
      footer={{
        Studio: ["About", "Team", "Jobs (2 open)"],
        Work: ["Brand", "Digital", "Print"],
        Elsewhere: ["Newsletter", "Instagram", "Are.na"],
      }}
      tagline="An independent brand and digital design studio in Providence, Rhode Island. Fourteen people, one long table."
    >
      {/* ---------------------------------------------------------- Hero */}
      <section
        aria-labelledby="agency-hero"
        class="relative overflow-hidden border-b border-t-border"
      >
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pt-14 sm:pt-20 lg:pt-24 pb-12 sm:pb-16">
          <div class="flex items-center justify-between gap-4 font-[family-name:var(--font-mono)] text-[11px] uppercase tracking-[0.25em] text-t-muted">
            <span>Fieldwork Studio — Index</span>
            <span class="hidden sm:inline">Booking Q1 2027</span>
          </div>

          <h1
            id="agency-hero"
            class="mt-8 sm:mt-10 font-[family-name:var(--font-display)] font-black text-t-bright text-[clamp(3rem,10.5vw,10rem)] leading-[0.86] tracking-[-0.055em]"
          >
            <span class="block">We design</span>
            <span class="block sm:pl-[12%]">brands people</span>
            <span class="block">
              actually <em class="italic font-black text-t-accent-ink">use.</em>
            </span>
          </h1>

          <div class="mt-12 sm:mt-16 grid grid-cols-1 md:grid-cols-12 gap-10 md:gap-6 items-end">
            <p class="md:col-span-5 md:col-start-1 text-lg sm:text-xl leading-relaxed text-t-text">
              Identity, websites and print for independent companies that make
              real things — cider, ferries, furniture, physiotherapy. Designed
              for the counter and the inbox, not the awards jury.
            </p>
            <dl class="md:col-span-6 md:col-start-7 grid grid-cols-2 sm:grid-cols-4 border-t border-t-border">
              {FACTS.map((f, i) => (
                <div
                  key={f.k}
                  class={[
                    "pt-4 pb-1 pr-4",
                    i > 0 ? "sm:border-l sm:border-t-border sm:pl-4" : "",
                    i % 2 === 1 ? "border-l border-t-border pl-4" : "",
                  ]}
                >
                  <dt class="font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.2em] text-t-muted">
                    {f.k}
                  </dt>
                  <dd class="mt-1.5 text-t-bright font-semibold">{f.v}</dd>
                </div>
              ))}
            </dl>
          </div>

          <div class="mt-14 flex flex-wrap items-center gap-x-8 gap-y-4">
            <a
              href="#work"
              class="group inline-flex items-center gap-3 text-t-bright font-semibold text-lg"
            >
              <span class="grid place-items-center w-12 h-12 rounded-full bg-t-accent text-t-on-accent transition-transform group-hover:translate-y-1">
                <svg
                  aria-hidden="true"
                  viewBox="0 0 24 24"
                  class="w-5 h-5"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                >
                  <path d="M12 5v14M6 13l6 6 6-6" />
                </svg>
              </span>
              See selected work
            </a>
            <a
              href="#contact"
              class="text-t-text underline decoration-t-border-active underline-offset-[6px] hover:text-t-bright"
            >
              or tell us about yours
            </a>
          </div>

          <figure class="mt-16 sm:mt-20 grid grid-cols-1 md:grid-cols-12 gap-4 md:gap-6 items-end">
            <figcaption class="order-2 md:order-1 md:col-span-3 font-[family-name:var(--font-mono)] text-[11px] uppercase leading-relaxed tracking-[0.2em] text-t-muted">
              <span class="block text-t-bright">The long table</span>
              Third floor, Westminster St. Proofs go up on the wall before they
              go to the client.
            </figcaption>
            <DuotoneImage
              src="/landing/agency/studio.webp"
              alt="Inside the Fieldwork studio: two designers at a worktable covered in printed proofs, in front of a wall of color swatches and cut-paper shapes"
              width={1440}
              height={800}
              position="50% 45%"
              class="order-1 md:order-2 md:col-span-9 aspect-[4/3] sm:aspect-[16/8] rounded-sm"
            />
          </figure>
        </div>
      </section>

      {/* ---------------------------------------------------------- Work */}
      <section
        id="work"
        aria-labelledby="agency-work"
        class="scroll-mt-20 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 sm:py-28"
      >
        <div class="flex flex-col lg:flex-row lg:items-end justify-between gap-8">
          <div>
            <p class="font-[family-name:var(--font-mono)] text-[11px] uppercase tracking-[0.25em] text-t-accent-ink">
              (01) Selected work
            </p>
            <h2
              id="agency-work"
              class="mt-4 font-[family-name:var(--font-display)] text-t-bright font-black text-[clamp(2.25rem,5.5vw,4.5rem)] leading-[0.92] tracking-[-0.04em] max-w-3xl"
            >
              Six from the last four years.
            </h2>
          </div>
          <div
            role="group"
            aria-label="Filter projects by discipline"
            class="flex flex-wrap gap-2"
          >
            {FILTERS.map((f) => {
              const count =
                f === "All"
                  ? PROJECTS.length
                  : PROJECTS.filter((p) => p.discipline === f).length;
              const on = filter.value === f;
              return (
                <button
                  key={f}
                  type="button"
                  aria-pressed={on}
                  onClick$={() => (filter.value = f)}
                  class={[
                    "px-4 py-2 rounded-full border text-sm font-semibold transition-colors",
                    on
                      ? "bg-t-bright text-t-deep border-t-bright"
                      : "border-t-border text-t-text hover:border-t-border-active hover:text-t-bright",
                  ]}
                >
                  {f}
                  <sup class="ml-1 font-[family-name:var(--font-mono)] text-[10px]">
                    {count}
                  </sup>
                </button>
              );
            })}
          </div>
        </div>

        <p class="sr-only" aria-live="polite">
          Showing {shown.length} {shown.length === 1 ? "project" : "projects"}
        </p>

        <ul class="mt-12 sm:mt-16 grid grid-cols-1 md:grid-cols-12 gap-x-6 gap-y-14 md:gap-y-10">
          {shown.map((p, i) => {
            const l = LAYOUT[i % LAYOUT.length];
            return (
              <li key={p.id} class={[l.span, l.offset]}>
                <a href="#contact" class="group block">
                  <div
                    class={[
                      "relative overflow-hidden rounded-sm border border-t-border transition-transform duration-500 group-hover:-translate-y-1",
                      l.aspect,
                    ]}
                  >
                    {p.photo ? (
                      <DuotoneImage
                        src={p.photo.src}
                        alt={p.photo.alt}
                        width={1024}
                        height={768}
                        position={p.photo.position}
                        class="h-full w-full transition-transform duration-700 group-hover:scale-[1.03]"
                      />
                    ) : (
                      p.cover && <Cover id={p.cover} />
                    )}
                  </div>
                  <div class="mt-4 grid grid-cols-[auto_1fr_auto] items-baseline gap-x-4">
                    <span class="font-[family-name:var(--font-mono)] text-xs text-t-muted tabular-nums">
                      {String(PROJECTS.indexOf(p) + 1).padStart(2, "0")}
                    </span>
                    <h3 class="text-t-bright font-semibold text-lg leading-snug">
                      {p.client}
                    </h3>
                    <span class="font-[family-name:var(--font-mono)] text-xs text-t-muted tabular-nums">
                      {p.year}
                    </span>
                    <span />
                    <p class="text-t-text leading-snug mt-1 group-hover:text-t-bright transition-colors">
                      {p.title}
                    </p>
                    <span class="mt-1 text-xs font-semibold uppercase tracking-[0.15em] text-t-accent-ink">
                      {p.discipline}
                    </span>
                  </div>
                </a>
              </li>
            );
          })}
        </ul>
      </section>

      {/* ------------------------------------------------------ Services */}
      <section
        id="services"
        aria-labelledby="agency-services"
        class="scroll-mt-20 bg-t-surface border-y border-t-border"
      >
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 sm:py-28 grid grid-cols-1 lg:grid-cols-12 gap-12">
          <div class="lg:col-span-4">
            <div class="lg:sticky lg:top-28">
              <p class="font-[family-name:var(--font-mono)] text-[11px] uppercase tracking-[0.25em] text-t-accent-ink">
                (02) Services
              </p>
              <h2
                id="agency-services"
                class="mt-4 font-[family-name:var(--font-display)] text-t-bright font-black text-[clamp(2.25rem,5vw,4rem)] leading-[0.92] tracking-[-0.04em]"
              >
                Four things, done properly.
              </h2>
              <p class="mt-6 text-t-text leading-relaxed max-w-sm">
                We take on nine or ten projects a year, so each one gets a
                partner, a lead designer and a developer from day one to launch.
              </p>
            </div>
          </div>

          <ol class="lg:col-span-8 border-t border-t-border">
            {SERVICES.map((s, i) => {
              const open = openService.value === i;
              const panelId = `agency-service-${i}`;
              return (
                <li key={s.name} class="border-b border-t-border">
                  <h3>
                    <button
                      type="button"
                      aria-expanded={open}
                      aria-controls={panelId}
                      onClick$={() => (openService.value = open ? -1 : i)}
                      class="group w-full grid grid-cols-[3rem_1fr_auto] sm:grid-cols-[5rem_1fr_auto] items-baseline gap-x-3 py-7 sm:py-8 text-left"
                    >
                      <span class="font-[family-name:var(--font-mono)] text-sm text-t-muted tabular-nums">
                        {String(i + 1).padStart(2, "0")}
                      </span>
                      <span>
                        <span
                          class={[
                            "block font-[family-name:var(--font-display)] font-bold text-[clamp(1.5rem,3.4vw,2.75rem)] leading-[1.02] tracking-[-0.03em] transition-colors",
                            open
                              ? "text-t-accent-ink"
                              : "text-t-bright group-hover:text-t-accent-ink",
                          ]}
                        >
                          {s.name}
                        </span>
                        <span class="block mt-2 text-t-muted text-base font-normal">
                          {s.summary}
                        </span>
                      </span>
                      <span
                        aria-hidden="true"
                        class={[
                          "grid place-items-center w-10 h-10 rounded-full border border-t-border text-t-bright text-xl transition-transform duration-300",
                          open ? "rotate-45 bg-t-accent text-t-on-accent" : "",
                        ]}
                      >
                        +
                      </span>
                    </button>
                  </h3>
                  <div
                    id={panelId}
                    hidden={!open}
                    class="pb-10 pl-[3.75rem] sm:pl-[5.75rem] pr-2"
                  >
                    <p class="text-t-text leading-relaxed max-w-2xl">
                      {s.detail}
                    </p>
                    <div class="mt-6 grid grid-cols-1 sm:grid-cols-3 gap-6">
                      <ul class="sm:col-span-2 grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm text-t-text">
                        {s.deliverables.map((d) => (
                          <li key={d} class="flex items-center gap-2">
                            <span class="w-1.5 h-1.5 rounded-full bg-t-accent shrink-0" />
                            {d}
                          </li>
                        ))}
                      </ul>
                      <dl class="text-sm space-y-2 sm:border-l sm:border-t-border sm:pl-6">
                        <div class="flex justify-between gap-4">
                          <dt class="text-t-muted">Typical</dt>
                          <dd class="text-t-bright font-semibold">
                            {s.timeline}
                          </dd>
                        </div>
                        <div class="flex justify-between gap-4">
                          <dt class="text-t-muted">From</dt>
                          <dd class="text-t-bright font-semibold">{s.from}</dd>
                        </div>
                      </dl>
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      </section>

      {/* ------------------------------------------------------- Process */}
      <section
        id="process"
        aria-labelledby="agency-process"
        class="scroll-mt-20 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 sm:py-28"
      >
        <div class="grid grid-cols-1 md:grid-cols-12 gap-6 items-end">
          <div class="md:col-span-7">
            <p class="font-[family-name:var(--font-mono)] text-[11px] uppercase tracking-[0.25em] text-t-accent-ink">
              (03) Process
            </p>
            <h2
              id="agency-process"
              class="mt-4 font-[family-name:var(--font-display)] text-t-bright font-black text-[clamp(2.25rem,5.5vw,4.5rem)] leading-[0.92] tracking-[-0.04em]"
            >
              Twelve weeks, four stages, no surprises.
            </h2>
          </div>
          <p class="md:col-span-4 md:col-start-9 text-t-text leading-relaxed">
            A typical identity project, start to hand-off. Websites run longer
            in the Build stage; the shape stays the same.
          </p>
        </div>

        <ol class="mt-14 sm:mt-20 relative grid grid-cols-1 md:grid-cols-4 gap-0">
          <div
            aria-hidden="true"
            class="hidden md:block absolute left-0 right-0 top-[7px] h-px bg-t-border"
          />
          <div
            aria-hidden="true"
            class="md:hidden absolute top-2 bottom-2 left-[7px] w-px bg-t-border"
          />
          {PROCESS.map((p, i) => (
            <li
              key={p.name}
              class="relative pl-10 md:pl-0 md:pr-8 pb-12 md:pb-0"
            >
              <span
                aria-hidden="true"
                class={[
                  "absolute left-0 md:static block w-[15px] h-[15px] rounded-full border-2 border-t-accent",
                  i === 0 ? "bg-t-accent" : "bg-t-deep",
                ]}
              />
              <p class="md:mt-8 font-[family-name:var(--font-mono)] text-[11px] uppercase tracking-[0.2em] text-t-muted">
                {p.weeks}
              </p>
              <h3 class="mt-2 flex items-baseline gap-3 font-[family-name:var(--font-display)] text-t-bright font-bold text-3xl tracking-[-0.03em]">
                <span class="text-t-accent-ink text-base font-[family-name:var(--font-mono)] tracking-normal">
                  {String(i + 1).padStart(2, "0")}
                </span>
                {p.name}
              </h3>
              <p class="mt-4 text-t-text leading-relaxed">{p.body}</p>
              <p class="mt-5 pt-4 border-t border-dashed border-t-border text-sm text-t-muted">
                <span class="text-t-bright font-semibold">You get:</span>{" "}
                {p.output}
              </p>
            </li>
          ))}
        </ol>
      </section>

      {/* ------------------------------------------------------- Contact */}
      <section
        id="contact"
        aria-labelledby="agency-contact"
        class="scroll-mt-20 bg-t-surface border-t border-t-border"
      >
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 sm:py-28 grid grid-cols-1 lg:grid-cols-12 gap-14">
          <div class="lg:col-span-5">
            <p class="font-[family-name:var(--font-mono)] text-[11px] uppercase tracking-[0.25em] text-t-accent-ink">
              (04) Contact
            </p>
            <h2
              id="agency-contact"
              class="mt-4 font-[family-name:var(--font-display)] text-t-bright font-black text-[clamp(2.5rem,6vw,5rem)] leading-[0.88] tracking-[-0.05em]"
            >
              Got something that needs making?
            </h2>
            <p class="mt-6 text-t-text leading-relaxed max-w-md">
              Tell us a little about it. A partner reads every inquiry and
              replies within two working days — including the ones we can't take
              on.
            </p>
            <dl class="mt-10 space-y-5 text-sm">
              <div>
                <dt class="font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.2em] text-t-muted">
                  New work
                </dt>
                <dd class="mt-1 text-t-bright text-lg font-semibold">
                  hello@fieldwork.example
                </dd>
              </div>
              <div>
                <dt class="font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.2em] text-t-muted">
                  Studio
                </dt>
                <dd class="mt-1 text-t-text">
                  3rd floor, 220 Westminster St
                  <br />
                  Providence, RI 02903
                </dd>
              </div>
            </dl>
          </div>

          <div class="lg:col-span-6 lg:col-start-7">
            {form.sent ? (
              <div
                role="status"
                class="h-full flex flex-col justify-center border-t-2 border-t-accent pt-10"
              >
                <p class="font-[family-name:var(--font-mono)] text-[11px] uppercase tracking-[0.25em] text-t-success">
                  Received
                </p>
                <p class="mt-4 font-[family-name:var(--font-display)] text-t-bright font-black text-4xl sm:text-5xl leading-[0.95] tracking-[-0.04em]">
                  Thanks, {form.name.split(" ")[0]}.
                </p>
                <p class="mt-5 text-t-text leading-relaxed max-w-md">
                  We'll reply to{" "}
                  <span class="text-t-bright font-semibold">{form.email}</span>{" "}
                  by the end of the second working day. If it's a fit, the next
                  step is a 45-minute call with two of us.
                </p>
                <button
                  type="button"
                  class="mt-8 self-start text-sm text-t-text underline underline-offset-4 decoration-t-border-active hover:text-t-bright"
                  onClick$={() => {
                    form.name = "";
                    form.email = "";
                    form.budget = "";
                    form.message = "";
                    form.errors = {};
                    form.sent = false;
                  }}
                >
                  Send another inquiry
                </button>
              </div>
            ) : (
              <form
                noValidate
                preventdefault:submit
                onSubmit$={() => {
                  const e: typeof form.errors = {};
                  if (form.name.trim().length < 2)
                    e.name = "Tell us who we're talking to.";
                  if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(form.email.trim()))
                    e.email = "That email doesn't look right.";
                  if (!form.budget)
                    e.budget = "Pick a range, even a rough one.";
                  if (form.message.trim().length < 20)
                    e.message =
                      "A couple of sentences, please (20 characters or more).";
                  form.errors = e;
                  if (Object.keys(e).length === 0) form.sent = true;
                }}
                class="space-y-9"
              >
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-9">
                  <div>
                    <label for="agency-name" class={label}>
                      Your name
                    </label>
                    <input
                      id="agency-name"
                      name="name"
                      autoComplete="name"
                      value={form.name}
                      onInput$={(_, el) => (form.name = el.value)}
                      aria-invalid={!!form.errors.name}
                      aria-describedby={
                        form.errors.name ? "agency-name-err" : undefined
                      }
                      placeholder="Ada Okafor"
                      class={field}
                    />
                    {form.errors.name && (
                      <p id="agency-name-err" class="mt-2 text-sm text-t-error">
                        {form.errors.name}
                      </p>
                    )}
                  </div>
                  <div>
                    <label for="agency-email" class={label}>
                      Email
                    </label>
                    <input
                      id="agency-email"
                      name="email"
                      type="email"
                      autoComplete="email"
                      value={form.email}
                      onInput$={(_, el) => (form.email = el.value)}
                      aria-invalid={!!form.errors.email}
                      aria-describedby={
                        form.errors.email ? "agency-email-err" : undefined
                      }
                      placeholder="ada@company.com"
                      class={field}
                    />
                    {form.errors.email && (
                      <p
                        id="agency-email-err"
                        class="mt-2 text-sm text-t-error"
                      >
                        {form.errors.email}
                      </p>
                    )}
                  </div>
                </div>

                <div>
                  <label for="agency-budget" class={label}>
                    Budget
                  </label>
                  <select
                    id="agency-budget"
                    name="budget"
                    onChange$={(_, el) => (form.budget = el.value)}
                    aria-invalid={!!form.errors.budget}
                    aria-describedby={
                      form.errors.budget ? "agency-budget-err" : undefined
                    }
                    class={[field, "cursor-pointer bg-t-surface"]}
                  >
                    <option value="" disabled selected={!form.budget}>
                      Choose a range
                    </option>
                    {BUDGETS.map((b) => (
                      <option key={b} value={b} selected={form.budget === b}>
                        {b}
                      </option>
                    ))}
                  </select>
                  {form.errors.budget && (
                    <p id="agency-budget-err" class="mt-2 text-sm text-t-error">
                      {form.errors.budget}
                    </p>
                  )}
                </div>

                <div>
                  <label for="agency-message" class={label}>
                    The project
                  </label>
                  <textarea
                    id="agency-message"
                    name="message"
                    rows={4}
                    value={form.message}
                    onInput$={(_, el) => (form.message = el.value)}
                    aria-invalid={!!form.errors.message}
                    aria-describedby={
                      form.errors.message ? "agency-message-err" : undefined
                    }
                    placeholder="What are you making, and what's not working about how it looks today?"
                    class={[field, "resize-y min-h-28"]}
                  />
                  {form.errors.message && (
                    <p
                      id="agency-message-err"
                      class="mt-2 text-sm text-t-error"
                    >
                      {form.errors.message}
                    </p>
                  )}
                </div>

                <div class="flex flex-wrap items-center gap-6">
                  <button
                    type="submit"
                    class="inline-flex items-center gap-3 px-7 py-4 rounded-full bg-t-accent text-t-on-accent font-semibold text-lg hover:opacity-90 transition-opacity"
                  >
                    Send inquiry
                    <span aria-hidden="true">→</span>
                  </button>
                  <p class="text-sm text-t-muted">
                    No mailing list. We only use this to reply.
                  </p>
                </div>
              </form>
            )}
          </div>
        </div>
      </section>
    </LandingShell>
  );
});

export const head: DocumentHead = {
  title: "Fieldwork Studio — Creative Agency template",
  meta: [
    {
      name: "description",
      content:
        "Landing template for an independent brand and digital design studio: oversized editorial type, a filterable case-study grid, services, a four-stage process and a contact form.",
    },
  ],
};
