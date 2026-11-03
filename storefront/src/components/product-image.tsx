import { component$ } from "@builder.io/qwik";

export interface ProductImageProps {
  src: string | null;
  alt: string;
  width: number;
  height: number;
  class?: string;
  loading?: "lazy" | "eager";
}

/**
 * A product photo, or — when the ERP has none — a neutral token-coloured
 * placeholder. Never another product's photo: a borrowed image misdescribes
 * what the customer is buying.
 */
export const ProductImage = component$<ProductImageProps>((props) =>
  props.src ? (
    <img
      src={props.src}
      alt={props.alt}
      width={props.width}
      height={props.height}
      class={props.class}
      loading={props.loading ?? "lazy"}
    />
  ) : (
    <div
      role="img"
      aria-label={`${props.alt} (no photo yet)`}
      class={["grid place-items-center", props.class ?? ""]}
      style={{
        backgroundColor: "var(--color-t-surface, #0f1011)",
        color: "var(--color-t-muted, #8a8f98)",
      }}
    >
      <svg
        class="w-10 h-10 opacity-60"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        aria-hidden="true"
      >
        <path
          stroke-linecap="round"
          stroke-linejoin="round"
          stroke-width="1.5"
          d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"
        />
      </svg>
    </div>
  ),
);
