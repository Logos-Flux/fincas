import { component$ } from "@builder.io/qwik";

export interface DuotoneImageProps {
  /** Path under /public, e.g. "/landing/app/lake-dock.webp". */
  src: string;
  /** Empty string for a purely decorative image. */
  alt: string;
  width: number;
  height: number;
  /** Sizing/rounding for the frame; the image always covers it. */
  class?: string;
  /** CSS object-position, e.g. "50% 30%". */
  position?: string;
  /** Above-the-fold images should pass "eager". */
  loading?: "lazy" | "eager";
}

/**
 * A photo re-coloured by the active theme: the image keeps its light and dark
 * but takes its hue from --color-t-accent, so switching skins re-tints it like
 * everything else on the page. Pure CSS (grayscale + luminosity blend over an
 * accent fill), so it needs no per-theme assets.
 */
export const DuotoneImage = component$<DuotoneImageProps>((props) => (
  <div
    class={[
      "isolate overflow-hidden bg-t-accent",
      // Tailwind's `relative` would win over a caller's `absolute`/`fixed`,
      // so only default to it when the caller didn't position the frame.
      /\b(absolute|fixed|sticky)\b/.test(props.class ?? "") ? "" : "relative",
      props.class ?? "",
    ]}
  >
    <img
      src={props.src}
      alt={props.alt}
      width={props.width}
      height={props.height}
      loading={props.loading ?? "lazy"}
      decoding="async"
      class="absolute inset-0 h-full w-full object-cover grayscale contrast-[1.08] mix-blend-luminosity"
      style={props.position ? { objectPosition: props.position } : undefined}
    />
    {/* Settles the photo into the page: a soft wash of the theme's canvas. */}
    <div
      aria-hidden="true"
      class="absolute inset-0 bg-t-deep opacity-15 pointer-events-none"
    />
  </div>
));
