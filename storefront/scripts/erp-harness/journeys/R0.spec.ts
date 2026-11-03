// R0: the harness's own self-test. Proves the server-error capture: a 500
// the page never shows (fetched from script, swallowed like a toast) must
// still fail a step, and a visible 500 fails through the page checks too.
// These steps carry expectServerError, so the journey PASSES when the
// capture works and FAILS when it misses.
import { journey, expect } from "../lib/harness";

journey("R0", "Server-error capture self-test", (step) => {
  step(1, "sign in as staff and open the dashboard", async ({ page, login }) => {
    await login("staff");
    await page.goto("/erp/");
    await expect(page).toHaveURL(/\/erp\/?$/);
  });

  step(
    2,
    "a hidden 500 (fetched from the page, result swallowed) fails the step",
    async ({ page }) => {
      // The page stays on the dashboard and looks fine; only the server log
      // knows /harness/boom/ redirected to a view that raised.
      const status = await page.evaluate(async () => {
        const r = await fetch("/harness/boom/", { redirect: "follow" });
        return r.status;
      });
      expect(status).toBe(500);
      await expect(page).toHaveURL(/\/erp\/?$/);
    },
    { expectServerError: true, axe: false, fit360: false },
  );

  step(
    3,
    "a visible 500 (navigating to the redirect) is caught as well",
    async ({ page }) => {
      await page.goto("/harness/boom/");
      await page.goto("/erp/");
    },
    { expectServerError: true, axe: false, fit360: false },
  );
});
