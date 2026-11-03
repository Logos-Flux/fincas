import { component$ } from "@builder.io/qwik";
import { Link } from "@builder.io/qwik-city";
import type { Crumb } from "../lib/seo-head";

/**
 * Visible breadcrumb trail. The last crumb is the current page (not a link);
 * pages emit the same list as BreadcrumbList JSON-LD through seo-head.
 */
export const Breadcrumbs = component$<{ crumbs: Crumb[] }>(({ crumbs }) => {
  return (
    <nav aria-label="Breadcrumb" class="text-xs opacity-70">
      <ol class="flex flex-wrap items-center gap-2">
        {crumbs.map((crumb, i) => {
          const last = i === crumbs.length - 1;
          return (
            <li key={`${crumb.url}-${i}`} class="flex items-center gap-2">
              {last ? (
                <span
                  aria-current="page"
                  class="font-medium truncate"
                  style={{ color: "var(--color-t-bright, #ffffff)" }}
                >
                  {crumb.name}
                </span>
              ) : (
                <>
                  <Link href={crumb.url} class="hover:underline">
                    {crumb.name}
                  </Link>
                  <span aria-hidden="true">/</span>
                </>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
});
