import {
  component$,
  useSignal,
  useVisibleTask$,
  $,
  PropFunction,
} from "@builder.io/qwik";
import {
  FEATURED_THEMES,
  ALL_THEMES,
  DEFAULT_THEME_ID,
  resolveThemeId,
  themeName,
  type ThemeMeta,
} from "../lib/themes";

export interface ThemeSwitcherProps {
  onThemeChange$?: PropFunction<(themeId: string) => void>;
}

export const ThemeSwitcher = component$<ThemeSwitcherProps>((props) => {
  const currentTheme = useSignal<string>(DEFAULT_THEME_ID);
  const isOpen = useSignal<boolean>(false);
  const searchFilter = useSignal<string>("");

  // Hydrate current theme from localStorage on client
  // eslint-disable-next-line qwik/no-use-visible-task
  useVisibleTask$(() => {
    try {
      const saved = localStorage.getItem("golden_theme");
      if (saved) {
        currentTheme.value = resolveThemeId(saved);
      }
    } catch {
      // ignore
    }
  });

  const selectTheme = $((themeId: string) => {
    currentTheme.value = themeId;
    try {
      localStorage.setItem("golden_theme", themeId);
      const link = document.getElementById("theme-css") as HTMLLinkElement;
      if (link) {
        link.href = `/themes/${themeId}.css`;
      }
    } catch (err) {
      console.warn("Failed to switch theme:", err);
    }
    if (props.onThemeChange$) {
      props.onThemeChange$(themeId);
    }
    isOpen.value = false;
  });

  const filteredThemes = ALL_THEMES.filter((t) =>
    t.name.toLowerCase().includes(searchFilter.value.toLowerCase()),
  );

  return (
    <div class="relative inline-block text-left">
      {/* Trigger Button */}
      <button
        type="button"
        onClick$={() => (isOpen.value = !isOpen.value)}
        class="flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs font-medium transition-all duration-200 cursor-pointer"
        style={{
          backgroundColor: "var(--color-t-card, #191a1b)",
          borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
          color: "var(--color-t-bright, #ffffff)",
        }}
        title="Switch Design Template"
      >
        <span
          class="inline-block w-2.5 h-2.5 rounded-full ring-1 ring-white/20 animate-pulse"
          style={{ backgroundColor: "var(--color-t-accent, #5e6ad2)" }}
        />
        <span class="opacity-70">Design Spec:</span>
        <span class="font-bold tracking-wide">
          {themeName(currentTheme.value)}
        </span>
        <svg
          class="w-3.5 h-3.5 opacity-60 ml-0.5"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path
            stroke-linecap="round"
            stroke-linejoin="round"
            stroke-width="2"
            d="M19 9l-7 7-7-7"
          />
        </svg>
      </button>

      {/* Popover Dropdown */}
      {isOpen.value && (
        <div
          class="absolute right-0 mt-2 w-80 max-h-[460px] rounded-xl shadow-2xl border z-50 overflow-hidden flex flex-col backdrop-blur-xl"
          style={{
            backgroundColor: "var(--color-t-surface, #0f1011)",
            borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.15))",
            color: "var(--color-t-text, #d0d6e0)",
          }}
        >
          {/* Header */}
          <div class="p-3 border-b border-white/10 flex items-center justify-between">
            <div>
              <h3
                class="text-xs font-bold uppercase tracking-wider"
                style={{ color: "var(--color-t-bright, #ffffff)" }}
              >
                Design Templates
              </h3>
              <p class="text-[10px] opacity-60">75+ instant theme skins</p>
            </div>
            <button
              onClick$={() => (isOpen.value = false)}
              class="p-1 rounded opacity-60 hover:opacity-100 text-xs"
            >
              ✕
            </button>
          </div>

          {/* Quick Presets */}
          <div class="p-2 border-b border-white/5">
            <div class="text-[10px] font-semibold opacity-50 px-1 mb-1 uppercase tracking-wider">
              Featured Specs
            </div>
            <div class="grid grid-cols-3 gap-1">
              {FEATURED_THEMES.slice(0, 9).map((th: ThemeMeta) => (
                <button
                  key={th.id}
                  onClick$={() => selectTheme(th.id)}
                  class={`flex items-center gap-1.5 px-2 py-1 rounded text-left text-[11px] font-medium transition-all ${
                    currentTheme.value === th.id
                      ? "ring-1"
                      : "opacity-75 hover:opacity-100 hover:bg-white/5"
                  }`}
                  style={{
                    backgroundColor:
                      currentTheme.value === th.id
                        ? "var(--color-t-card, #191a1b)"
                        : "transparent",
                    borderColor:
                      currentTheme.value === th.id
                        ? "var(--color-t-accent, #5e6ad2)"
                        : "transparent",
                    color:
                      currentTheme.value === th.id
                        ? "var(--color-t-bright, #ffffff)"
                        : "inherit",
                  }}
                >
                  <span
                    class="w-2 h-2 rounded-full shrink-0"
                    style={{ backgroundColor: th.accent }}
                  />
                  <span class="truncate">{th.name}</span>
                </button>
              ))}
            </div>
          </div>

          {/* Search Input */}
          <div class="p-2 border-b border-white/5">
            <input
              type="text"
              placeholder="Search 75+ themes..."
              value={searchFilter.value}
              onInput$={(e) =>
                (searchFilter.value = (e.target as HTMLInputElement).value)
              }
              class="w-full px-2.5 py-1.5 text-xs rounded-lg border bg-black/20 outline-none focus:border-indigo-400 placeholder:opacity-40"
              style={{
                borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
                color: "var(--color-t-bright, #ffffff)",
              }}
            />
          </div>

          {/* All Themes List */}
          <div class="overflow-y-auto max-h-56 p-1 space-y-0.5">
            {filteredThemes.map(({ id, name }) => (
              <button
                key={id}
                onClick$={() => selectTheme(id)}
                class={`w-full flex items-center justify-between px-3 py-1.5 rounded-lg text-left text-xs transition-all ${
                  currentTheme.value === id
                    ? "font-bold"
                    : "opacity-70 hover:opacity-100 hover:bg-white/5"
                }`}
                style={{
                  backgroundColor:
                    currentTheme.value === id
                      ? "var(--color-t-hover, #28282c)"
                      : "transparent",
                  color:
                    currentTheme.value === id
                      ? "var(--color-t-bright, #ffffff)"
                      : "inherit",
                }}
              >
                <span>{name}</span>
                {currentTheme.value === id && (
                  <span
                    class="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded font-bold"
                    style={{
                      backgroundColor: "var(--color-t-accent, #5e6ad2)",
                      color: "#ffffff",
                    }}
                  >
                    Active
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
});
