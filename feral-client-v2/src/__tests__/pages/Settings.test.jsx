import { describe, it, expect } from 'vitest';
import { fireEvent, waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Settings from '../../pages/Settings';

const catalogProviders = [
  {
    id: 'openai',
    display_name: 'OpenAI',
    supports_local: false,
    requires_api_key: true,
    configured: true,
    reachable: true,
 default_base_url: 'https://api.openai.com/v1',
    default_model: 'gpt-4o-mini',
    credential_env_var: 'OPENAI_API_KEY',
    aliases: ['open ai'],
  },
  {
    id: 'anthropic',
    display_name: 'Anthropic',
    supports_local: false,
    requires_api_key: true,
    configured: false,
    reachable: null,
 default_base_url: 'https://api.anthropic.com/v1',
    default_model: 'claude-sonnet-4-20250514',
    credential_env_var: 'ANTHROPIC_API_KEY',
    aliases: [],
  },
  {
    id: 'ollama',
    display_name: 'Ollama',
    supports_local: true,
    requires_api_key: false,
    configured: true,
    reachable: false,
 default_base_url: 'http://localhost:11434/v1',
    default_model: 'llama3',
    credential_env_var: '',
    aliases: ['local'],
  },
];

const healthSnapshot = {
  active: { provider: 'openai', model: 'gpt-4o-mini', has_key: true, available: true, base_url: '' },
  candidates: [
    { provider: 'openai', model: 'gpt-4o-mini', has_key: true, in_cooldown: false, cooldown_remaining: 0 },
    { provider: 'anthropic', model: 'claude', has_key: false, in_cooldown: false, cooldown_remaining: 0 },
    { provider: 'ollama', model: 'llama3', has_key: true, in_cooldown: true, cooldown_remaining: 42 },
  ],
  fallback_providers: ['ollama'],
  total_available: 1,
};

function providersResponder(url) {
  if (url.includes('/api/llm/providers')) {
    return { providers: catalogProviders, count: catalogProviders.length };
  }
  if (url.includes('/api/llm/status')) {
    return { available: true, provider: 'openai', model: 'gpt-4o-mini' };
  }
  if (url.includes('/api/llm/presets')) {
    return { presets: [{ id: 'openai_default', label: 'OpenAI default' }] };
  }
  if (url.includes('/api/llm/health')) {
    return healthSnapshot;
  }
  if (url.includes('/api/memory/backend')) {
    return {
      backend: 'sqlite_vec',
      runtime: 'sqlite_vec',
      active_store: 'sqlite_vec',
      pending_unapplied: false,
      fell_back: false,
      boot_error: null,
      available: { sqlite_vec: true, chroma: false, qdrant: false },
    };
  }
  if (url.includes('/api/identity')) {
    return { name: 'FERAL', personality: '', rules: [], greeting_style: '', voice: {} };
  }
  if (url.includes('/api/about-me')) {
    return { facts: [] };
  }
  return {};
}

describe('Settings', () => {
  it('renders the settings split layout with all sections including Self + Twin', () => {
    const { getAllByText, getByText } = renderV2(<Settings />);
    expect(getAllByText(/^Self$/i).length).toBeGreaterThan(0);
    for (const s of ['General', 'Providers', 'Memory', 'Channels', 'Autonomy', 'Voice', 'Access', 'Twin']) {
      expect(getByText(s)).toBeInTheDocument();
    }
  });

  it('exposes a Self button in the settings nav that opens the Self editors', () => {
    const { getAllByText } = renderV2(<Settings />);
    expect(getAllByText(/IDENTITY \/ SOUL \/ MEMORY \/ ABOUT-ME/i).length).toBeGreaterThan(0);
  });

  it('exposes an ABOUT ME tab inside the Self workspace', () => {
    const { getByRole } = renderV2(<Settings />);
    expect(getByRole('tab', { name: /ABOUT ME/i })).toBeInTheDocument();
  });

  it('Providers section renders the catalog card grid + current-provider banner', async () => {
    const { getByText, findAllByText, findByText } = renderV2(<Settings />, {
      fetch: providersResponder,
    });
 // Switch to Providers section.
    fireEvent.click(getByText(/^Providers$/));
 // Current provider card.
    expect(await findByText(/Current provider/i)).toBeInTheDocument();
 // Every built-in descriptor exposed (sanity check a few).
    expect((await findAllByText(/OpenAI/i)).length).toBeGreaterThan(0);
    expect((await findAllByText(/Anthropic/i)).length).toBeGreaterThan(0);
    expect((await findAllByText(/Ollama/i)).length).toBeGreaterThan(0);
  });

  it('Providers section exposes a Fallbacks card with cooldown state', async () => {
    const { getByText, findByText } = renderV2(<Settings />, {
      fetch: providersResponder,
    });
    fireEvent.click(getByText(/^Providers$/));
    expect(await findByText(/Fallbacks/i)).toBeInTheDocument();
 // Cooldown hint from health_snapshot (ollama in_cooldown).
    expect(await findByText(/cooling down 42s/)).toBeInTheDocument();
  });

  it('Providers section shows preset buttons when the API returns presets', async () => {
    const { getByText, findByText } = renderV2(<Settings />, {
      fetch: providersResponder,
    });
    fireEvent.click(getByText(/^Providers$/));
    expect(await findByText(/OpenAI default/)).toBeInTheDocument();
  });

  it('Memory section renders the backend picker', async () => {
    const { getByText, findAllByText } = renderV2(<Settings />, {
      fetch: providersResponder,
    });
    fireEvent.click(getByText(/^Memory$/));
 // The MemorySection renders the runtime backend ("In use now") plus a
 // row per available backend, so sqlite_vec appears more than once.
    expect((await findAllByText(/sqlite_vec/i)).length).toBeGreaterThan(0);
  });

  it('Access section renders current mode and tailscale snapshot', async () => {
    const fetcher = (url) => {
      if (url.includes('/api/access/status')) {
        return {
          pairing_mode: 'localhost',
          remote_url: '',
          tailscale: { installed: true, running: true, logged_in: false, dns_name: '', tailnet: '', error: 'not_logged_in' },
          funnel: { active: false, ports: [] },
        };
      }
      return providersResponder(url);
    };
    const { getByText, findByText, findByTestId } = renderV2(<Settings />, { fetch: fetcher });
    fireEvent.click(getByText(/^Access$/));
    expect(await findByText(/Current pairing mode/i)).toBeInTheDocument();
    expect(await findByTestId('settings-access-section')).toBeInTheDocument();
    expect(await findByTestId('settings-access-mode-localhost')).toBeInTheDocument();
  });

  it('Access section remote-up action calls backend endpoint', async () => {
    const calls = [];
    let statusSnapshot = {
      pairing_mode: 'localhost',
      remote_url: '',
      tailscale: { installed: true, running: true, logged_in: true, dns_name: 'macbook.tailnet.ts.net', tailnet: 'tailnet.ts.net', error: '' },
      funnel: { active: false, ports: [] },
    };
    const fetcher = (url, init) => {
      calls.push({ url, method: init?.method || 'GET' });
      if (url.includes('/api/access/status')) return statusSnapshot;
      if (url.includes('/api/access/remote-up')) {
        statusSnapshot = {
          ...statusSnapshot,
          pairing_mode: 'remote',
 remote_url: 'https://macbook.tailnet.ts.net',
          funnel: { active: true, ports: [9090] },
        };
 return { ok: true, pairing_mode: 'remote', remote_url: 'https://macbook.tailnet.ts.net' };
      }
      return providersResponder(url);
    };
    const { getByText, findByTestId } = renderV2(<Settings />, { fetch: fetcher });
    fireEvent.click(getByText(/^Access$/));
    fireEvent.click(await findByTestId('settings-access-remote-up'));
    await waitFor(() => {
      expect(calls.some((c) => c.url.includes('/api/access/remote-up') && c.method === 'POST')).toBe(true);
    });
    expect(await findByTestId('settings-access-message')).toBeInTheDocument();
  });

 // ── Twin honesty (no executor wired → no theatre) ────────────
 // Pre-2026.4.29 the Twin section rendered nine canned domains
 // regardless of whether a backing executor existed. These tests pin
 // the new contract: empty payload → empty-state, wired executor →
 // toggles.

  it('Twin section renders the empty-state when /api/twin/policies returns empty', async () => {
    const fetcher = (url) => {
      if (url.includes('/api/twin/policies')) {
        return { policies: [], disconnected: [], available: [] };
      }
      if (url.includes('/api/twin/approvals')) return { approvals: [] };
      if (url.includes('/api/supervisor/stats')) return { paused: false };
      return providersResponder(url);
    };
    const { getByText, findByTestId, queryByText } = renderV2(<Settings />, {
      fetch: fetcher,
    });
    fireEvent.click(getByText(/^Twin$/));
    expect(await findByTestId('twin-empty-state')).toBeInTheDocument();
 // None of the canned domain labels should appear when nothing is wired.
    await waitFor(() => {
      expect(queryByText(/Respond to iMessage/i)).toBeNull();
      expect(queryByText(/Reply on Slack/i)).toBeNull();
    });
  });

  it('Twin section renders rows + toggles when a real executor exists', async () => {
    const fetcher = (url) => {
      if (url.includes('/api/twin/policies')) {
        return {
          policies: [
            {
              domain: 'reply_slack',
              mode: 'draft_only',
              time_windows: [],
              max_per_day: 10,
              requires_user_online: false,
              wired: true,
              label: '',
            },
          ],
          disconnected: [],
          available: [{ domain: 'reply_slack', label: '' }],
        };
      }
      if (url.includes('/api/twin/approvals')) return { approvals: [] };
      if (url.includes('/api/supervisor/stats')) return { paused: false };
      return providersResponder(url);
    };
    const { getByText, findAllByText, findByText, queryByTestId } = renderV2(
      <Settings />,
      { fetch: fetcher },
    );
    fireEvent.click(getByText(/^Twin$/));
 // The row label must render and the empty-state must NOT.
    expect((await findAllByText(/Reply on Slack/i)).length).toBeGreaterThan(0);
    expect(queryByTestId('twin-empty-state')).toBeNull();
 // Draft / Auto / Off toggles render against the wired row.
    expect(await findByText('Draft')).toBeInTheDocument();
    expect(await findByText('Auto')).toBeInTheDocument();
    expect(await findByText('Off')).toBeInTheDocument();
  });

  it('Twin section dims a row that lost its executor (disconnected bucket)', async () => {
    const fetcher = (url) => {
      if (url.includes('/api/twin/policies')) {
        return {
          policies: [],
          disconnected: [
            {
              domain: 'reply_slack',
              mode: 'auto_send',
              time_windows: [],
              max_per_day: 10,
              requires_user_online: false,
              wired: false,
              label: '',
            },
          ],
          available: [],
        };
      }
      if (url.includes('/api/twin/approvals')) return { approvals: [] };
      if (url.includes('/api/supervisor/stats')) return { paused: false };
      return providersResponder(url);
    };
    const { getByText, findByTestId, findAllByText } = renderV2(<Settings />, {
      fetch: fetcher,
    });
    fireEvent.click(getByText(/^Twin$/));
    expect(await findByTestId('twin-disconnected')).toBeInTheDocument();
    expect((await findAllByText(/Disconnected/i)).length).toBeGreaterThan(0);
  });

  // ── Twin no-theatre contract (Roadmap §A.5) ─────────────
 // The three tests below pin the no-theatre contract: when the
 // backend has zero configured executors the Settings → Twin block
 // is allowed to render copy + a CTA, but it MUST NOT render the
 // Pause/Resume kill switch (there is nothing to pause), and
 // available-but-not-yet-connected executors render a Connect
 // affordance with zero toggles. When at least one executor is
 // configured the kill switch must be present and clicking it
 // must hit the supervisor pause endpoint (the kill switch covers
 // the whole supervisor — twin + orchestrator — there is no
 // narrower /api/twin/pause route by design).

  it('twin-empty-state contract: empty policies AND empty available hides the Pause/Resume kill switch', async () => {
    const fetcher = (url) => {
      if (url.includes('/api/twin/policies')) {
 // Mirrors the integrations/twin/status `{ configured: [] }`
 // contract: when the backend has no wired executors at all,
 // both `policies` and `available` come back empty.
        return { policies: [], disconnected: [], available: [] };
      }
      if (url.includes('/api/twin/approvals')) return { approvals: [] };
      if (url.includes('/api/supervisor/stats')) return { paused: false };
      return providersResponder(url);
    };
    const {
      getByText,
      findByTestId,
      queryByTestId,
      queryByRole,
    } = renderV2(<Settings />, { fetch: fetcher });
    fireEvent.click(getByText(/^Twin$/));
    expect(await findByTestId('twin-empty-state')).toBeInTheDocument();
 // No kill-switch container.
    expect(queryByTestId('twin-kill-switch')).toBeNull();
 // No Pause / Resume button accessible by role.
    expect(queryByRole('button', { name: /Pause all actions/i })).toBeNull();
    expect(queryByRole('button', { name: /Resume all actions/i })).toBeNull();
  });

  it('twin-non-configured-toggle-absent contract: an "available" row offers a single Connect button and zero checkboxes', async () => {
    const fetcher = (url) => {
      if (url.includes('/api/twin/policies')) {
 // `available` lists a wired executor that the user has not
 // turned into a stored policy yet. This bucket must NEVER
 // render a toggle / checkbox — only a Connect affordance
 // that points the user at Channels / Integrations.
        return {
          policies: [],
          disconnected: [],
          available: [{ domain: 'reply_slack', label: '' }],
        };
      }
      if (url.includes('/api/twin/approvals')) return { approvals: [] };
      if (url.includes('/api/supervisor/stats')) return { paused: false };
      return providersResponder(url);
    };
    const {
      getByText,
      findByRole,
      findByTestId,
      container,
    } = renderV2(<Settings />, { fetch: fetcher });
    fireEvent.click(getByText(/^Twin$/));
 // Expand the default-collapsed Available executors panel.
    const expander = await findByRole('button', {
      name: /Available executors/i,
    });
    fireEvent.click(expander);
    const row = await findByTestId('twin-available-row-reply_slack');
    expect(row).toBeInTheDocument();
 // Exactly one Connect button on the row.
    const connectBtns = row.querySelectorAll('button');
    const connectMatches = Array.from(connectBtns).filter(
      (b) => /^Connect/i.test((b.textContent || '').trim()),
    );
    expect(connectMatches.length).toBe(1);
 // Zero <input type="checkbox" /> in the entire Twin block — the
 // contract bans toggles on non-configured rows and there should
 // be none anywhere else in the section either.
    expect(container.querySelectorAll('input[type="checkbox"]').length).toBe(0);
  });

  it('twin-kill-switch-conditional contract: a configured executor renders the kill switch and clicking it pauses the supervisor', async () => {
 // The contract bullet says "POST /api/twin/pause" but no such
 // narrower route exists by design — the kill switch covers the
 // whole supervisor (twin + orchestrator dispatch), so the v2
 // client posts to /api/supervisor/pause. We assert the actual
 // canonical endpoint instead of weakening the test.
    const calls = [];
    const fetcher = (url, init) => {
      calls.push({ url, method: init?.method || 'GET' });
      if (url.includes('/api/twin/policies')) {
        return {
          policies: [
            {
              domain: 'reply_slack',
              mode: 'draft_only',
              time_windows: [],
              max_per_day: 10,
              requires_user_online: false,
              wired: true,
              label: '',
            },
          ],
          disconnected: [],
          available: [{ domain: 'reply_slack', label: '' }],
        };
      }
      if (url.includes('/api/twin/approvals')) return { approvals: [] };
      if (url.includes('/api/supervisor/stats')) return { paused: false };
      if (url.includes('/api/supervisor/pause')) return { paused: true };
      return providersResponder(url);
    };
    const { getByText, findByRole } = renderV2(<Settings />, {
      fetch: fetcher,
    });
    fireEvent.click(getByText(/^Twin$/));
    const pauseBtn = await findByRole('button', { name: /Pause all actions/i });
    expect(pauseBtn).toBeInTheDocument();
    fireEvent.click(pauseBtn);
    await waitFor(() => {
      const hit = calls.find(
        (c) => c.url.includes('/api/supervisor/pause') && c.method === 'POST',
      );
      expect(hit).toBeTruthy();
    });
  });
});
