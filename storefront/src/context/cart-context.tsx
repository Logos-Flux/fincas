import { createContextId } from "@builder.io/qwik";
import type { CartItem } from "../lib/api-client";

export interface CartStore {
  items: CartItem[];
  isOpen: boolean;
}

export const CartContext = createContextId<CartStore>("golden_cart_context");
