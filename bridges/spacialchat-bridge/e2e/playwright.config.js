// @ts-check
import { defineConfig } from "@playwright/test";

const PORT = 8099;

export default defineConfig({
  testDir: ".",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    headless: true,
    permissions: ["clipboard-read", "clipboard-write"],
  },
  webServer: {
    command: `node serve.mjs ${PORT}`,
    url: `http://127.0.0.1:${PORT}/popup.html`,
    reuseExistingServer: !process.env.CI,
    timeout: 20000,
  },
});
