export type ScreenId = "tide" | "swell" | "quality" | "buddy";

/* ---------- tiny glyphs ---------- */

const StatusBar = () => (
  <div class="flex items-center justify-between px-6 pt-3 pb-1 text-[11px] font-semibold text-t-bright">
    <span>9:41</span>
    <span class="flex items-center gap-1" aria-hidden="true">
      <svg viewBox="0 0 18 12" class="w-4 h-3" fill="currentColor">
        <rect x="0" y="8" width="3" height="4" rx="1" />
        <rect x="5" y="5" width="3" height="7" rx="1" />
        <rect x="10" y="2.5" width="3" height="9.5" rx="1" />
        <rect x="15" y="0" width="3" height="12" rx="1" />
      </svg>
      <svg
        viewBox="0 0 16 12"
        class="w-4 h-3"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
      >
        <path d="M1.5 4.5a9.5 9.5 0 0 1 13 0M4 7.3a6 6 0 0 1 8 0" />
        <circle cx="8" cy="10" r="1.2" fill="currentColor" stroke="none" />
      </svg>
      <span class="relative inline-block w-6 h-3 rounded-[3px] border border-t-bright/70 p-[1.5px]">
        <span class="block h-full w-[72%] rounded-[1.5px] bg-t-bright" />
      </span>
    </span>
  </div>
);

const TabBar = ({ active }: { active: ScreenId }) => {
  const items: { id: ScreenId; label: string; d: string }[] = [
    { id: "tide", label: "Tides", d: "M2 13c3-4 5-4 8 0s5 4 8 0" },
    { id: "swell", label: "Swell", d: "M3 16h3V9H3zm6 0h3V4H9zm6 0h3v-5h-3z" },
    {
      id: "quality",
      label: "Water",
      d: "M10 2s-6 7-6 11a6 6 0 0 0 12 0c0-4-6-11-6-11z",
    },
    {
      id: "buddy",
      label: "Buddy",
      d: "M7 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm7 1a2.5 2.5 0 1 0 0-5M1 17c0-3 3-5 6-5s6 2 6 5m2-5c2 0 4 1.5 4 4",
    },
  ];
  return (
    <div class="mt-auto grid grid-cols-4 border-t border-t-border bg-t-surface/90 px-2 pt-2 pb-5">
      {items.map((it) => (
        <span
          key={it.id}
          class={[
            "flex flex-col items-center gap-0.5 text-[9px] font-semibold",
            it.id === active ? "text-t-accent-ink" : "text-t-muted",
          ]}
        >
          <svg
            viewBox="0 0 20 20"
            class="w-5 h-5"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d={it.d} />
          </svg>
          {it.label}
        </span>
      ))}
    </div>
  );
};

const Header = ({ kicker, title }: { kicker: string; title: string }) => (
  <div class="px-5 pt-3">
    <p class="text-[10px] font-semibold uppercase tracking-[0.14em] text-t-muted">
      {kicker}
    </p>
    <p class="mt-0.5 text-[17px] font-bold leading-tight text-t-bright font-[family-name:var(--font-display)]">
      {title}
    </p>
  </div>
);

/* ---------- screens ---------- */

const TideScreen = () => (
  <>
    <Header kicker="Cove Rock · North Beach" title="Rising, 1.42 m" />
    <div class="mx-4 mt-3 rounded-2xl bg-t-card border border-t-border p-3">
      <div class="flex items-baseline justify-between text-[10px] text-t-muted">
        <span>Today’s tide</span>
        <span class="text-t-accent-ink font-semibold">
          High 4:52 pm · 1.86 m
        </span>
      </div>
      <svg viewBox="0 0 240 90" class="mt-2 w-full h-20" aria-hidden="true">
        <path
          d="M0 62 C 30 18, 55 16, 80 38 S 130 88, 160 72 S 215 12, 240 26 L 240 90 L 0 90 Z"
          class="text-t-accent"
          fill="currentColor"
          fill-opacity="0.16"
        />
        <path
          d="M0 62 C 30 18, 55 16, 80 38 S 130 88, 160 72 S 215 12, 240 26"
          class="text-t-accent"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
        />
        <line
          x1="176"
          y1="0"
          x2="176"
          y2="90"
          class="text-t-muted"
          stroke="currentColor"
          stroke-dasharray="3 3"
        />
        <circle
          cx="176"
          cy="58"
          r="5"
          class="text-t-accent"
          fill="currentColor"
        />
      </svg>
      <div class="flex justify-between text-[9px] text-t-muted">
        <span>6a</span>
        <span>9a</span>
        <span>12p</span>
        <span>3p</span>
        <span>6p</span>
        <span>9p</span>
      </div>
    </div>
    <div class="mx-4 mt-3 grid grid-cols-3 gap-2">
      {[
        ["Water", "17.4°", "+0.6 since Sun"],
        ["Swell", "0.9 m", "11 s, WSW"],
        ["Wind", "8 km/h", "light offshore"],
      ].map(([k, v, s]) => (
        <div key={k} class="rounded-xl bg-t-card border border-t-border p-2">
          <p class="text-[9px] text-t-muted">{k}</p>
          <p class="text-[15px] font-bold text-t-bright">{v}</p>
          <p class="text-[8.5px] leading-tight text-t-muted">{s}</p>
        </div>
      ))}
    </div>
    <div class="mx-4 mt-3 flex items-center gap-2.5 rounded-xl bg-t-success/15 px-3 py-2.5">
      <span class="w-2 h-2 rounded-full bg-t-success shrink-0" />
      <div>
        <p class="text-[11px] font-bold text-t-bright">
          Your window: 3:40 – 5:10 pm
        </p>
        <p class="text-[9.5px] text-t-text">
          Matches 3 of 3 rules · bacteria clear
        </p>
      </div>
    </div>
    <p class="mx-5 mt-4 text-[10px] font-semibold uppercase tracking-[0.12em] text-t-muted">
      Next 3 days
    </p>
    <div class="mx-4 mt-1.5 grid grid-cols-3 gap-2">
      {[
        ["Thu", "4:30–6:00p", "ok"],
        ["Fri", "7:10–8:20a", "ok"],
        ["Sat", "Rain advisory", "warn"],
      ].map(([d, w, t]) => (
        <div key={d} class="rounded-xl border border-t-border px-2 py-1.5">
          <p class="flex items-center gap-1 text-[10px] font-bold text-t-bright">
            <span
              class={[
                "w-1.5 h-1.5 rounded-full",
                t === "ok" ? "bg-t-success" : "bg-t-warning",
              ]}
            />
            {d}
          </p>
          <p class="text-[8.5px] leading-tight text-t-text">{w}</p>
        </div>
      ))}
    </div>
  </>
);

const SwellScreen = () => {
  const bars = [0.6, 0.7, 0.8, 0.9, 1.1, 1.3, 1.2, 1.0, 0.9, 0.8, 0.8, 0.7];
  return (
    <>
      <Header kicker="Pelican Point · Reef break" title="Clean, waist-high" />
      <div class="mx-4 mt-3 rounded-2xl bg-t-card border border-t-border p-3">
        <div class="flex items-baseline justify-between text-[10px] text-t-muted">
          <span>Swell height, next 12 h</span>
          <span class="text-t-accent-ink font-semibold">Peak 1.3 m @ 2pm</span>
        </div>
        <div class="mt-3 flex h-24 items-end gap-1">
          {bars.map((h, i) => (
            <span
              key={i}
              class={[
                "flex-1 rounded-t-[3px]",
                i === 5 ? "bg-t-accent" : "bg-t-accent/35",
              ]}
              style={{ height: `${(h / 1.3) * 100}%` }}
            />
          ))}
        </div>
        <div class="mt-1 flex justify-between text-[9px] text-t-muted">
          <span>9a</span>
          <span>12p</span>
          <span>3p</span>
          <span>6p</span>
        </div>
      </div>
      <div class="mx-4 mt-3 grid grid-cols-2 gap-2">
        <div class="rounded-xl bg-t-card border border-t-border p-2.5 flex items-center gap-2.5">
          <span class="grid place-items-center w-9 h-9 rounded-full border border-t-border-active text-t-accent">
            <svg
              viewBox="0 0 20 20"
              class="w-4 h-4 rotate-[200deg]"
              fill="currentColor"
              aria-hidden="true"
            >
              <path d="M10 2l5 14-5-3-5 3z" />
            </svg>
          </span>
          <div>
            <p class="text-[9px] text-t-muted">Wind</p>
            <p class="text-[12px] font-bold text-t-bright">NNE 9</p>
          </div>
        </div>
        <div class="rounded-xl bg-t-card border border-t-border p-2.5">
          <p class="text-[9px] text-t-muted">Period</p>
          <p class="text-[12px] font-bold text-t-bright">12 s groundswell</p>
        </div>
      </div>
      <div class="mx-4 mt-3 rounded-xl border border-t-border p-2.5 text-[10px] text-t-text">
        <span class="font-bold text-t-bright">Crowd guess:</span> 6–9 in the
        lineup by 7 am, based on 41 check-ins last Saturday.
      </div>
      <div class="mx-4 mt-3 flex items-center gap-2.5 rounded-xl bg-t-accent/15 px-3 py-2.5">
        <span class="w-2 h-2 rounded-full bg-t-accent shrink-0" />
        <div>
          <p class="text-[11px] font-bold text-t-bright">
            Best session: 6:30 – 8:00 am
          </p>
          <p class="text-[9.5px] text-t-text">
            Light offshore, mid tide, before the crowd
          </p>
        </div>
      </div>
    </>
  );
};

const QualityScreen = () => {
  const rows: [string, string, string, "ok" | "warn" | "bad"][] = [
    ["Cove Rock", "Clear · 12 E. coli / 100 ml", "Sampled 7:10 am", "ok"],
    ["Harbor Steps", "Advisory · 38 mm rain Tue", "Sampled yesterday", "warn"],
    ["Pelican Point", "Clear · 4 / 100 ml", "Sampled 6:45 am", "ok"],
    ["Mill Creek Mouth", "Closed · outflow reading", "Sampled 8:02 am", "bad"],
  ];
  const tone = {
    ok: "bg-t-success",
    warn: "bg-t-warning",
    bad: "bg-t-error",
  } as const;
  return (
    <>
      <Header kicker="Your 4 spots" title="Water quality" />
      <ul class="mx-4 mt-3 divide-y divide-t-border rounded-2xl bg-t-card border border-t-border">
        {rows.map(([name, status, when, t]) => (
          <li key={name} class="flex items-start gap-2.5 p-2.5">
            <span class={["mt-1 w-2.5 h-2.5 rounded-full shrink-0", tone[t]]} />
            <div class="min-w-0">
              <p class="text-[12px] font-bold text-t-bright">{name}</p>
              <p class="text-[10px] text-t-text">{status}</p>
              <p class="text-[9px] text-t-muted">{when}</p>
            </div>
          </li>
        ))}
      </ul>
      <p class="mx-5 mt-3 text-[9.5px] leading-snug text-t-muted">
        Pulled from county health samples every morning. Tidepool flags any spot
        within 48 h of more than 25 mm of rain.
      </p>
      <div class="mx-4 mt-3 flex items-center justify-between gap-2 rounded-xl border border-t-border bg-t-card px-3 py-2.5">
        <p class="text-[10.5px] font-semibold text-t-bright">
          Tell me when Harbor Steps clears
        </p>
        <span class="relative h-5 w-9 shrink-0 rounded-full bg-t-accent">
          <span class="absolute right-0.5 top-0.5 h-4 w-4 rounded-full bg-t-on-accent" />
        </span>
      </div>
    </>
  );
};

const BuddyScreen = () => (
  <>
    <Header kicker="Checked in · Cove Rock" title="You’re in the water" />
    <div class="mx-4 mt-3 rounded-2xl bg-t-card border border-t-border p-4 text-center">
      <p class="text-[10px] text-t-muted">Time in</p>
      <p class="mt-0.5 text-[34px] font-bold tabular-nums leading-none text-t-bright font-[family-name:var(--font-mono)]">
        00:23:41
      </p>
      <div class="mt-3 h-1.5 rounded-full bg-t-hover overflow-hidden">
        <span class="block h-full w-[39%] rounded-full bg-t-accent" />
      </div>
      <p class="mt-1.5 text-[9.5px] text-t-muted">
        Check out by 6:15 pm · 37 min left
      </p>
    </div>
    <div class="mx-4 mt-3 rounded-xl bg-t-card border border-t-border p-2.5">
      <p class="text-[9px] text-t-muted">Watching out for you</p>
      <div class="mt-1.5 flex items-center gap-2">
        {[
          ["MO", "Maya"],
          ["DK", "Dev"],
        ].map(([i, n]) => (
          <span key={n} class="flex items-center gap-1.5">
            <span class="grid place-items-center w-6 h-6 rounded-full bg-t-accent/20 text-[8px] font-bold text-t-bright">
              {i}
            </span>
            <span class="text-[11px] font-semibold text-t-bright">{n}</span>
          </span>
        ))}
      </div>
    </div>
    <div class="mx-4 mt-3 rounded-xl border border-t-border bg-t-card p-2.5">
      <p class="text-[9px] text-t-muted">Shared with Maya &amp; Dev if late</p>
      <svg viewBox="0 0 220 64" class="mt-1 w-full h-14" aria-hidden="true">
        <path
          d="M0 50 C 40 40, 70 58, 110 46 S 180 30, 220 38 L 220 64 L 0 64 Z"
          class="text-t-accent"
          fill="currentColor"
          fill-opacity="0.14"
        />
        <path
          d="M30 30 C 60 18, 90 26, 120 20 S 160 12, 170 22"
          class="text-t-accent"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-dasharray="4 4"
        />
        <circle
          cx="30"
          cy="30"
          r="3.5"
          class="text-t-muted"
          fill="currentColor"
        />
        <circle
          cx="170"
          cy="22"
          r="6"
          class="text-t-accent"
          fill="currentColor"
          fill-opacity="0.3"
        />
        <circle
          cx="170"
          cy="22"
          r="3.5"
          class="text-t-accent"
          fill="currentColor"
        />
      </svg>
      <p class="text-[9.5px] text-t-text">Last fix 40 s ago · 380 m out</p>
    </div>
    <div class="mx-4 mt-3 grid grid-cols-2 gap-2">
      <span class="rounded-xl bg-t-accent py-2.5 text-center text-[12px] font-bold text-t-on-accent">
        I’m out
      </span>
      <span class="rounded-xl border border-t-border-active py-2.5 text-center text-[12px] font-bold text-t-bright">
        +15 min
      </span>
    </div>
  </>
);

/**
 * A div-built phone. The bezel uses `bg-t-bright` so it reads as a solid
 * device on both light and dark themes; the screen is the theme's page tone.
 */
export const Phone = (props: { screen: ScreenId; label: string }) => {
  const screen = props.screen;
  return (
    <div
      role="img"
      aria-label={props.label}
      class="relative w-[290px] rounded-[3rem] bg-t-bright p-[9px] shadow-[0_40px_80px_-30px_var(--color-t-accent-glow),0_20px_40px_-20px_var(--color-t-border-active)]"
    >
      <span class="absolute -left-[3px] top-28 h-10 w-[3px] rounded-l bg-t-bright" />
      <span class="absolute -left-[3px] top-40 h-10 w-[3px] rounded-l bg-t-bright" />
      <span class="absolute -right-[3px] top-32 h-16 w-[3px] rounded-r bg-t-bright" />
      <div class="relative flex h-[596px] flex-col overflow-hidden rounded-[2.4rem] bg-t-deep">
        <span class="absolute left-1/2 top-2.5 z-10 h-[26px] w-[92px] -translate-x-1/2 rounded-full bg-t-bright" />
        <StatusBar />
        <div class="h-4" />
        {screen === "tide" && <TideScreen />}
        {screen === "swell" && <SwellScreen />}
        {screen === "quality" && <QualityScreen />}
        {screen === "buddy" && <BuddyScreen />}
        <TabBar active={screen} />
      </div>
    </div>
  );
};
