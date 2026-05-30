/**
 * Minimal Playwright config used by the pair-device regression spec.
 *
 * NOT the full v2 e2e setup — the full program owns that bigger surface
 * (`.github/workflows/v2-e2e.yml`, multi-browser matrix, full webServer
 * wiring against the brain). We deliberately keep this tight so it
 * can stand on its own when run as `npx playwright test pair_device`
 * without dragging the rest of the e2e program in.
 *
 * Behavior:
 *   - Chromium only (just needs to repro the modal-stacking bug).
 *   - baseURL reads from FERAL_E2E_URL so devs can point at any
 *     running instance (`pnpm dev` or `vite preview`).
 *   - When FERAL_E2E_URL is unset, vite preview is started on 5173
 *     against the production-style bundle.
 */
import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.FERAL_E2E_URL || 'http://127.0.0.1:5173';
const startWebServer = !process.env.FERAL_E2E_URL;

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL,
    headless: true,
    viewport: { width: 1280, height: 800 },
    actionTimeout: 5_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: startWebServer
    ? {
      command: 'npm run preview -- --host 127.0.0.1 --port 5173 --strictPort',
      url: baseURL,
      reuseExistingServer: true,
      timeout: 60_000,
    }
    : undefined,
});
