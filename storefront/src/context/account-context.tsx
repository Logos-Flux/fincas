import { createContextId } from "@builder.io/qwik";
import type { PortalProfile } from "../lib/portal-client";

export interface AccountStore {
  /** "checking" until the auth adapter has answered; pages wait for "ready". */
  status: "checking" | "ready";
  email: string;
  /** Loaded once by the account layout; null until then or if it failed. */
  profile: PortalProfile | null;
  profileError: string;
}

export const AccountContext = createContextId<AccountStore>("account-context");
