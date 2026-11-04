import { component$, useContext } from "@builder.io/qwik";
import { Link } from "@builder.io/qwik-city";
import { ThemeSwitcher } from "./theme-switcher";
import { CartContext } from "../context/cart-context";
import { getBackendOrigin } from "../lib/api-client";

export const Header = component$(() => {
  const cart = useContext(CartContext);
  const totalCount = cart.items.reduce((sum, item) => sum + item.quantity, 0);

  return (
    <header
      class="w-full border-b transition-colors duration-300"
      style={{
        backgroundColor: "var(--color-t-deep, #08090a)",
        borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
      }}
    >
      <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between gap-4">
        {/* Brand Logo */}
        <div class="flex items-center gap-6">
          <Link
            href="/"
            class="flex items-center gap-2.5 text-lg font-black tracking-tight group"
          >
            <img
              src="/fincas-mark.svg"
              alt="Fincas Store"
              width={32}
              height={32}
              class="w-8 h-8 transition-transform group-hover:scale-105 shrink-0"
            />
            <span
              style={{ color: "var(--color-t-bright, #ffffff)" }}
              class="hidden sm:inline"
            >
              Fincas
              <span
                style={{
                  color:
                    "var(--color-t-accent-ink, var(--color-t-accent, #5e6ad2))",
                }}
              >
                Store
              </span>
            </span>
          </Link>

          {/* Navigation Links */}
          <nav class="hidden md:flex items-center gap-1 text-sm font-medium">
            <Link
              href="/"
              class="px-3 py-1.5 rounded-md transition-colors opacity-80 hover:opacity-100"
              style={{ color: "var(--color-t-text, #d0d6e0)" }}
            >
              Catalog
            </Link>
            <Link
              href="/brands/"
              class="px-3 py-1.5 rounded-md transition-colors opacity-80 hover:opacity-100"
              style={{ color: "var(--color-t-text, #d0d6e0)" }}
            >
              Brands
            </Link>
            <Link
              href="/design-specs"
              class="px-3 py-1.5 rounded-md transition-colors opacity-80 hover:opacity-100 flex items-center gap-1.5"
              style={{ color: "var(--color-t-text, #d0d6e0)" }}
            >
              <span
                class="w-1.5 h-1.5 rounded-full"
                style={{ backgroundColor: "var(--color-t-accent, #5e6ad2)" }}
              />
              Design Specs
            </Link>
            <Link
              href="/landing/"
              class="px-3 py-1.5 rounded-md transition-colors opacity-80 hover:opacity-100"
              style={{ color: "var(--color-t-text, #d0d6e0)" }}
            >
              Landing Pages
            </Link>
            <a
              href={`${getBackendOrigin()}/erp/`}
              target="_blank"
              rel="noreferrer"
              class="px-3 py-1.5 rounded-md transition-colors opacity-80 hover:opacity-100"
              style={{ color: "var(--color-t-text, #d0d6e0)" }}
            >
              ERP Admin ↗
            </a>
            <a
              href={`${getBackendOrigin()}/api/docs/`}
              target="_blank"
              rel="noreferrer"
              class="px-3 py-1.5 rounded-md transition-colors opacity-80 hover:opacity-100"
              style={{ color: "var(--color-t-text, #d0d6e0)" }}
            >
              API Docs ↗
            </a>
          </nav>
        </div>

        {/* Right Side Controls */}
        <div class="flex items-center gap-3">
          <ThemeSwitcher />

          <Link
            href="/account/"
            class="flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs font-semibold transition-all hover:opacity-90"
            style={{
              backgroundColor: "var(--color-t-card, #191a1b)",
              borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
              color: "var(--color-t-bright, #ffffff)",
            }}
          >
            <svg
              class="w-4 h-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              aria-hidden="true"
            >
              <path
                stroke-linecap="round"
                stroke-linejoin="round"
                stroke-width="2"
                d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
              />
            </svg>
            <span class="sr-only sm:not-sr-only">Account</span>
          </Link>

          {/* Cart Button */}
          <button
            type="button"
            onClick$={() => (cart.isOpen = true)}
            class="relative flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs font-semibold transition-all cursor-pointer hover:opacity-90"
            style={{
              backgroundColor: "var(--color-t-card, #191a1b)",
              borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.1))",
              color: "var(--color-t-bright, #ffffff)",
            }}
          >
            <svg
              class="w-4 h-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                stroke-linecap="round"
                stroke-linejoin="round"
                stroke-width="2"
                d="M16 11V7a4 4 0 00-8 0v4M5 9h14l1 12H4L5 9z"
              />
            </svg>
            <span class="hidden sm:inline">Cart</span>
            {totalCount > 0 && (
              <span
                class="px-1.5 py-0.2 rounded-full text-[10px] font-bold text-white shadow-sm"
                style={{ backgroundColor: "var(--color-t-accent, #5e6ad2)" }}
              >
                {totalCount}
              </span>
            )}
          </button>
        </div>
      </div>
    </header>
  );
});
