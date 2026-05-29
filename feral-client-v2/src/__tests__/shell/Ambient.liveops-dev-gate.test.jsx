/**
 * Dev-only LiveOps debug strip.
 *
 * The faint column of WS event rows ("EVENT text_response" ×N) in the
 * bottom-left of every shelled page was visible in the demo build. The
 * fix gates Ambient's <LiveOpsStream> behind `import.meta.env.DEV`,
 * which is `false` in `vite build` and therefore the production bundle
 * served to the user. We stub the env value in this test so we exercise
 * the same code path the production bundle hits regardless of how
 * vitest happens to set `DEV` in its harness.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render } from '@testing-library/react';
import { StubWebSocket } from '../_helpers/renderV2';

beforeEach(() => {
  if (!window.matchMedia) {
    window.matchMedia = vi.fn().mockImplementation((q) => ({
      matches: false, media: q, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
  }
  StubWebSocket.instances = [];
  vi.stubGlobal('WebSocket', StubWebSocket);
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe('Ambient — LiveOps debug strip gating', () => {
  it('does NOT render the LiveOps event strip when import.meta.env.DEV is false', async () => {
    vi.stubEnv('DEV', false);
    // Re-import after stubbing so the module-level
    // SHOW_LIVE_OPS constant sees the patched env. The gate value is
    // captured once per module load.
    vi.resetModules();
    const { default: Ambient } = await import('../../shell/Ambient');

    const { container } = render(<Ambient />);
    expect(container.querySelector('[data-testid="v2-ambient-ops"]')).toBeNull();
    expect(container.querySelector('.v2-liveops')).toBeNull();
  });

  it('DOES render the strip when import.meta.env.DEV is true (sanity check)', async () => {
    vi.stubEnv('DEV', true);
    vi.resetModules();
    const { default: Ambient } = await import('../../shell/Ambient');

    const { container } = render(<Ambient />);
    expect(container.querySelector('[data-testid="v2-ambient-ops"]')).not.toBeNull();
  });
});
