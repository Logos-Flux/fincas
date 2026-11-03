import { component$, Slot } from "@builder.io/qwik";

/** The card surface every /account section sits on. */
export const Panel = component$<{ flush?: boolean }>((props) => (
  <section
    class={[
      "rounded-2xl border",
      props.flush ? "overflow-hidden" : "p-5 sm:p-6",
    ]}
    style={{
      backgroundColor: "var(--color-t-card, #191a1b)",
      borderColor: "var(--color-t-border, rgba(255, 255, 255, 0.08))",
    }}
  >
    <Slot />
  </section>
));

/** Loading / error / empty message inside a panel. */
export const Notice = component$<{ tone?: "status" | "alert" }>(
  ({ tone = "status" }) => (
    <p
      role={tone}
      class="text-sm"
      style={{
        color:
          tone === "alert"
            ? "var(--color-t-bright, #f7f8f8)"
            : "var(--color-t-text, #d0d6e0)",
      }}
    >
      <Slot />
    </p>
  ),
);
