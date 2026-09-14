import { defineConfig } from "@playwright/test";

const webURL = "http://localhost:3210";
const e2eDbPath = `apps/web/test-results/e2e-api-${Date.now()}.db`;
process.env.E2E_DB_PATH = e2eDbPath;

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
      env: {
        SCHOOLSIFT_DATABASE_PATH: e2eDbPath,
      },
      url: "http://localhost:8000/health",
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
