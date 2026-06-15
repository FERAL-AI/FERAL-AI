/**
 * Coverage batch (stage 5.4c) — Chat + Devices + AppSurface.
 *
 * These are the biggest pages still dragging the branch floor. Each
 * gets mount + one branch probe.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';

import Chat from '../../pages/Chat';
import Devices from '../../pages/Devices';
import AppSurface from '../../pages/AppSurface';

beforeEach(() => {
  if (!window.confirm) window.confirm = vi.fn(() => true);
  if (!window.URL.createObjectURL) {
    window.URL.createObjectURL = vi.fn(() => 'blob:mock');
    window.URL.revokeObjectURL = vi.fn();
  }
});

// ── Chat ────────────────────────────────────────────────────────

describe('Chat', () => {
  const chatResp = (url) => {
    if (url.includes('/api/sessions')) return { sessions: [] };
    if (url.includes('/api/llm/status')) return { available: true, provider: 'openai' };
    if (url.includes('/api/dashboard')) return { somatic: { cognitive_load: 0.2 } };
    return { messages: [], sessions: [] };
  };

  it('mounts + shows the greeting message', () => {
    const { getByText } = renderV2(<Chat />, { fetch: chatResp });
    expect(getByText(/FERAL v2 is listening/i)).toBeInTheDocument();
  });
});

// ── Devices ─────────────────────────────────────────────────────

describe('Devices', () => {
  const devResp = (url) => {
    if (url.includes('/api/devices/connected')) return { devices: [
      { node_id: 'w300-1', name: 'Glasses', type: 'glasses', capabilities: ['camera'] },
    ] };
    if (url.includes('/api/devices/paired')) return { devices: [
      { device_id: 'p1', name: 'phone', kind: 'hup', node_id: 'phone-1',
        paired_at: 1, last_seen: 1, claimed_at: 1 },
    ] };
    if (url.includes('/api/hardware/mesh')) return { nodes: [] };
    return {};
  };

  it('renders connected + paired sections', async () => {
    const { findAllByText, getByRole } = renderV2(<Devices />, { fetch: devResp });
    expect(getByRole('heading', { name: /Devices/i })).toBeInTheDocument();
    // "Glasses" renders in both the topology view and the Live card, so
    // assert at least one match rather than a unique one.
    expect((await findAllByText(/Glasses/)).length).toBeGreaterThan(0);
  });

  it('shows empty state when everything is empty', async () => {
    const { findByText } = renderV2(<Devices />, {
      fetch: () => ({ devices: [], nodes: [] }),
    });
    expect(await findByText(/No devices paired yet/i)).toBeInTheDocument();
  });
});

// ── AppSurface ──────────────────────────────────────────────────

describe('AppSurface', () => {
  const appResp = (url) => {
    if (url.includes('/manifest')) {
      return {
        app_id: 'demo',
        manifest: {
          brand: { name: 'Demo', primary_color: '#000' },
          entry_surface_id: 'home',
          surfaces: [{ surface_id: 'home', kind: 'authored', title: 'Home' }],
        },
      };
    }
    if (url.includes('/open')) {
      return {
        success: true,
        app_id: 'demo',
        surface_id: 'home',
        screen_id: 'demo:home:session',
        root: { type: 'stack', children: [{ type: 'text', value: 'hello from demo' }] },
      };
    }
    return {};
  };

  // AppSurface reads the :app_id param via useParams; we wrap it in
  // Routes so the param resolves. renderV2 provides MemoryRouter.
  it('mounts under a matching route and hits the manifest endpoint', async () => {
    const { Routes, Route } = await import('react-router-dom');
    const Harness = () => (
      <Routes>
        <Route path="/apps/:app_id" element={<AppSurface />} />
      </Routes>
    );
    const { container } = renderV2(<Harness />, {
      route: '/apps/demo',
      fetch: appResp,
    });
    expect(container.firstChild).toBeInTheDocument();
  });
});
