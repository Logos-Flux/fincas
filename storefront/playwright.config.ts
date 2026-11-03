import { defineConfig, devices } from "@playwright/test";

// Two CI runner instances share one host, so a fixed dev-server port can be
// taken by the other job. CI sets PW_PORT to a free port per run.
const port = process.env.PW_PORT || "5173";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  use: {
    baseURL: process.env.STOREFRONT_URL || `http://localhost:${port}`,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: process.env.PW_NO_SERVER
    ? undefined
    : {
        command: `bun run dev -- --port ${port} --strictPort`,
        url: `http://localhost:${port}`,
        reuseExistingServer: !process.env.CI,
        timeout: 120000,
      },
});
