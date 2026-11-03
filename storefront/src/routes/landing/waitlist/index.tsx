import { $, component$, useSignal, useStyles$ } from "@builder.io/qwik";
import type { DocumentHead } from "@builder.io/qwik-city";
import { DuotoneImage } from "../../../components/landing/duotone-image";
import { LandingShell } from "../../../components/landing/landing-shell";

/*
 * Halcyon — a pre-launch email client that delivers mail in three batches a
 * day. The quietest template: one atmospheric screen, then two short bands.
 * Keyframes live here (global, prefixed) because Tailwind's arbitrary
 * `animate-[…]` needs a named keyframe and global.css is shared.
 */
const KEYFRAMES = `
@keyframes halcyon-drift-a {
  0%, 100% { transform: translate3d(0, 0, 0) scale(1); }
  50% { transform: translate3d(6%, 4%, 0) scale(1.08); }
}
@keyframes halcyon-drift-b {
  0%, 100% { transform: translate3d(0, 0, 0) scale(1.05); }
  50% { transform: translate3d(-7%, -3%, 0) scale(0.95); }
}
@keyframes halcyon-breathe {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.04); }
}
@keyframes halcyon-tide {
  from { transform: translateX(0); }
  to { transform: translateX(-50%); }
}
`;

const BASE_COUNT = 3412;
const DELIVERIES = [
  { time: "08:00", label: "Morning" },
  { time: "12:30", label: "Midday" },
  { time: "17:00", label: "Evening" },
];

/** Small, stable string hash so the same email always gets the same spot. */
function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

export default component$(() => {
  useStyles$(KEYFRAMES);

  const email = useSignal("");
  const error = useSignal("");
  const position = useSignal(0);
  const code = useSignal("");
  const copied = useSignal(false);

  const submit = $(() => {
    const value = email.value.trim().toLowerCase();
    if (!value) {
      error.value = "Enter an email address so we can save your spot.";
      return;
    }
    if (!EMAIL_RE.test(value)) {
      error.value = "That doesn't look like a full address — check for a typo.";
      return;
    }
    error.value = "";
    const h = hash(value);
    position.value = BASE_COUNT + 1 + (h % 37);
    code.value = h.toString(36).slice(0, 6).padStart(6, "q");
  });

  const copy = $(async () => {
    const link = `https://halcyon.example/r/${code.value}`;
    try {
      await navigator.clipboard.writeText(link);
      copied.value = true;
    } catch {
      const field = document.getElementById(
        "halcyon-ref",
      ) as HTMLInputElement | null;
      field?.select();
      copied.value = false;
    }
    setTimeout(() => (copied.value = false), 2200);
  });

  const joined = position.value > 0;
  const waiting = joined ? position.value : BASE_COUNT;

  return (
    <LandingShell
      brand="Halcyon"
      mark="H"
      links={[
        { label: "Why", href: "#why" },
        { label: "Note", href: "#note" },
        { label: "Join", href: "#join" },
      ]}
      cta={{ label: "Join the waitlist", href: "#join" }}
      footer={{ Halcyon: ["Privacy", "Press kit", "hello@halcyon"] }}
      tagline="Email that arrives three times a day, not three hundred. Launching spring 2027."
    >
      {/* ── Hero ─────────────────────────────────────────────── */}
      <section
        aria-labelledby="hero-title"
        class="relative isolate overflow-hidden min-h-[calc(100svh-6.75rem)] flex items-center"
      >
        {/* Ambient background: drifting token-coloured fields + horizon. */}
        <div aria-hidden="true" class="absolute inset-0 -z-10">
          {/* Still water at dawn, kept faint: a texture, not a picture. */}
          <div class="absolute inset-0 opacity-30 [mask-image:linear-gradient(to_bottom,transparent,black_35%,black_75%,transparent)]">
            <DuotoneImage
              src="/landing/waitlist/still-water.webp"
              alt=""
              width={1440}
              height={800}
              loading="eager"
              position="50% 55%"
              class="h-full w-full"
            />
          </div>
          {/* A pool of canvas behind the copy so the form stays crisp. */}
          <div class="absolute inset-0 bg-[radial-gradient(ellipse_38%_48%_at_50%_52%,var(--color-t-deep),transparent)] opacity-70" />
          <div
            class="absolute -top-1/4 -left-1/4 w-[80vw] h-[80vw] max-w-[60rem] max-h-[60rem] rounded-full blur-3xl motion-safe:animate-[halcyon-drift-a_26s_ease-in-out_infinite]"
            style={{
              background:
                "radial-gradient(circle at center, var(--color-t-accent-glow), transparent 65%)",
            }}
          />
          <div
            class="absolute -bottom-1/3 -right-1/4 w-[70vw] h-[70vw] max-w-[52rem] max-h-[52rem] rounded-full blur-3xl opacity-60 motion-safe:animate-[halcyon-drift-b_32s_ease-in-out_infinite]"
            style={{
              background:
                "radial-gradient(circle at center, color-mix(in oklab, var(--color-t-accent-alt) 28%, transparent), transparent 62%)",
            }}
          />
          {/* Concentric rings, like ripples on still water. */}
          <svg
            class="absolute left-1/2 top-[58%] -translate-x-1/2 -translate-y-1/2 w-[140vw] max-w-[110rem] text-t-border-active motion-safe:animate-[halcyon-breathe_12s_ease-in-out_infinite] opacity-30"
            viewBox="0 0 800 800"
            fill="none"
          >
            {[120, 200, 280, 360].map((r) => (
              <circle
                key={r}
                cx="400"
                cy="400"
                r={r}
                stroke="currentColor"
                stroke-width="1"
              />
            ))}
          </svg>
          {/* Slow tide line along the bottom edge. */}
          <div class="absolute bottom-0 inset-x-0 h-24 overflow-hidden">
            <svg
              class="absolute bottom-0 left-0 h-24 w-[200%] text-t-accent/10 motion-safe:animate-[halcyon-tide_40s_linear_infinite]"
              viewBox="0 0 2400 96"
              preserveAspectRatio="none"
              fill="currentColor"
            >
              <path d="M0 60 C 200 30 400 30 600 60 S 1000 90 1200 60 S 1600 30 1800 60 S 2200 90 2400 60 V96 H0 Z" />
            </svg>
          </div>
          <div class="absolute inset-0 bg-[linear-gradient(to_bottom,transparent_60%,var(--color-t-deep))]" />
        </div>

        <div class="w-full max-w-3xl mx-auto px-5 sm:px-8 py-20 text-center">
          <p class="inline-flex items-center gap-2 rounded-full border border-t-border bg-t-deep/60 backdrop-blur px-3.5 py-1.5 text-xs text-t-muted font-[family-name:var(--font-mono)]">
            <span class="relative flex w-1.5 h-1.5">
              <span class="absolute inset-0 rounded-full bg-t-accent motion-safe:animate-ping opacity-60" />
              <span class="relative w-1.5 h-1.5 rounded-full bg-t-accent" />
            </span>
            Private beta · spring 2027
          </p>

          <h1
            id="hero-title"
            class="mt-8 font-[family-name:var(--font-display)] text-t-bright text-[2.6rem] leading-[1.05] sm:text-6xl md:text-7xl font-semibold tracking-tight text-balance"
          >
            Your inbox, three times a day.
          </h1>
          <p class="mt-6 text-base sm:text-lg text-t-text max-w-xl mx-auto leading-relaxed text-pretty">
            Halcyon holds your email and delivers it in calm batches at 8:00,
            12:30 and 17:00. Anything truly urgent still gets through. Nothing
            else gets to tap you on the shoulder.
          </p>

          <div id="join" class="mt-10 max-w-md mx-auto scroll-mt-28">
            {!joined ? (
              <form
                preventdefault:submit
                onSubmit$={submit}
                noValidate
                class="text-left"
              >
                <label
                  for="halcyon-email"
                  class="block text-sm font-medium text-t-bright mb-2"
                >
                  Your email
                </label>
                <div class="flex flex-col sm:flex-row gap-2 rounded-2xl sm:rounded-full border border-t-border bg-t-card/80 backdrop-blur p-1.5 focus-within:border-t-border-active transition-colors">
                  <input
                    id="halcyon-email"
                    type="email"
                    autocomplete="email"
                    placeholder="you@company.com"
                    aria-invalid={error.value ? "true" : undefined}
                    aria-describedby={error.value ? "halcyon-error" : undefined}
                    bind:value={email}
                    class="flex-1 min-w-0 bg-transparent px-4 py-3 text-t-bright placeholder:text-t-muted outline-none"
                  />
                  <button
                    type="submit"
                    class="rounded-full bg-t-accent text-t-on-accent px-6 py-3 text-sm font-semibold hover:opacity-90 transition-opacity focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-t-accent"
                  >
                    Save my spot
                  </button>
                </div>
                {error.value ? (
                  <p
                    id="halcyon-error"
                    role="alert"
                    class="mt-2 text-sm text-t-error"
                  >
                    {error.value}
                  </p>
                ) : (
                  <p class="mt-2 text-xs text-t-muted">
                    One email when your invite is ready. No newsletter.
                  </p>
                )}
              </form>
            ) : (
              <div
                role="status"
                class="rounded-3xl border border-t-border-active bg-t-card/85 backdrop-blur p-6 text-left"
              >
                <p class="text-xs uppercase tracking-[0.2em] text-t-muted font-[family-name:var(--font-mono)]">
                  You're in
                </p>
                <p class="mt-2 font-[family-name:var(--font-display)] text-3xl sm:text-4xl text-t-bright font-semibold">
                  You're #{position.value.toLocaleString("en-US")} on the list
                </p>
                <p class="mt-3 text-sm text-t-text leading-relaxed">
                  Every friend who joins with your link moves you up 25 places.
                  Invites go out in waves of 500.
                </p>
                <label
                  for="halcyon-ref"
                  class="mt-5 block text-xs font-medium text-t-muted"
                >
                  Your referral link
                </label>
                <div class="mt-1.5 flex gap-2">
                  <input
                    id="halcyon-ref"
                    readOnly
                    value={`https://halcyon.example/r/${code.value}`}
                    class="flex-1 min-w-0 rounded-xl border border-t-border bg-t-deep px-3 py-2.5 text-sm text-t-bright font-[family-name:var(--font-mono)] outline-none focus:border-t-border-active"
                  />
                  <button
                    type="button"
                    onClick$={copy}
                    class="shrink-0 rounded-xl border border-t-border-active px-4 text-sm font-semibold text-t-bright hover:bg-t-hover transition-colors"
                  >
                    {copied.value ? "Copied" : "Copy"}
                  </button>
                </div>
                <p class="sr-only" aria-live="polite">
                  {copied.value ? "Referral link copied to clipboard" : ""}
                </p>
              </div>
            )}
          </div>

          <div class="mt-10 flex items-center justify-center gap-3 text-sm text-t-muted">
            <div class="flex -space-x-1" aria-hidden="true">
              {["M", "J", "R", "T"].map((i, n) => (
                <span
                  key={i}
                  class={[
                    "grid place-items-center w-7 h-7 rounded-full ring-2 ring-t-deep text-[11px] font-bold",
                    n % 2 === 0
                      ? "bg-t-accent text-t-on-accent"
                      : "bg-t-hover text-t-bright",
                  ]}
                >
                  {i}
                </span>
              ))}
            </div>
            <p>
              <span class="text-t-bright font-semibold tabular-nums">
                {waiting.toLocaleString("en-US")}
              </span>{" "}
              people waiting for a quieter inbox
            </p>
          </div>
        </div>
      </section>

      {/* ── Why ─────────────────────────────────────────────── */}
      <section
        id="why"
        aria-labelledby="why-title"
        class="scroll-mt-16 border-t border-t-border"
      >
        <div class="max-w-5xl mx-auto px-5 sm:px-8 py-20 sm:py-24">
          <div class="flex flex-col md:flex-row md:items-end md:justify-between gap-6">
            <h2
              id="why-title"
              class="font-[family-name:var(--font-display)] text-t-bright text-2xl sm:text-3xl font-semibold tracking-tight max-w-sm"
            >
              Fewer arrivals. Same amount of mail.
            </h2>
            {/* Delivery schedule strip */}
            <ol
              aria-label="Default delivery times"
              class="flex items-center gap-2 font-[family-name:var(--font-mono)] text-xs"
            >
              {DELIVERIES.map((d, i) => (
                <li key={d.time} class="flex items-center gap-2">
                  <span class="whitespace-nowrap rounded-full border border-t-border px-3 py-1.5 text-t-text">
                    <span class="text-t-accent-ink font-semibold">
                      {d.time}
                    </span>{" "}
                    <span class="hidden sm:inline text-t-muted">{d.label}</span>
                  </span>
                  {i < DELIVERIES.length - 1 && (
                    <span
                      aria-hidden="true"
                      class="hidden sm:block w-6 h-px bg-t-border-active"
                    />
                  )}
                </li>
              ))}
            </ol>
          </div>

          <dl class="mt-14 grid md:grid-cols-3 gap-x-10 gap-y-10">
            {[
              {
                n: "01",
                t: "Batched delivery",
                d: "Mail lands in three scheduled drops. Between them, the inbox is genuinely empty — no badge, no count, no preview text.",
              },
              {
                n: "02",
                t: "A short list of who can interrupt",
                d: "Pick up to five people. Their messages skip the queue. In beta, people chose an average of 2.3.",
              },
              {
                n: "03",
                t: "Replies that wait for you",
                d: "Write when you like; Halcyon sends at the next drop, so a 23:40 answer doesn't start a 23:41 thread.",
              },
            ].map((h) => (
              <div key={h.n} class="border-t border-t-border-active pt-5">
                <dt>
                  <span class="block text-xs text-t-muted font-[family-name:var(--font-mono)]">
                    {h.n}
                  </span>
                  <span class="mt-2 block text-lg font-semibold text-t-bright">
                    {h.t}
                  </span>
                </dt>
                <dd class="mt-2 text-sm text-t-text leading-relaxed">{h.d}</dd>
              </div>
            ))}
          </dl>
        </div>
      </section>

      {/* ── Founder note ─────────────────────────────────────── */}
      <section
        id="note"
        aria-labelledby="note-title"
        class="scroll-mt-16 bg-t-surface border-t border-t-border"
      >
        <div class="max-w-2xl mx-auto px-5 sm:px-8 py-20 sm:py-24">
          <h2
            id="note-title"
            class="text-xs uppercase tracking-[0.25em] text-t-muted font-[family-name:var(--font-mono)]"
          >
            A note from the founder
          </h2>
          <div class="mt-6 space-y-5 font-[family-name:var(--font-display)] text-xl sm:text-2xl leading-snug text-t-bright">
            <p>
              Two years ago I counted: I checked email 71 times on an ordinary
              Tuesday, and answered nine messages.
            </p>
          </div>
          <div class="mt-6 space-y-4 text-t-text leading-relaxed">
            <p>
              The other 62 checks were just in case. Filters didn't fix it, and
              neither did turning notifications off — I kept looking anyway,
              because something might have arrived.
            </p>
            <p>
              Halcyon removes the "might". You know exactly when mail comes, so
              you can stop watching for it. We're four people, we're funded
              through 2028, and we'll never show you an ad.
            </p>
          </div>
          <div class="mt-8 flex items-center gap-3">
            <span class="grid place-items-center w-10 h-10 rounded-full bg-t-accent text-t-on-accent text-sm font-bold">
              IV
            </span>
            <div class="text-sm">
              <p class="font-semibold text-t-bright">Ines Varga</p>
              <p class="text-t-muted">Founder, Halcyon</p>
            </div>
            <a
              href="#join"
              class="ml-auto text-sm font-semibold text-t-accent-ink hover:text-t-bright transition-colors"
            >
              Join the list →
            </a>
          </div>
        </div>
      </section>
    </LandingShell>
  );
});

export const head: DocumentHead = {
  title: "Halcyon — Startup Waitlist template",
  meta: [
    {
      name: "description",
      content:
        "Halcyon delivers your email in three calm batches a day. Join the waitlist for the private beta.",
    },
  ],
};
