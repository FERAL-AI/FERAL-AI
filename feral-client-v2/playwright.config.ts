/**
 * Playwright config for the v2 e2e specs under ./e2e.
 *
 * This header used to defer to "the full program" and cite
 * `.github/workflows/v2-e2e.yml` as the thing that owned a multi-browser
 * matrix and real-brain wiring. That file has never existed in this repo.
 * The effect of the citation was that this config looked provisional and
 * nobody noticed the specs were running in no workflow at all. They are
 * now run by the `client-v2-e2e` job in .github/workflows/ci.yml and by
 * `make e2e`, both against this config.
 *
 * SCOPE, stated plainly rather than deferred: chromium only, and `/api/*`
 * is stubbed per-spec in every file EXCEPT `real_brain_pages.spec.ts` and
 * `real_brain_controls.spec.ts`. Those two stub nothing and walk every
 * destination against a live brain; they skip themselves unless
 * `FERAL_E2E_REAL_BRAIN=1` is set alongside a `FERAL_E2E_URL` pointing at
 * one, and CI runs them from the opt-in
 * `.github/workflows/v2-real-brain-e2e.yml` rather than from the required
 * gate, because they need a brain and the control walk mutates it.
 * A multi-browser matrix is still not covered here. That is a known gap,
 * not a hidden one.
 *
 * Behavior:
 *   - Chromium only.
 *   - baseURL reads from FERAL_E2E_URL so devs can point at any
 *     running instance (`npm run dev`, `npm run preview`, or a brain
 *     serving the bundled webui_v2).
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
