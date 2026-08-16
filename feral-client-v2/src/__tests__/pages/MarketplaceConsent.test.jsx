import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Marketplace from '../../pages/Marketplace';

/**
 * WORK-ORDER P0.2 / P0.3. Installing a third-party skill is a consent
 * decision: the user must be shown what the package can reach and
 * whether it is signed, and nothing may be installed until they say so.
 *
 * jsdom can prove the wiring (no request before confirm, the permission
 * copy is in the tree, the token is echoed back). It cannot prove the
 * dialog is readable, which is why this page is also driven in Chrome.
 */

const CATALOG_ITEM = {
  id: 'consent_demo',
  kind: 'skill',
  name: 'Consent Demo',
  version: '1.2.3',
  description: 'A demo skill.',
  publisher: 'test-publisher',
  downloads: 12,
};

const PREVIEW = {
  success: true,
  kind: 'skill',
  id: 'consent_demo',
  name: 'Consent Demo',
  version: '1.2.3',
  publisher: 'test-publisher',
  permissions: ['filesystem', 'network'],
  permission_details: [
    { id: 'filesystem', label: 'Files', description: 'Read and write files on this computer.' },
    { id: 'network', label: 'Internet access', description: 'Send and receive data over the network.' },
  ],
  signature: {
    verified: true,
    publisher: 'test-publisher',
    sha256: 'a'.repeat(64),
    reason: '',
  },
  install_token: 'preview-token-abc',
};

function responder(url) {
  if (url.includes('/api/marketplace/preview')) return PREVIEW;
  if (url.includes('/api/marketplace/install')) return { success: true, skill_id: 'consent_demo' };
  if (url.includes('/api/marketplace/catalog')) return { items: [CATALOG_ITEM] };
  if (url.includes('/api/marketplace/installed')) return { skills: [] };
  return { items: [] };
}

function postsTo(path) {
  return (global.fetch?.mock?.calls || []).filter(([u, init]) => (
    String(u).includes(path) && (init?.method || 'GET') === 'POST'
  ));
}

describe('Marketplace install consent', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows permissions and signature status before installing, and installs only on confirm', async () => {
    renderV2(<Marketplace />, { fetch: responder });

    const installBtn = await screen.findByRole('button', { name: /^Install/i });
    fireEvent.click(installBtn);

    // The preview request happens; the install request must not.
    await waitFor(() => expect(postsTo('/api/marketplace/preview').length).toBe(1));
    expect(postsTo('/api/marketplace/install').length).toBe(0);

    // The user is shown what the skill can reach.
    expect(await screen.findByText(/Read and write files on this computer\./)).toBeInTheDocument();
    expect(screen.getByText(/Send and receive data over the network\./)).toBeInTheDocument();
    // ...and whether the package is verified.
    expect(screen.getByTestId('v2-install-signature')).toHaveTextContent(/verified/i);

    // Still nothing installed while the dialog sits open.
    expect(postsTo('/api/marketplace/install').length).toBe(0);

    fireEvent.click(screen.getByTestId('v2-install-confirm'));

    await waitFor(() => expect(postsTo('/api/marketplace/install').length).toBe(1));
    const [, init] = postsTo('/api/marketplace/install')[0];
    expect(JSON.parse(init.body)).toMatchObject({
      id: 'consent_demo',
      kind: 'skill',
      install_token: 'preview-token-abc',
    });
  });

  it('cancelling the dialog installs nothing', async () => {
    renderV2(<Marketplace />, { fetch: responder });

    fireEvent.click(await screen.findByRole('button', { name: /^Install/i }));
    await screen.findByTestId('v2-install-confirm');

    fireEvent.click(screen.getByTestId('v2-install-cancel'));

    await waitFor(() => expect(screen.queryByTestId('v2-install-confirm')).toBeNull());
    expect(postsTo('/api/marketplace/install').length).toBe(0);
  });

  it('warns instead of reassuring when the package is not verified', async () => {
    const unverified = (url) => {
      if (url.includes('/api/marketplace/preview')) {
        return {
          ...PREVIEW,
          signature: { verified: false, publisher: '', sha256: '', reason: 'no signature in registry response' },
        };
      }
      return responder(url);
    };
    renderV2(<Marketplace />, { fetch: unverified });

    fireEvent.click(await screen.findByRole('button', { name: /^Install/i }));

    const sig = await screen.findByTestId('v2-install-signature');
    expect(sig).toHaveTextContent(/not verified/i);
    expect(screen.getByTestId('v2-install-confirm')).toBeDisabled();
    expect(postsTo('/api/marketplace/install').length).toBe(0);
  });

  it('lists permissions on the catalog card when the catalog carries them', async () => {
    const withPerms = (url) => {
      if (url.includes('/api/marketplace/catalog')) {
        return {
          items: [{
            ...CATALOG_ITEM,
            permissions: ['filesystem'],
            permission_details: [
              { id: 'filesystem', label: 'Files', description: 'Read and write files on this computer.' },
            ],
          }],
        };
      }
      return responder(url);
    };
    renderV2(<Marketplace />, { fetch: withPerms });

    expect(await screen.findByTestId('v2-card-permissions')).toHaveTextContent(/Files/);
  });
});
