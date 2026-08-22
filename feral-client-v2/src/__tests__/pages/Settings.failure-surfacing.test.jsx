/**
 * Settings — controls that failed without saying so.
 *
 * Every case here was reproduced against a live brain by clicking the
 * control in a browser and re-reading the brain's own state over HTTP.
 *
 *   Push / "Send test"   — `testSend` discarded the response and set
 *     "Test push sent." in the green chip unconditionally. The brain
 *     answered 200 with {"success": false, "sent": 0, "failed": 1,
 *     "degraded": ["no push credentials configured …", "apns device …:
 *     APNs key not configured"]}. Nothing left the machine. `degraded`
 *     carries no `error` key so `apiFetch` does not throw on it either.
 *
 *   Push / "Register"    — `r.ok ? … : …` with no catch. `apiFetch`
 *     throws on non-2xx, so the false branch was unreachable and a
 *     failure produced an unhandled rejection and no UI change.
 *
 *   General / feature toggles — `try { await update(...) } finally {}`
 *     with no catch. Same shape: refused toggle, silent.
 *
 *   Autonomy / tier      — `if (r.ok) setMode(next)`, unreachable false
 *     branch, no catch. This control changes what the agent may do
 *     without asking, so it also re-reads the brain rather than
 *     trusting the POST.
 *
 *   Security / Vault     — `catch { setItems([]) }` rendered "No stored
 *     keys yet." out of a request that never landed: an affirmative
 *     claim about the user's secrets.
 *
 *   MCP / Connect        — a refused connection was written to `msg`,
 *     which renders in the green "live" chip.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fireEvent, waitFor } from '@testing-library/react';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Settings from '../../pages/Settings';

afterEach(() => { vi.unstubAllGlobals(); });

/**
 * A fetch stub that can actually fail. The shared `renderV2` helper
 * always resolves 200, which is exactly the condition none of these
 * defects survive under.
 */
function installFetch(handler) {
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const res = (await handler(url, init)) || {};
    const status = res.status ?? 200;
    const body = res.body ?? {};
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: String(status),
      json: async () => body,
      text: async () => JSON.stringify(body),
      clone() { return this; },
      headers: new Map(),
    };
  }));
  vi.stubGlobal('WebSocket', class {
    constructor() { this.readyState = 1; this.send = vi.fn(); this.close = vi.fn(); this.addEventListener = vi.fn(); this.removeEventListener = vi.fn(); }
  });
  if (!window.matchMedia) {
    window.matchMedia = vi.fn().mockImplementation((q) => ({
      matches: false, media: q, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
  }
}

function renderSettings(section) {
  return render(
    <MemoryRouter initialEntries={[`/settings?section=${section}`]}>
      <Settings />
    </MemoryRouter>,
  );
}

describe('Settings / Push', () => {
  it('does not claim a push was sent when the brain sent none', async () => {
    installFetch(async (url) => {
      if (url.includes('/api/push/send')) {
        return {
          status: 200,
          body: {
            success: false,
            sent: 0,
            failed: 1,
            degraded: ['no push credentials configured (set FERAL_APNS_KEY_PATH for iOS)'],
          },
        };
      }
      return { status: 200, body: {} };
    });
    const { getByText, queryByTestId, findByTestId } = renderSettings('Push');
    fireEvent.click(await waitFor(() => getByText('Send test')));
    const err = await findByTestId('push-error');
    expect(err.textContent).toContain('Nothing was sent');
    expect(err.textContent).toContain('no push credentials configured');
    expect(queryByTestId('push-ok')).toBeNull();
  });

  it('reports a real send with the device count', async () => {
    installFetch(async (url) => {
      if (url.includes('/api/push/send')) {
        return { status: 200, body: { success: true, sent: 2, failed: 0, degraded: [] } };
      }
      return { status: 200, body: {} };
    });
    const { getByText, findByTestId } = renderSettings('Push');
    fireEvent.click(await waitFor(() => getByText('Send test')));
    expect((await findByTestId('push-ok')).textContent).toContain('Sent to 2 devices');
  });

  it('surfaces a refused registration instead of swallowing it', async () => {
    installFetch(async (url) => {
      if (url.includes('/api/push/register')) return { status: 503, body: { detail: 'push service down' } };
      return { status: 200, body: {} };
    });
    const { getByPlaceholderText, getByText, findByTestId } = renderSettings('Push');
    const input = await waitFor(() => getByPlaceholderText(/APNs \/ FCM token/i));
    fireEvent.change(input, { target: { value: 'tok' } });
    fireEvent.click(getByText('Register'));
    expect((await findByTestId('push-error')).textContent).toContain('push service down');
  });
});

describe('Settings / General', () => {
  it('says so when a feature toggle does not land', async () => {
    installFetch(async (url) => {
      if (url.includes('/api/config/update')) return { status: 500, body: { detail: 'settings.json is read-only' } };
      if (url.includes('/api/config')) {
        return { status: 200, body: { version: '1.0', features: { streaming: true } } };
      }
      return { status: 200, body: {} };
    });
    const { container, findByTestId } = renderSettings('General');
    const sw = await waitFor(() => {
      const el = container.querySelector('[role="switch"]');
      if (!el) throw new Error('no toggle');
      return el;
    });
    fireEvent.click(sw);
    const err = await findByTestId('general-error');
    expect(err.textContent).toContain('did not change');
    expect(err.textContent).toContain('settings.json is read-only');
  });
});

describe('Settings / Autonomy', () => {
  it('says so when the tier change is refused', async () => {
    installFetch(async (url, init) => {
      if (url.includes('/api/autonomy') && (init?.method || 'GET') === 'POST') {
        return { status: 403, body: { detail: 'autonomy is locked by policy' } };
      }
      if (url.includes('/api/autonomy')) return { status: 200, body: { mode: 'hybrid' } };
      return { status: 200, body: {} };
    });
    const { getAllByText, findByTestId } = renderSettings('Autonomy');
    fireEvent.click((await waitFor(() => getAllByText('Select')))[0]);
    expect((await findByTestId('autonomy-error')).textContent).toContain('autonomy is locked by policy');
  });

  it('reports the brain tier, not the tier that was asked for', async () => {
    let mode = 'hybrid';
    installFetch(async (url, init) => {
      if (url.includes('/api/autonomy') && (init?.method || 'GET') === 'POST') {
        // Accepted, but the brain clamps it.
        return { status: 200, body: { ok: true } };
      }
      if (url.includes('/api/autonomy')) return { status: 200, body: { mode } };
      return { status: 200, body: {} };
    });
    const { getAllByText, findByTestId } = renderSettings('Autonomy');
    fireEvent.click((await waitFor(() => getAllByText('Select')))[0]);
    const err = await findByTestId('autonomy-error');
    expect(err.textContent).toContain('reports tier "hybrid"');
    expect(mode).toBe('hybrid');
  });
});

describe('Settings / Security vault', () => {
  it('never says "No stored keys yet" about a vault it could not read', async () => {
    installFetch(async (url) => {
      if (url.includes('/api/security/vault')) return { status: 502, body: { detail: 'vault locked' } };
      return { status: 200, body: {} };
    });
    const { queryByText, findByTestId } = renderSettings('Security');
    expect((await findByTestId('vault-unknown')).textContent).toContain('could not be read');
    expect(queryByText('No stored keys yet.')).toBeNull();
  });

  it('still says the vault is empty when the brain says it is', async () => {
    installFetch(async (url) => {
      if (url.includes('/api/security/vault')) return { status: 200, body: { keys: {} } };
      return { status: 200, body: {} };
    });
    const { findByText } = renderSettings('Security');
    expect(await findByText('No stored keys yet.')).toBeTruthy();
  });

  it('surfaces a refused store instead of clearing the fields quietly', async () => {
    installFetch(async (url, init) => {
      if (url.includes('/api/security/vault/store')) return { status: 500, body: { detail: 'vault is read-only' } };
      if (url.includes('/api/security/vault')) return { status: 200, body: { keys: {} } };
      return { status: 200, body: {} };
    });
    const { getByPlaceholderText, getByText, container, findByTestId } = renderSettings('Security');
    const nameInput = await waitFor(() => getByPlaceholderText('OPENWEATHER_API_KEY'));
    fireEvent.change(nameInput, { target: { value: 'K' } });
    const pwd = container.querySelector('input[type="password"]');
    fireEvent.change(pwd, { target: { value: 'v' } });
    fireEvent.click(getByText('Store'));
    expect((await findByTestId('vault-error')).textContent).toContain('vault is read-only');
  });
});
