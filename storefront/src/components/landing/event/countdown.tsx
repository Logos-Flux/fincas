import { component$, useSignal, useVisibleTask$ } from "@builder.io/qwik";
import { EVENT_START } from "./data";

const pad = (n: number, width = 2) => String(n).padStart(width, "0");

function split(ms: number) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return {
    days: Math.floor(s / 86400),
    hours: Math.floor((s % 86400) / 3600),
    minutes: Math.floor((s % 3600) / 60),
    seconds: s % 60,
  };
}

/**
 * Live countdown to doors-open. The server renders the remaining time at
 * request time (serialised into the signal, so resuming never disagrees with
 * the HTML); the browser then ticks it once a second.
 */
export const Countdown = component$(() => {
  const remaining = useSignal(Math.max(0, EVENT_START - Date.now()));

  // eslint-disable-next-line qwik/no-use-visible-task
  useVisibleTask$(({ cleanup }) => {
    const tick = () => {
      remaining.value = Math.max(0, EVENT_START - Date.now());
    };
    tick();
    const id = setInterval(tick, 1000);
    cleanup(() => clearInterval(id));
  });

  const t = split(remaining.value);
  const units = [
    { label: "Days", value: pad(t.days, 3) },
    { label: "Hours", value: pad(t.hours) },
    { label: "Min", value: pad(t.minutes) },
    { label: "Sec", value: pad(t.seconds) },
  ];

  return (
    <div
      role="timer"
      aria-label={`Doors open in ${t.days} days and ${t.hours} hours`}
      class="grid grid-cols-4 gap-1.5 sm:gap-2"
    >
      {units.map((u, i) => (
        <div
          key={u.label}
          class={[
            "rounded-lg border px-1.5 sm:px-3 pt-3 pb-2 text-center",
            i === 0
              ? "bg-t-accent border-t-accent text-t-on-accent"
              : "bg-t-deep border-t-border text-t-bright",
          ]}
        >
          <div
            aria-hidden="true"
            class="font-[family-name:var(--font-mono)] tabular-nums font-bold text-2xl sm:text-4xl leading-none tracking-tight"
          >
            {u.value}
          </div>
          <div
            aria-hidden="true"
            class={[
              "mt-2 text-[10px] sm:text-[11px] font-semibold uppercase tracking-[0.18em]",
              i === 0 ? "text-t-on-accent" : "text-t-muted",
            ]}
          >
            {u.label}
          </div>
        </div>
      ))}
    </div>
  );
});
