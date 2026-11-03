import { component$, Slot, useSignal } from "@builder.io/qwik";
import { Link } from "@builder.io/qwik-city";
import { ThemeSwitcher } from "../theme-switcher";

export interface NavLink {
  label: string;
  href: string;
}

export interface LandingShellProps {
  /** The fictional company the page is written for. */
  brand: string;
  /** A one- or two-letter mark for the logo tile. */
  mark: string;
  links: NavLink[];
  cta: NavLink;
  /** Footer link columns, keyed by heading. */
  footer: Record<string, string[]>;
  tagline: string;
}

/**
 * Chrome shared by every landing template: a thin preview bar (back to the
 * gallery + theme switcher) that is not part of the design, then the page's
 * own nav, the page body, and its footer.
 */
export const LandingShell = component$<LandingShellProps>((props) => {
  const menuOpen = useSignal(false);

  return (
    <div class="min-h-screen flex flex-col bg-t-deep text-t-text font-[family-name:var(--font-primary)]">
      <div class="bg-t-surface border-b border-t-border text-xs">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-11 flex items-center justify-between gap-3">
          <Link
            href="/landing/"
            class="inline-flex items-center gap-1.5 text-t-muted hover:text-t-bright transition-colors"
          >
            <span aria-hidden="true">←</span> All landing templates
          </Link>
          <ThemeSwitcher />
        </div>
      </div>

      <header class="sticky top-0 z-40 border-b border-t-border bg-t-deep/85 backdrop-blur-md">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between gap-6">
          <a href="#top" class="flex items-center gap-2.5 shrink-0">
            <span class="grid place-items-center w-8 h-8 rounded-lg bg-t-accent text-t-on-accent text-sm font-black">
              {props.mark}
            </span>
            <span class="font-bold tracking-tight text-t-bright font-[family-name:var(--font-display)]">
              {props.brand}
            </span>
          </a>

          <nav
            aria-label="Primary"
            class="hidden md:flex items-center gap-1 text-sm"
          >
            {props.links.map((l) => (
              <a
                key={l.href}
                href={l.href}
                class="px-3 py-2 rounded-md text-t-text hover:text-t-bright transition-colors"
              >
                {l.label}
              </a>
            ))}
          </nav>

          <div class="flex items-center gap-2">
            <a
              href={props.cta.href}
              class="hidden sm:inline-flex items-center px-4 py-2 rounded-lg bg-t-accent text-t-on-accent text-sm font-semibold hover:opacity-90 transition-opacity"
            >
              {props.cta.label}
            </a>
            <button
              type="button"
              class="md:hidden p-2 rounded-md text-t-bright"
              aria-label="Open menu"
              aria-expanded={menuOpen.value}
              onClick$={() => (menuOpen.value = !menuOpen.value)}
            >
              <svg
                class="w-5 h-5"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                aria-hidden="true"
              >
                <path
                  stroke-linecap="round"
                  stroke-width="2"
                  d="M4 7h16M4 12h16M4 17h16"
                />
              </svg>
            </button>
          </div>
        </div>
        {menuOpen.value && (
          <nav
            aria-label="Mobile"
            class="md:hidden border-t border-t-border px-4 py-3 flex flex-col"
          >
            {[...props.links, props.cta].map((l) => (
              <a
                key={l.href + l.label}
                href={l.href}
                onClick$={() => (menuOpen.value = false)}
                class="py-2 text-t-text hover:text-t-bright"
              >
                {l.label}
              </a>
            ))}
          </nav>
        )}
      </header>

      <main id="top" class="flex-1">
        <Slot />
      </main>

      <footer class="border-t border-t-border bg-t-surface">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-14 grid grid-cols-2 md:grid-cols-5 gap-8 text-sm">
          <div class="col-span-2 space-y-3">
            <div class="flex items-center gap-2.5">
              <span class="grid place-items-center w-7 h-7 rounded-md bg-t-accent text-t-on-accent text-xs font-black">
                {props.mark}
              </span>
              <span class="font-bold text-t-bright">{props.brand}</span>
            </div>
            <p class="text-t-muted max-w-xs leading-relaxed">{props.tagline}</p>
          </div>
          {Object.entries(props.footer).map(([heading, items]) => (
            <div key={heading} class="space-y-3">
              <h2 class="text-xs font-semibold uppercase tracking-wider text-t-bright">
                {heading}
              </h2>
              <ul class="space-y-2 text-t-muted">
                {items.map((item) => (
                  <li key={item}>
                    <a
                      href="#top"
                      class="hover:text-t-bright transition-colors"
                    >
                      {item}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <div class="border-t border-t-border">
          <p class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 text-xs text-t-muted">
            © 2026 {props.brand}. A fictional company — this page is a Fincas
            storefront landing template.
          </p>
        </div>
      </footer>
    </div>
  );
});
