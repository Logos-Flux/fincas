import { component$, useSignal, useVisibleTask$, $ } from "@builder.io/qwik";
import { Link, type DocumentHead } from "@builder.io/qwik-city";
import {
  FEATURED_THEMES,
  ALL_THEMES,
  DEFAULT_THEME_ID,
  resolveThemeId,
  themeName,
  type ThemeMeta,
} from "../../lib/themes";

export default component$(() => {
  const activeTheme = useSignal<string>(DEFAULT_THEME_ID);
  const searchFilter = useSignal<string>("");

  // eslint-disable-next-line qwik/no-use-visible-task
  useVisibleTask$(() => {
    try {
      const saved = localStorage.getItem("golden_theme");
      if (saved) activeTheme.value = resolveThemeId(saved);
    } catch {
      // ignore
    }
  });

  const selectTheme = $((themeId: string) => {
    activeTheme.value = themeId;
    try {
      localStorage.setItem("golden_theme", themeId);
      const link = document.getElementById("theme-css") as HTMLLinkElement;
      if (link) {
        link.href = `/themes/${themeId}.css`;
      }
    } catch (err) {
      console.warn(err);
    }
  });

  const filteredThemes = ALL_THEMES.filter((t) =>
    t.name.toLowerCase().includes(searchFilter.value.toLowerCase()),
  );

  return (
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-12">
      {/* Header */}
      <div class="space-y-4 max-w-3xl">
        <div
          class="inline-flex items-center gap-2 px-3 py-1 rounded-full text-xs font-semibold border backdrop-blur-md"
          style={{
            borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.15))",
            color: "var(--color-t-bright, #ffffff)",
          }}
        >
          <span
            class="w-2 h-2 rounded-full"
            style={{ backgroundColor: "var(--color-t-accent, #5e6ad2)" }}
          />
          <span>Interactive Frontend Design-Specs Showcase</span>
        </div>

        <h1
          class="text-3xl sm:text-5xl font-black tracking-tight"
          style={{ color: "var(--color-t-bright, #ffffff)" }}
        >
          Design Templates <br />
          <span style={{ color: "var(--color-t-accent, #5e6ad2)" }}>
            Real-Time CSS Reskinning
          </span>
        </h1>

        <p class="text-sm sm:text-base opacity-75 leading-relaxed">
          Select any template below to immediately morph the design tokens,
          typography, surfaces, borders, buttons, and elevation models across
          this showcase and the entire store.
        </p>
      </div>

      {/* Theme Selector Palette Grid */}
      <section
        class="rounded-3xl border p-6 sm:p-8 space-y-6"
        style={{
          backgroundColor: "var(--color-t-card, #191a1b)",
          borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
        }}
      >
        <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div>
            <h2
              class="text-base font-bold uppercase tracking-wider text-xs"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              Featured Iconic Design Specs
            </h2>
            <p class="text-xs opacity-60">
              Click any system to hot-swap the stylesheet
            </p>
          </div>

          <div class="flex items-center gap-2 w-full sm:w-64">
            <input
              type="text"
              placeholder="Search 75+ themes..."
              value={searchFilter.value}
              onInput$={(e) =>
                (searchFilter.value = (e.target as HTMLInputElement).value)
              }
              class="w-full px-3 py-1.5 text-xs rounded-xl border bg-black/30 outline-none placeholder:opacity-40"
              style={{
                borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                color: "var(--color-t-bright, #ffffff)",
              }}
            />
          </div>
        </div>

        {/* Featured Badges Grid */}
        <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-3">
          {FEATURED_THEMES.map((th: ThemeMeta) => (
            <button
              key={th.id}
              onClick$={() => selectTheme(th.id)}
              class={`p-3.5 rounded-2xl border text-left flex flex-col justify-between h-24 transition-all duration-200 cursor-pointer ${
                activeTheme.value === th.id
                  ? "ring-2 scale-102 shadow-xl"
                  : "opacity-75 hover:opacity-100 hover:scale-100"
              }`}
              style={{
                backgroundColor: th.background,
                borderColor:
                  activeTheme.value === th.id
                    ? th.accent
                    : th.isDark
                      ? "rgba(255, 255, 255, 0.12)"
                      : "rgba(0, 0, 0, 0.12)",
              }}
            >
              <div class="flex items-center justify-between w-full">
                <span
                  class="w-3.5 h-3.5 rounded-full ring-2 ring-white/20"
                  style={{ backgroundColor: th.accent }}
                />
                {activeTheme.value === th.id && (
                  <span
                    class="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded text-white"
                    style={{ backgroundColor: th.accent }}
                  >
                    Active
                  </span>
                )}
              </div>
              <div>
                <div
                  class="text-xs font-bold"
                  style={{ color: th.isDark ? "#ffffff" : "#111827" }}
                >
                  {th.name}
                </div>
                <div
                  class="text-[10px] font-mono mt-0.5"
                  style={{
                    color: th.isDark
                      ? "rgba(255, 255, 255, 0.6)"
                      : "rgba(0, 0, 0, 0.6)",
                  }}
                >
                  {th.category}
                </div>
              </div>
            </button>
          ))}
        </div>

        {/* Full Themes Dropdown Drawer / List if searched */}
        {searchFilter.value.trim() !== "" && (
          <div class="pt-4 border-t border-white/10 space-y-2">
            <div class="text-[10px] uppercase font-bold opacity-60">
              Search Results:
            </div>
            <div class="flex flex-wrap gap-1.5 max-h-40 overflow-y-auto p-1">
              {filteredThemes.map(({ id, name }) => (
                <button
                  key={id}
                  onClick$={() => selectTheme(id)}
                  class={`px-2.5 py-1 rounded-lg text-xs font-mono border transition-all ${
                    activeTheme.value === id
                      ? "bg-white/20 text-white font-bold border-white"
                      : "opacity-70 hover:opacity-100 border-white/10"
                  }`}
                >
                  {name}
                </button>
              ))}
            </div>
          </div>
        )}
      </section>

      {/* Live Specimen Demonstration Grid */}
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
        {/* Token Surfaces & Elevations */}
        <section
          class="rounded-3xl border p-6 space-y-6"
          style={{
            backgroundColor: "var(--color-t-card, #191a1b)",
            borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
          }}
        >
          <div class="flex items-center justify-between border-b border-white/5 pb-3">
            <h3
              class="text-sm font-bold uppercase tracking-wider"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              1. Color Token Surfaces
            </h3>
            <span class="text-[11px] font-mono opacity-50">
              Active: {themeName(activeTheme.value)}
            </span>
          </div>

          <div class="grid grid-cols-2 gap-3 text-xs font-mono">
            <div
              class="p-3 rounded-xl border border-white/5 bg-[var(--color-t-deep)]"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              <span class="opacity-60 text-[10px]">--color-t-deep</span>
              <div class="font-bold mt-1 truncate">Deep Canvas</div>
            </div>
            <div
              class="p-3 rounded-xl border border-white/5 bg-[var(--color-t-surface)]"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              <span class="opacity-60 text-[10px]">--color-t-surface</span>
              <div class="font-bold mt-1 truncate">Surface / Panel</div>
            </div>
            <div
              class="p-3 rounded-xl border border-white/5 bg-[var(--color-t-card)]"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              <span class="opacity-60 text-[10px]">--color-t-card</span>
              <div class="font-bold mt-1 truncate">Card Layer</div>
            </div>
            <div
              class="p-3 rounded-xl border border-white/5 bg-[var(--color-t-hover)]"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              <span class="opacity-60 text-[10px]">--color-t-hover</span>
              <div class="font-bold mt-1 truncate">Hover Elevation</div>
            </div>
            <div
              class="p-3 rounded-xl border border-white/5 text-white"
              style={{ backgroundColor: "var(--color-t-accent, #5e6ad2)" }}
            >
              <span class="opacity-80 text-[10px]">--color-t-accent</span>
              <div class="font-bold mt-1 truncate">Brand Accent</div>
            </div>
            <div
              class="p-3 rounded-xl border border-white/5 text-white"
              style={{ backgroundColor: "var(--color-t-accent-alt, #7170ff)" }}
            >
              <span class="opacity-80 text-[10px]">--color-t-accent-alt</span>
              <div class="font-bold mt-1 truncate">Secondary Accent</div>
            </div>
          </div>
        </section>

        {/* Buttons & Interactive States */}
        <section
          class="rounded-3xl border p-6 space-y-6"
          style={{
            backgroundColor: "var(--color-t-card, #191a1b)",
            borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
          }}
        >
          <div class="flex items-center justify-between border-b border-white/5 pb-3">
            <h3
              class="text-sm font-bold uppercase tracking-wider"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              2. Interactive Buttons & Badges
            </h3>
            <span class="text-[11px] font-mono opacity-50">Component Spec</span>
          </div>

          <div class="space-y-4">
            <div class="flex flex-wrap items-center gap-3">
              <button
                type="button"
                class="px-4 py-2 rounded-xl text-xs font-bold text-white shadow-lg cursor-pointer"
                style={{
                  backgroundColor: "var(--color-t-accent, #5e6ad2)",
                  boxShadow:
                    "0 4px 12px var(--color-t-accent-glow, rgba(94, 106, 210, 0.3))",
                }}
              >
                Primary Brand CTA
              </button>

              <button
                type="button"
                class="px-4 py-2 rounded-xl text-xs font-semibold border transition-all cursor-pointer hover:bg-white/5"
                style={{
                  borderColor:
                    "var(--color-t-border, rgba(255, 255, 255, 0.15))",
                  color: "var(--color-t-bright, #ffffff)",
                }}
              >
                Secondary Bordered
              </button>

              <button
                type="button"
                disabled
                class="px-4 py-2 rounded-xl text-xs font-semibold opacity-40 border border-white/5 bg-white/5 cursor-not-allowed"
              >
                Disabled Action
              </button>
            </div>

            {/* Badges */}
            <div class="flex flex-wrap items-center gap-2 pt-2">
              <span class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
                ● Success
              </span>
              <span class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-indigo-500/20 text-indigo-400 border border-indigo-500/30">
                ● Info / Live
              </span>
              <span class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-400 border border-amber-500/30">
                ● Warning
              </span>
              <span class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-red-500/20 text-red-400 border border-red-500/30">
                ● Error
              </span>
            </div>
          </div>
        </section>

        {/* Typography & Hierarchy */}
        <section
          class="rounded-3xl border p-6 space-y-6"
          style={{
            backgroundColor: "var(--color-t-card, #191a1b)",
            borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
          }}
        >
          <div class="flex items-center justify-between border-b border-white/5 pb-3">
            <h3
              class="text-sm font-bold uppercase tracking-wider"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              3. Typography & Hierarchy
            </h3>
            <span class="text-[11px] font-mono opacity-50">Font Spec</span>
          </div>

          <div class="space-y-3">
            <h1
              class="text-2xl font-black tracking-tight"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              Heading 1 (Display Weight)
            </h1>
            <h2
              class="text-lg font-bold tracking-tight"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              Heading 2 (Section Subtitle)
            </h2>
            <p class="text-xs leading-relaxed opacity-75">
              Body paragraph copy rendered with the active design system's
              line-height, letter-spacing, and anti-aliasing optimizations.
            </p>
            <div class="p-3 rounded-xl bg-black/40 border border-white/5 font-mono text-xs opacity-80">
              <code>
                const order = await golden.orders.create(&#123; id: 1042
                &#125;);
              </code>
            </div>
          </div>
        </section>

        {/* Form Controls */}
        <section
          class="rounded-3xl border p-6 space-y-6"
          style={{
            backgroundColor: "var(--color-t-card, #191a1b)",
            borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
          }}
        >
          <div class="flex items-center justify-between border-b border-white/5 pb-3">
            <h3
              class="text-sm font-bold uppercase tracking-wider"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              4. Form Inputs & Selectors
            </h3>
            <span class="text-[11px] font-mono opacity-50">
              Input Focus Spec
            </span>
          </div>

          <div class="space-y-3 text-xs">
            <div class="space-y-1">
              <label class="opacity-70 font-medium">Standard Text Input</label>
              <input
                type="text"
                placeholder="Type something..."
                class="w-full p-2.5 rounded-xl border bg-black/20 outline-none transition-all"
                style={{
                  borderColor:
                    "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                  color: "var(--color-t-bright, #ffffff)",
                }}
              />
            </div>

            <div class="space-y-1">
              <label class="opacity-70 font-medium">Select Dropdown</label>
              <select
                class="w-full p-2.5 rounded-xl border bg-black/20 outline-none"
                style={{
                  borderColor:
                    "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                  color: "var(--color-t-bright, #ffffff)",
                }}
              >
                <option>Option A (Tier 1 Pricing)</option>
                <option>Option B (Tier 2 Volume Break)</option>
              </select>
            </div>
          </div>
        </section>
      </div>

      {/* Direct CTA Back to Store */}
      <div
        class="rounded-3xl border p-8 text-center space-y-4 shadow-xl"
        style={{
          backgroundColor: "var(--color-t-surface, #0f1011)",
          borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.15))",
        }}
      >
        <h2
          class="text-xl font-bold"
          style={{ color: "var(--color-t-bright, #ffffff)" }}
        >
          Experience the Store in "{activeTheme.value}"
        </h2>
        <p class="text-xs opacity-70 max-w-md mx-auto">
          The selected design system is active across the entire application
          session.
        </p>
        <div>
          <Link
            href="/"
            class="inline-block px-6 py-3 rounded-xl font-bold text-xs uppercase tracking-wider text-white shadow-xl transition-transform active:scale-95"
            style={{
              backgroundColor: "var(--color-t-accent, #5e6ad2)",
              boxShadow:
                "0 4px 14px var(--color-t-accent-glow, rgba(94, 106, 210, 0.4))",
            }}
          >
            ← View Catalog in {activeTheme.value}
          </Link>
        </div>
      </div>
    </div>
  );
});

export const head: DocumentHead = {
  title: "Design Templates Showcase — Fincas Store",
  meta: [
    {
      name: "description",
      content:
        "Explore 75+ dynamic design templates and real-time CSS reskinning.",
    },
  ],
};
