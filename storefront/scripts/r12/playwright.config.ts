// Playwright project for release journey R12's last step, the first invoice
// (docs/getting-started.md step 7). Run by scripts/release/r12_fresh_install.sh,
// which installs Fincas from the page, starts its server and sets R12_*.
import { defineConfig } from "@playwright/test";
import path from "node:path";

const OUT = path.resolve(process.env.R12_ARTIFACTS ?? "test-results/r12");

export default defineConfig({
  testDir: ".",
  testMatch: "first-invoice.spec.ts",
  outputDir: OUT,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: process.env.R12_BASE_URL,
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
