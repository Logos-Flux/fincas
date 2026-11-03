import { component$, useStore, $ } from "@builder.io/qwik";

type Field = "zip" | "bill" | "owner" | "name" | "phone";

interface QuoteState {
  zip: string;
  bill: string;
  owner: "" | "yes" | "no";
  name: string;
  phone: string;
  errors: Partial<Record<Field, string>>;
  sent: boolean;
}

const BILL_OPTIONS = [
  "Under $100",
  "$100 – $150",
  "$150 – $250",
  "$250 – $400",
  "Over $400",
];

const inputCls =
  "w-full rounded-md border bg-t-deep px-3 py-2.5 text-sm text-t-bright placeholder:text-t-muted focus:outline-none focus:ring-2 focus:ring-t-accent/40 focus:border-t-accent";

function validate(s: QuoteState): Partial<Record<Field, string>> {
  const e: Partial<Record<Field, string>> = {};
  if (!/^\d{5}$/.test(s.zip.trim())) e.zip = "Enter a 5-digit ZIP code.";
  if (!s.bill) e.bill = "Pick the closest range.";
  if (!s.owner) e.owner = "Let us know if you own the home.";
  if (s.name.trim().length < 2)
    e.name = "Add your name so we know who to ask for.";
  const digits = s.phone.replace(/\D/g, "");
  if (digits.length !== 10 && !(digits.length === 11 && digits[0] === "1"))
    e.phone = "Enter a 10-digit phone number.";
  return e;
}

export const QuoteForm = component$(() => {
  const s = useStore<QuoteState>({
    zip: "",
    bill: "",
    owner: "",
    name: "",
    phone: "",
    errors: {},
    sent: false,
  });

  const submit = $(() => {
    const errors = validate(s);
    s.errors = errors;
    if (Object.keys(errors).length === 0) {
      s.sent = true;
    } else {
      const first = (["zip", "bill", "owner", "name", "phone"] as Field[]).find(
        (f) => errors[f],
      );
      const el = document.getElementById(
        first === "owner" ? "q-owner-yes" : `q-${first}`,
      );
      el?.focus();
    }
  });

  const border = (f: Field) =>
    s.errors[f] ? "border-t-error" : "border-t-border";

  if (s.sent) {
    const first = s.name.trim().split(/\s+/)[0];
    return (
      <div
        role="status"
        class="rounded-xl border border-t-border bg-t-card p-6 sm:p-7 shadow-xl shadow-t-accent/10"
      >
        <div class="flex items-center gap-3">
          <span class="grid place-items-center w-10 h-10 rounded-full bg-t-success/15 text-t-success">
            <svg
              class="w-5 h-5"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2.5"
              aria-hidden="true"
            >
              <path stroke-linecap="round" d="M5 12.5l4.5 4.5L19 7.5" />
            </svg>
          </span>
          <h2 class="text-xl font-bold text-t-bright font-[family-name:var(--font-display)]">
            Thanks, {first}. Request received.
          </h2>
        </div>
        <p class="mt-4 text-sm text-t-text leading-relaxed">
          Dana from our Millbrook office will call{" "}
          <span class="font-semibold text-t-bright">{s.phone}</span> within one
          business day to book a free roof check for ZIP{" "}
          <span class="font-semibold text-t-bright">{s.zip}</span>.
        </p>
        <ol class="mt-5 space-y-2.5 text-sm text-t-text">
          <li class="flex gap-2.5">
            <span class="font-[family-name:var(--font-mono)] text-t-accent-ink">
              1
            </span>
            Have a recent electric bill handy (a photo is fine).
          </li>
          <li class="flex gap-2.5">
            <span class="font-[family-name:var(--font-mono)] text-t-accent-ink">
              2
            </span>
            We pull satellite imagery of your roof before the call.
          </li>
          <li class="flex gap-2.5">
            <span class="font-[family-name:var(--font-mono)] text-t-accent-ink">
              3
            </span>
            You get a written, itemized quote. No pressure, no expiry tricks.
          </li>
        </ol>
        {s.owner === "no" && (
          <p class="mt-5 text-sm rounded-md bg-t-warning/10 border border-t-warning/30 px-3 py-2 text-t-text">
            Since you rent, we'll include a one-page summary you can pass to
            your landlord.
          </p>
        )}
        <button
          type="button"
          class="mt-6 text-sm font-semibold text-t-accent-ink hover:underline"
          onClick$={() => {
            s.sent = false;
            s.zip = s.bill = s.name = s.phone = "";
            s.owner = "";
            s.errors = {};
          }}
        >
          Submit another request
        </button>
      </div>
    );
  }

  return (
    <form
      preventdefault:submit
      onSubmit$={submit}
      noValidate
      aria-labelledby="quote-title"
      class="rounded-xl border border-t-border bg-t-card shadow-xl shadow-t-accent/10 overflow-hidden"
    >
      <div class="bg-t-accent px-5 sm:px-6 py-3 flex items-center justify-between gap-3">
        <h2
          id="quote-title"
          class="text-base font-bold font-[family-name:var(--font-display)]"
          // Theme skins colour h1-h3 directly, which beats utility classes.
          style={{ color: "var(--color-t-on-accent)" }}
        >
          Get a free quote
        </h2>
        <span class="text-xs font-semibold text-t-on-accent">
          Takes 60 seconds
        </span>
      </div>

      <div class="p-5 sm:p-6 grid grid-cols-2 gap-x-3 gap-y-4">
        <div class="col-span-1">
          <label
            for="q-zip"
            class="block text-xs font-semibold text-t-bright mb-1.5"
          >
            ZIP code
          </label>
          <input
            id="q-zip"
            name="zip"
            inputMode="numeric"
            autoComplete="postal-code"
            maxLength={5}
            placeholder="e.g. 48104"
            value={s.zip}
            onInput$={(_, el) => (s.zip = el.value)}
            aria-invalid={!!s.errors.zip}
            aria-describedby={s.errors.zip ? "q-zip-err" : undefined}
            class={[inputCls, border("zip")]}
          />
          {s.errors.zip && (
            <p id="q-zip-err" class="mt-1 text-xs text-t-error">
              {s.errors.zip}
            </p>
          )}
        </div>

        <div class="col-span-1">
          <label
            for="q-bill"
            class="block text-xs font-semibold text-t-bright mb-1.5"
          >
            Monthly electric bill
          </label>
          <select
            id="q-bill"
            name="bill"
            value={s.bill}
            onChange$={(_, el) => (s.bill = el.value)}
            aria-invalid={!!s.errors.bill}
            aria-describedby={s.errors.bill ? "q-bill-err" : undefined}
            class={[inputCls, border("bill")]}
          >
            <option value="">Select…</option>
            {BILL_OPTIONS.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
          {s.errors.bill && (
            <p id="q-bill-err" class="mt-1 text-xs text-t-error">
              {s.errors.bill}
            </p>
          )}
        </div>

        <fieldset
          class="col-span-2"
          aria-describedby={s.errors.owner ? "q-owner-err" : undefined}
        >
          <legend class="block text-xs font-semibold text-t-bright mb-1.5">
            Do you own this home?
          </legend>
          <div class="grid grid-cols-2 gap-2">
            {(["yes", "no"] as const).map((v) => (
              <label
                key={v}
                for={`q-owner-${v}`}
                class={[
                  "flex items-center gap-2 rounded-md border px-3 py-2.5 text-sm cursor-pointer transition-colors has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-t-accent/40",
                  s.owner === v
                    ? "border-t-accent bg-t-accent/10 text-t-bright"
                    : `${border("owner")} text-t-text hover:bg-t-hover`,
                ]}
              >
                <input
                  id={`q-owner-${v}`}
                  type="radio"
                  name="owner"
                  value={v}
                  checked={s.owner === v}
                  onChange$={() => (s.owner = v)}
                  class="sr-only"
                />
                <span
                  aria-hidden="true"
                  class={[
                    "grid place-items-center w-4 h-4 rounded-full border-2 shrink-0",
                    s.owner === v ? "border-t-accent" : "border-t-muted",
                  ]}
                >
                  {s.owner === v && (
                    <span class="w-2 h-2 rounded-full bg-t-accent" />
                  )}
                </span>
                {v === "yes" ? "Yes, I own it" : "No, I rent"}
              </label>
            ))}
          </div>
          {s.errors.owner && (
            <p id="q-owner-err" class="mt-1 text-xs text-t-error">
              {s.errors.owner}
            </p>
          )}
        </fieldset>

        <div class="col-span-2 sm:col-span-1">
          <label
            for="q-name"
            class="block text-xs font-semibold text-t-bright mb-1.5"
          >
            Full name
          </label>
          <input
            id="q-name"
            name="name"
            autoComplete="name"
            placeholder="Jordan Ellis"
            value={s.name}
            onInput$={(_, el) => (s.name = el.value)}
            aria-invalid={!!s.errors.name}
            aria-describedby={s.errors.name ? "q-name-err" : undefined}
            class={[inputCls, border("name")]}
          />
          {s.errors.name && (
            <p id="q-name-err" class="mt-1 text-xs text-t-error">
              {s.errors.name}
            </p>
          )}
        </div>

        <div class="col-span-2 sm:col-span-1">
          <label
            for="q-phone"
            class="block text-xs font-semibold text-t-bright mb-1.5"
          >
            Phone
          </label>
          <input
            id="q-phone"
            name="phone"
            type="tel"
            autoComplete="tel"
            placeholder="(555) 014-2290"
            value={s.phone}
            onInput$={(_, el) => (s.phone = el.value)}
            aria-invalid={!!s.errors.phone}
            aria-describedby={s.errors.phone ? "q-phone-err" : undefined}
            class={[inputCls, border("phone")]}
          />
          {s.errors.phone && (
            <p id="q-phone-err" class="mt-1 text-xs text-t-error">
              {s.errors.phone}
            </p>
          )}
        </div>

        <button
          type="submit"
          class="col-span-2 mt-1 w-full rounded-md bg-t-accent px-4 py-3 text-sm font-bold text-t-on-accent hover:opacity-90 transition-opacity"
        >
          Send my free quote request
        </button>
        <p class="col-span-2 text-xs text-t-muted leading-relaxed">
          One call from a local estimator, no sales sequence. We never sell or
          share your number.
        </p>
      </div>
    </form>
  );
});
