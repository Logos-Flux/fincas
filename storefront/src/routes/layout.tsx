import {
  component$,
  Slot,
  useContextProvider,
  useStore,
  useVisibleTask$,
} from "@builder.io/qwik";
import { useLocation } from "@builder.io/qwik-city";
import { Header } from "../components/header";
import { Footer } from "../components/footer";
import { CartDrawer } from "../components/cart-drawer";
import { CartContext, type CartStore } from "../context/cart-context";

export default component$(() => {
  const cart = useStore<CartStore>({
    items: [],
    isOpen: false,
  });

  useContextProvider(CartContext, cart);

  // Restore cart from localStorage if exists
  // eslint-disable-next-line qwik/no-use-visible-task
  useVisibleTask$(({ track }) => {
    track(() => cart.items);
    try {
      if (cart.items.length === 0) {
        const saved = localStorage.getItem("golden_cart");
        if (saved) {
          const parsed = JSON.parse(saved);
          if (Array.isArray(parsed)) {
            cart.items = parsed;
          }
        }
      } else {
        localStorage.setItem("golden_cart", JSON.stringify(cart.items));
      }
    } catch {
      // ignore
    }
  });

  // Landing-page templates are standalone sites: no store header or cart.
  const loc = useLocation();
  if (/^\/landing\/[^/]+/.test(loc.url.pathname)) {
    return <Slot />;
  }

  return (
    <div class="min-h-screen flex flex-col transition-colors duration-300">
      <Header />
      <CartDrawer />
      <main class="flex-1">
        <Slot />
      </main>
      <Footer />
    </div>
  );
});
