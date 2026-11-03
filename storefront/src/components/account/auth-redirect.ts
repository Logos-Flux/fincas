import { getAuthProvider, loginPathFor } from "../../lib/auth";
import { PortalError } from "../../lib/portal-client";

/**
 * Send the visitor to sign in, coming back to `currentPath` afterwards.
 *
 * A full-page `location.replace`, not Qwik City's `nav()`: a `nav()` issued
 * from the gate's visible task right after a client-side navigation into
 * /account (e.g. the header link after signing out) never completed, leaving
 * the visitor on "Checking your session…". There is no session to keep at
 * this point, so the reload costs nothing but a page load.
 */
export function redirectToLogin(currentPath: string, reason?: "expired"): void {
  const target =
    loginPathFor(currentPath) + (reason ? `&reason=${reason}` : "");
  window.location.replace(target);
}

/**
 * Shared handling for a failed portal call: an unauthorized answer clears
 * the local session and sends the customer to sign in (coming back to the
 * same page); anything else is returned as a message for an error state.
 */
export async function portalFailure(
  err: unknown,
  currentPath: string,
): Promise<string | null> {
  if (err instanceof PortalError && err.kind === "unauthorized") {
    try {
      await getAuthProvider().signOut();
    } catch {
      /* already signed out locally */
    }
    redirectToLogin(currentPath, "expired");
    return null;
  }
  if (err instanceof PortalError) return err.message;
  return "Something went wrong loading your account. Please try again.";
}
