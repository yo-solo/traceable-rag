import { defineConfig, devices } from "@playwright/test";

const localChrome = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
const externalBaseUrl = process.env.PLAYWRIGHT_BASE_URL;
const baseURL = externalBaseUrl ?? "http://127.0.0.1:8000";

export default defineConfig({
  testDir: "./e2e",
  outputDir: "../.artifacts/playwright-results",
  reporter: [["line"], ["html", { open: "never", outputFolder: "../.artifacts/playwright-report" }]],
  use: {
    baseURL,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    launchOptions: localChrome ? { executablePath: localChrome } : undefined,
  },
  webServer: externalBaseUrl
    ? undefined
    : {
        command: ".venv/bin/python scripts/run_e2e_server.py",
        cwd: "..",
        url: `${baseURL}/api/health`,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
      },
  projects: [
    {
      name: "desktop-chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "compact-desktop-chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
    {
      name: "mobile-chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 390, height: 844 },
        deviceScaleFactor: 1,
        hasTouch: true,
        isMobile: true,
      },
    },
  ],
});
