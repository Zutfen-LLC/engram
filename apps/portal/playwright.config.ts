import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the Portal browser smoke (ENG-PORTAL-001A).
 *
 * Browsers are provided by the CI runner (or a dedicated test container), NOT
 * by the production Portal image (alteration 7). The base URL targets a
 * locally-booted Portal (the hosted-smoke CI job boots the Compose overlay).
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:3000",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
