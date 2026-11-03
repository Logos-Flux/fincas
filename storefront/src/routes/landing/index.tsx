import { component$, $ } from "@builder.io/qwik";
import { Link, type DocumentHead } from "@builder.io/qwik-city";
import { LANDING_TEMPLATES } from "../../lib/landing-templates";
import { themeName } from "../../lib/themes";

export default component$(() => {
  const openWithTheme = $((slug: string, themeId: string) => {
    try {
      localStorage.setItem("golden_theme", themeId);
    } catch {
      // ignore — the page still opens, in the current theme
    }
    window.location.href = `/landing/${slug}/`;
  });

  return (
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-10">
      <div class="space-y-4 max-w-3xl">
        <h1 class="text-3xl sm:text-5xl font-black tracking-tight text-t-bright">
          Landing Page Templates
        </h1>
        <p class="text-sm sm:text-base text-t-text opacity-80 leading-relaxed">
          The same Qwik front end, beyond the store. Each template is a
          standalone page built only from the theme tokens, so all 75 design
          templates in the switcher reskin it instantly.
        </p>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
        {LANDING_TEMPLATES.map((t) => (
          <article
            key={t.slug}
            class="rounded-2xl border border-t-border bg-t-card p-6 flex flex-col gap-4"
          >
            <div class="flex items-start justify-between gap-3">
              <div>
                <h2 class="text-lg font-bold text-t-bright">{t.name}</h2>
                <p class="text-xs text-t-muted mt-0.5">for “{t.brand}”</p>
              </div>
              <span class="w-3 h-3 rounded-full bg-t-accent mt-1.5 shrink-0" />
            </div>
            <p class="text-sm text-t-text leading-relaxed flex-1">{t.blurb}</p>
            <ul class="flex flex-wrap gap-1.5" aria-label="Sections">
              {t.sections.map((s) => (
                <li
                  key={s}
                  class="text-[11px] px-2 py-0.5 rounded-full border border-t-border text-t-muted"
                >
                  {s}
                </li>
              ))}
            </ul>
            <div class="flex flex-wrap items-center gap-2 pt-1">
              <Link
                href={`/landing/${t.slug}/`}
                class="px-3.5 py-2 rounded-lg bg-t-accent text-t-on-accent text-sm font-semibold hover:opacity-90"
              >
                Open
              </Link>
              <button
                type="button"
                onClick$={() => openWithTheme(t.slug, t.suggestedTheme)}
                class="px-3.5 py-2 rounded-lg border border-t-border text-sm text-t-text hover:text-t-bright"
              >
                Open in {themeName(t.suggestedTheme)}
              </button>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
});

export const head: DocumentHead = {
  title: "Landing Page Templates — Fincas Store",
  meta: [
    {
      name: "description",
      content:
        "Six standalone landing-page templates for the Fincas Qwik front end, all driven by the theme tokens.",
    },
  ],
};
