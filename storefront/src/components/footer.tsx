import { component$ } from "@builder.io/qwik";
import { Link } from "@builder.io/qwik-city";
import { getBackendOrigin } from "../lib/api-client";

export const Footer = component$(() => {
  return (
    <footer
      class="border-t mt-20 transition-colors duration-300"
      style={{
        backgroundColor: "var(--color-t-deep, #08090a)",
        borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
      }}
    >
      <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12">
        <div class="grid grid-cols-1 md:grid-cols-4 gap-8">
          <div class="md:col-span-2 space-y-3">
            <div class="flex items-center gap-2.5">
              <img
                src="/fincas-mark.svg"
                alt="Fincas Store"
                width={24}
                height={24}
                class="w-6 h-6 shrink-0"
              />
              <span
                class="font-black tracking-tight text-base"
                style={{ color: "var(--color-t-bright, #ffffff)" }}
              >
                Fincas Storefront
              </span>
            </div>
            <p class="text-xs opacity-70 max-w-sm leading-relaxed">
              Open-source, headless B2B commerce storefront built with Qwik City
              & TailwindCSS. Connected to the Fincas general ledger, automated
              fulfillment, and double-entry accounting engine.
            </p>
            <div class="flex items-center gap-3 pt-2 text-xs">
              <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-medium">
                <span class="w-1.5 h-1.5 rounded-full bg-emerald-400" />
                Headless Engine Online
              </span>
              <span class="opacity-40">|</span>
              <span class="opacity-60">75+ Dynamic Design Specs</span>
            </div>
          </div>

          <div>
            <h4
              class="text-xs font-bold uppercase tracking-wider mb-3"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              Resources
            </h4>
            <ul class="space-y-2 text-xs opacity-75">
              <li>
                <Link href="/" class="hover:opacity-100 transition-opacity">
                  Catalog Products
                </Link>
              </li>
              <li>
                <Link
                  href="/design-specs"
                  class="hover:opacity-100 transition-opacity"
                >
                  Design Specs Showcase
                </Link>
              </li>
              <li>
                <a
                  href={`${getBackendOrigin()}/api/docs/`}
                  target="_blank"
                  rel="noreferrer"
                  class="hover:opacity-100 transition-opacity"
                >
                  OpenAPI 3.0 Documentation ↗
                </a>
              </li>
              <li>
                <a
                  href={`${getBackendOrigin()}/erp/`}
                  target="_blank"
                  rel="noreferrer"
                  class="hover:opacity-100 transition-opacity"
                >
                  Fincas Admin Console ↗
                </a>
              </li>
            </ul>
          </div>

          <div>
            <h4
              class="text-xs font-bold uppercase tracking-wider mb-3"
              style={{ color: "var(--color-t-bright, #ffffff)" }}
            >
              Enterprise Stack
            </h4>
            <ul class="space-y-2 text-xs opacity-75">
              <li>Python 3.12 & Django 5.1</li>
              <li>Qwik City 1.20 (Resumable)</li>
              <li>TailwindCSS & DaisyUI</li>
              <li>WeasyPrint PDF Invoicing</li>
              <li>PostgreSQL 16 & Valkey 7.2</li>
            </ul>
          </div>
        </div>

        <div
          class="mt-8 pt-8 border-t border-white/5 flex flex-col sm:flex-row items-center justify-between gap-4 text-xs"
          style={{ color: "var(--color-t-muted, #8a8f98)" }}
        >
          <div>© 2026 Fincas Project. Released under MIT License.</div>
          <div class="flex items-center gap-4">
            <span>Terms of Commerce</span>
            <span>Privacy Policy</span>
            <a
              href="https://github.com/Logos-Flux/fincas"
              target="_blank"
              rel="noreferrer"
              class="hover:underline"
            >
              GitHub Repository
            </a>
          </div>
        </div>
      </div>
    </footer>
  );
});
