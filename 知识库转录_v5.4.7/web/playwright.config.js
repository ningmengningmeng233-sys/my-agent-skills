import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 120000,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:5888',
    channel: 'msedge',
    headless: true,
    viewport: { width: 1280, height: 800 },
  },
  webServer: undefined,
});
