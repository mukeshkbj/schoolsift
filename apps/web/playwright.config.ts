import { defineConfig } from "@playwright/test";

const webURL = "http://localhost:3210";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: 1,
  use: {
    baseURL: webURL,
  },
  webServer: [
    {
      command: "pnpm exec next dev -p 3210",
      url: webURL,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command:
        "uv run uvicorn schoolsift.api:create_app --factory --port 8000",
      cwd: "../..",
      url: "http://localhost:8000/health",
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
