/**
 * Ambient renders no event overlay, in any build.
 *
 * History: a faint column of WS event rows ("EVENT text_response" xN) in
 * the bottom-left of every shelled page was visible in the demo build.
 * The first fix gated Ambient's <LiveOpsStream> behind
 * `import.meta.env.DEV`, which is false under `vite build`. In 2026.8.12
 * the component was deleted outright rather than left dev-only: its
 * label table named thirteen frame types and the brain emits exactly one
 * of them (`tool_result`), its `hop === 'system'` branch tested for a
 * value `FeralMessage.hop` cannot hold, and it rendered aria-hidden
 * inside an aria-hidden layer. /timeline and /oversight are the real
 * activity surfaces.
 *
 * This test is the guard against re-mounting any such strip. It asserts
 * the absence under DEV=true as well as DEV=false, so a future
 * "just for dev" reintroduction fails here instead of shipping.
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

describe('Ambient — no event overlay', () => {
  for (const dev of [false, true]) {
    it(`renders no LiveOps strip when import.meta.env.DEV is ${dev}`, async () => {
      vi.stubEnv('DEV', dev);
      // Re-import after stubbing: any module-level env gate would be
      // captured once per module load.
      vi.resetModules();
      const { default: Ambient } = await import('../../shell/Ambient');

      const { container } = render(<Ambient />);
      expect(container.querySelector('[data-testid="v2-ambient-ops"]')).toBeNull();
      expect(container.querySelector('.v2-ambient-ops')).toBeNull();
      expect(container.querySelector('.v2-liveops')).toBeNull();
      // The background layer itself still renders.
      expect(container.querySelector('.v2-ambient')).not.toBeNull();
      expect(container.querySelector('.v2-ambient-field')).not.toBeNull();
    });
  }
});
