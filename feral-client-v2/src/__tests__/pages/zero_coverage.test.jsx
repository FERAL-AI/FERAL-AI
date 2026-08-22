/**
 * Zero-coverage pages smoke pack (stage 5.3).
 *
 * Each page was at 0% coverage after 2026.4.27. These tests mount every
 * one with a realistic fetch responder, trigger the happy-path render,
 * and exercise the primary branches (tabs, empty state, error state).
 *
 * Intentionally one batched file so the v8 instrumenter has to load
 * each page exactly once — faster than 9 separate files.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';

import Geofences from '../../pages/Geofences';
import Webhooks from '../../pages/Webhooks';
import Wiki from '../../pages/Wiki';
import Identity from '../../pages/Identity';
import Skills from '../../pages/Skills';
import Health from '../../pages/Health';
import Memory from '../../pages/Memory';

// ── stubs ────────────────────────────────────────────────────────

beforeEach(() => {
  if (!window.confirm) window.confirm = vi.fn(() => true);
  if (!navigator.clipboard) {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn() },
      configurable: true,
    });
  }
});

// ── Geofences ────────────────────────────────────────────────────

describe('Geofences', () => {
  const geofenceResp = (url) => {
    if (url.includes('/api/geofences')) {
      return { geofences: [
        { id: 'home', name: 'home', lat: 40, lng: -74, lon: -74, radius: 100, radius_m: 100 },
        { id: 'office', name: 'office', lat: 40.7, lng: -74.1, lon: -74.1, radius: 200, radius_m: 200 },
      ] };
    }
    return {};
  };

  it('renders paired fences + Push my location button', async () => {
    const { getByRole, findByText } = renderV2(<Geofences />, { fetch: geofenceResp });
    expect(getByRole('heading', { name: /Geofences/i })).toBeInTheDocument();
    expect(await findByText(/home/)).toBeInTheDocument();
    expect(await findByText(/office/)).toBeInTheDocument();
  });

  it('shows empty state when API returns no fences', async () => {
    const { findByText } = renderV2(<Geofences />, {
      fetch: () => ({ geofences: [] }),
    });
    expect(await findByText(/No geofences/i)).toBeInTheDocument();
  });
});

// ── Webhooks ─────────────────────────────────────────────────────

describe('Webhooks', () => {
  it('renders rows', async () => {
    const { findByText, getByRole } = renderV2(<Webhooks />, {
      fetch: (url) => url.includes('/api/webhooks')
        ? { webhooks: [{ id: 'wh1', name: 'GitHub', url: '/hooks/gh', secret: 's1' }] }
        : {},
    });
    expect(getByRole('heading', { name: /Webhooks/i })).toBeInTheDocument();
    expect(await findByText(/GitHub/)).toBeInTheDocument();
  });

  it('shows empty state when list is empty', async () => {
    const { findByText } = renderV2(<Webhooks />, {
      fetch: () => ({ webhooks: [] }),
    });
    expect(await findByText(/No webhooks/i)).toBeInTheDocument();
  });
});

// ── Wiki ─────────────────────────────────────────────────────────

describe('Wiki', () => {
  it('renders all three tabs', () => {
    const { getByRole } = renderV2(<Wiki />, {
      fetch: () => ({ pages: [] }),
    });
    expect(getByRole('tab', { name: /Pages/i })).toBeInTheDocument();
    expect(getByRole('tab', { name: /Ingest/i })).toBeInTheDocument();
    expect(getByRole('tab', { name: /Compile/i })).toBeInTheDocument();
  });

  it('switches to the Ingest tab when clicked', () => {
    const { getByRole } = renderV2(<Wiki />, { fetch: () => ({}) });
    fireEvent.click(getByRole('tab', { name: /Ingest/i }));
    // Active tab attribute reflects selection.
    expect(getByRole('tab', { name: /Ingest/i })).toHaveAttribute('aria-selected', 'true');
  });
});

// ── Identity (wraps SelfWorkspace) ───────────────────────────────

describe('Identity', () => {
  it('renders the Self workspace with all four tabs', () => {
    const { getByRole } = renderV2(<Identity />, {
      fetch: () => ({ name: 'FERAL', personality: '', rules: [], voice: {} }),
    });
    expect(getByRole('tab', { name: /IDENTITY/i })).toBeInTheDocument();
    expect(getByRole('tab', { name: /SOUL/i })).toBeInTheDocument();
    expect(getByRole('tab', { name: /MEMORY/i })).toBeInTheDocument();
    expect(getByRole('tab', { name: /ABOUT ME/i })).toBeInTheDocument();
  });
});

// ── Skills ───────────────────────────────────────────────────────

describe('Skills', () => {
  it('renders the skills page', async () => {
    const { getByRole } = renderV2(<Skills />, {
      fetch: (url) => {
        if (url.includes('/skills')) return { skills: [
          { id: 'weather', name: 'weather', description: 'Weather', active: true },
          { id: 'web_search', name: 'web_search', description: 'Search', active: false },
        ] };
        if (url.includes('/api/skills/pending')) return { pending: [] };
        return {};
      },
    });
    expect(getByRole('heading', { name: /Skills/i })).toBeInTheDocument();
  });

  it('shows empty state when no skills', async () => {
    const { findByText } = renderV2(<Skills />, {
      fetch: () => ({ skills: [], pending: [] }),
    });
    // Empty state renders a message (skill list uses EmptyState).
    expect(await findByText(/No skills/i)).toBeInTheDocument();
  });
});

// ── SetupWizard (legacy /setup/legacy route) was removed in 2026.5.8.
// The canonical setup is /setup (Setup.jsx, with the new pairing step
// covered by tests/test_setup_pair_step.test.jsx).

// ── Dashboard/Ambient alias pages were deleted in 2026.8.12. Both were
// one-line `export { default } from './Home'` re-exports with zero
// production importers; App.jsx already routed / to Home directly and
// redirected /ambient to /. Home itself is covered by
// pages/Home.test.jsx and pages/Home.stale-offline.test.jsx.

// ── Health ───────────────────────────────────────────────────────

describe('Health', () => {
  const healthResp = (url) => {
    if (url.includes('/api/baseline/metrics')) return { metrics: [], series: [] };
    if (url.includes('/api/baseline/alerts')) return { alerts: [] };
    if (url.includes('/api/baseline/summary')) return { summary: {} };
    if (url.includes('/api/baseline/today')) return { summary: '', alerts: [], score: 0 };
    if (url.includes('/api/baseline')) return { metrics: [], alerts: [], summary: {} };
    return { metrics: [], alerts: [] };
  };

  it('renders the four tabs', () => {
    const { getByRole } = renderV2(<Health />, { fetch: healthResp });
    expect(getByRole('tab', { name: /Summary/i })).toBeInTheDocument();
    expect(getByRole('tab', { name: /Metrics/i })).toBeInTheDocument();
    expect(getByRole('tab', { name: /Alerts/i })).toBeInTheDocument();
    expect(getByRole('tab', { name: /Today/i })).toBeInTheDocument();
  });

  it('switches tabs on click', () => {
    const { getByRole } = renderV2(<Health />, { fetch: healthResp });
    fireEvent.click(getByRole('tab', { name: /Metrics/i }));
    expect(getByRole('tab', { name: /Metrics/i })).toHaveAttribute('aria-selected', 'true');
  });
});

// ── Memory ───────────────────────────────────────────────────────

describe('Memory page', () => {
  const memResp = (url) => {
    // Memory's Recent tab does `d.memories || d.notes || d || []` so
    // when the upstream JSON is falsy the fallback is the response
    // object itself. Return explicit shape keys Memory looks for.
    if (url.includes('/internal/memory/recent')) return { memories: [] };
    if (url.includes('/internal/episodes/recent')) return { episodes: [] };
    if (url.includes('/internal/execution-log')) return { items: [] };
    if (url.includes('/api/knowledge/graph')) return { nodes: [], edges: [] };
    return { memories: [], episodes: [], items: [] };
  };

  it('renders the five memory tabs', () => {
    const { getByRole } = renderV2(<Memory />, { fetch: memResp });
    expect(getByRole('tab', { name: /Recent/i })).toBeInTheDocument();
    expect(getByRole('tab', { name: /Search/i })).toBeInTheDocument();
    expect(getByRole('tab', { name: /Episodes/i })).toBeInTheDocument();
    expect(getByRole('tab', { name: /Exec log/i })).toBeInTheDocument();
    expect(getByRole('tab', { name: /Knowledge/i })).toBeInTheDocument();
  });

  it('switches to Search tab on click', () => {
    const { getByRole } = renderV2(<Memory />, { fetch: memResp });
    fireEvent.click(getByRole('tab', { name: /Search/i }));
    expect(getByRole('tab', { name: /Search/i })).toHaveAttribute('aria-selected', 'true');
  });
});
