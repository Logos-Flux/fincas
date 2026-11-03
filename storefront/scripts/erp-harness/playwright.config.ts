// Playwright project for the back-office release harness
// (docs/release-testing.md). Run through scripts/harness/run.sh, which starts
// the Django server on config.settings.harness and sets the HARNESS_* env.
// Every artefact goes under HARNESS_RUN_DIR (.runs/<sha>/), gitignored.
import { defineConfig } from "@playwright/test";
import path from "node:path";

const RUN_DIR = path.resolve(
  process.env.HARNESS_RUN_DIR ?? "test-results/erp-harness",
);
const BASE = process.env.HARNESS_BASE_URL ?? "http://127.0.0.1:8771";

export default defineConfig({
  testDir: "./journeys",
  outputDir: path.join(RUN_DIR, "test-results"),
  // Journeys are serial state machines: one worker, no parallelism.
  fullyParallel: false,
  workers: 1,
  retries: Number(process.env.HARNESS_RETRIES ?? 0),
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: [
    ["list"],
    ["json", { outputFile: path.join(RUN_DIR, "playwright.json") }],
    [
      "html",
      { outputFolder: path.join(RUN_DIR, "playwright-report"), open: "never" },
    ],
  ],
  use: {
    baseURL: BASE,
    viewport: { width: 1440, height: 900 },
    // The step runner keeps its own trace chunks, videos and screenshots per
    // failed step (lib/harness.ts) on the context it creates; Playwright's
    // own tracing would start a second trace on that context and collide.
    trace: "off",
    screenshot: "off",
    video: "off",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
