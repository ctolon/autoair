import { defineConfig } from "@playwright/test";

// Requires a running Airflow with the plugin: ../scripts/dev-airflow.sh
export default defineConfig({
  testDir: "e2e",
  // The visual suite has its own hermetic config (playwright.visual.config.ts).
  testIgnore: ["visual/**"],
  timeout: 120_000,
  use: {
    baseURL: process.env.AIRFLOW_URL ?? "http://localhost:8080",
    channel: "chrome",
    headless: true,
    viewport: { width: 1600, height: 1000 },
    screenshot: "only-on-failure",
  },
  outputDir: "e2e/.results",
});
