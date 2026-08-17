import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Marketplace from '../../pages/Marketplace';
import Apps from '../../pages/Apps';

/**
 * Installing a GenUI app used to POST straight to /api/apps/install,
 * which asked for nothing and silently installed the app's
 * `skill_dependencies` over the unsigned marketplace path. A skill runs
 * Python inside the brain, so that was an unattended code install behind
 * a page that says "Signed community registry".
 *
 * jsdom proves the wiring: no install request before confirm, the skills
 * are named with their permissions, the remediation for a skill FERAL
 * cannot install is on screen, and the token is echoed back. It cannot
 * prove the sheet is readable, which is why e2e/app_install_consent.spec.ts
 * drives the same flow in Chrome.
 */

const APP_ITEM = {
  id: 'trail-app',
  kind: 'app',
  name: 'Trail',
  version: '2.0.0',
  description: 'Plans hikes.',
  publisher: 'acme-labs',
};

const BASE_PREVIEW = {
  success: true,
  app: {
    app_id: 'trail-app',
    version: '2.0.0',
    author: 'acme-labs',
    description: 'Plans hikes.',
    brand: { name: 'Trail' },
    entry_surface_id: 'home',
    surfaces: [{ surface_id: 'home', kind: 'authored' }],
  },
  source: { origin: 'registry_id', value: 'trail-app' },
  signature: { verified: true, publisher: 'acme-labs', sha256: 'b'.repeat(64), reason: '' },
  permission_details: [
    {
      id: 'network:api.trail.example',
      label: 'Contact api.trail.example',
      description: "This app's surfaces may send and receive data from api.trail.example.",
      known: true,
    },
  ],
  skill_dependencies: {
    declared: ['trail_notes', 'map_tiles'],
    already_installed: [
      {
        skill_id: 'map_tiles',
        name: 'Map Tiles',
        version: '3.0.0',
        permissions: ['network'],
        permission_details: [
          { id: 'network', label: 'Internet access', description: 'Contact servers.' },
        ],
      },
    ],
    to_install: [
      {
        skill_id: 'trail_notes',
        name: 'Trail Notes',
        version: '1.4.0',
        publisher: 'acme-labs',
        permissions: ['filesystem'],
        permission_details: [
          {
            id: 'filesystem',
            label: 'Your files',
            description: 'Read and write files on this computer.',
          },
        ],
        signature: { verified: true, sha256: 'c'.repeat(64) },
      },
    ],
    unavailable: [],
  },
  degraded: false,
  install_token: 'app-token-abc',
};

const DEGRADED_PREVIEW = {
  ...BASE_PREVIEW,
  degraded: true,
  skill_dependencies: {
    declared: ['ghost_skill'],
    already_installed: [],
    to_install: [],
    unavailable: [
      {
        skill_id: 'ghost_skill',
        reason: "'ghost_skill' is not published in the registry at https://registry.feral.sh",
        remediation: {
          code: 'not_published',
          message: "'ghost_skill' is not published in the FERAL registry, so there is nothing for FERAL to verify or download.",
          action: 'Ask the publisher of this app to publish the skill to registry.feral.sh.',
          command: '',
        },
        impact: [
          {
            surface_id: 'home',
            action_id: 'sync_0',
            description: 'Sync your notes',
            target: 'ghost_skill/sync',
          },
        ],
      },
    ],
  },
};

function responder(preview) {
  return (url) => {
    if (url.includes('/api/apps/preview')) return preview;
    if (url.includes('/api/apps/install')) return { success: true, app: { app_id: 'trail-app' } };
    if (url.includes('/api/marketplace/catalog')) return { items: [APP_ITEM] };
    if (url.includes('/api/marketplace/installed')) return { skills: [] };
    return { items: [] };
  };
}

function postsTo(path) {
  return (global.fetch?.mock?.calls || []).filter(([u, init]) => (
    String(u).includes(path) && (init?.method || 'GET') === 'POST'
  ));
}

async function openAppSheet(preview) {
  renderV2(<Marketplace />, { fetch: responder(preview) });
  // The catalog defaults to the skill tab; switch to apps.
  fireEvent.click(await screen.findByText('app'));
  const installBtn = await screen.findByRole('button', { name: /^Install/i });
  fireEvent.click(installBtn);
  await waitFor(() => expect(postsTo('/api/apps/preview').length).toBe(1));
}

describe('App install consent', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('previews before installing, and installs nothing until confirm', async () => {
    await openAppSheet(BASE_PREVIEW);

    expect(postsTo('/api/apps/install').length).toBe(0);
    expect(await screen.findByTestId('v2-app-install-signature')).toHaveTextContent(/verified/i);
  });

  it('names the skills it will install, with what each one reaches', async () => {
    await openAppSheet(BASE_PREVIEW);

    const newSkills = await screen.findByTestId('v2-app-install-new-skills');
    expect(newSkills).toHaveTextContent('Trail Notes');
    expect(newSkills).toHaveTextContent('trail_notes');
    // The permission of the *skill*, not of the app: this is the part
    // that runs code and it was previously invisible.
    expect(newSkills).toHaveTextContent(/Read and write files on this computer\./);
  });

  it('separates a skill already installed from one newly installed', async () => {
    await openAppSheet(BASE_PREVIEW);

    const existing = await screen.findByTestId('v2-app-install-existing-skills');
    expect(existing).toHaveTextContent('Map Tiles');
    expect(screen.getByTestId('v2-app-install-new-skills')).not.toHaveTextContent('Map Tiles');
    expect(screen.getByText(/Nothing new is installed for these/i)).toBeInTheDocument();
  });

  it("shows the app's own reach separately from its skills", async () => {
    await openAppSheet(BASE_PREVIEW);

    expect(await screen.findByText(/What the app itself can reach/i)).toBeInTheDocument();
    expect(
      screen.getByText(/may send and receive data from api\.trail\.example/i),
    ).toBeInTheDocument();
  });

  it('installs with the token the preview minted', async () => {
    await openAppSheet(BASE_PREVIEW);

    fireEvent.click(screen.getByTestId('v2-app-install-confirm'));

    await waitFor(() => expect(postsTo('/api/apps/install').length).toBe(1));
    const [, init] = postsTo('/api/apps/install')[0];
    expect(JSON.parse(init.body)).toEqual({ install_token: 'app-token-abc' });
  });

  it('cancelling installs nothing', async () => {
    await openAppSheet(BASE_PREVIEW);

    fireEvent.click(screen.getByTestId('v2-app-install-cancel'));

    await waitFor(() =>
      expect(screen.queryByTestId('v2-app-install-confirm')).not.toBeInTheDocument());
    expect(postsTo('/api/apps/install').length).toBe(0);
  });

  it('a skill FERAL cannot install is named with the reason, the cost and the fix', async () => {
    await openAppSheet(DEGRADED_PREVIEW);

    const box = await screen.findByTestId('v2-app-install-unavailable-skills');
    // The reason, from the brain, not a generic string.
    expect(box).toHaveTextContent(/not published in the FERAL registry/i);
    // What the app cannot do without it.
    expect(box).toHaveTextContent(/Sync your notes/);
    // How to get it.
    expect(box).toHaveTextContent(/Ask the publisher of this app to publish the skill/i);
    // And the decision is still available, framed as what it is.
    expect(screen.getByTestId('v2-app-install-confirm')).toHaveTextContent(/Install without 1 skill/);
    expect(screen.getByTestId('v2-app-install-confirm')).toBeEnabled();
  });

  it('an unverified app cannot be installed and lists nothing', async () => {
    await openAppSheet({
      success: false,
      error: 'registry signature verification failed',
      signature: { verified: false, reason: 'sha256 mismatch', sha256: '', publisher: '' },
    });

    const sig = await screen.findByTestId('v2-app-install-signature');
    expect(sig).toHaveTextContent(/Not verified/i);
    expect(screen.queryByTestId('v2-app-install-new-skills')).not.toBeInTheDocument();
    expect(screen.getByTestId('v2-app-install-confirm')).toBeDisabled();
  });
});

describe('Apps page keeps reporting a missing dependency', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows what is missing, what it costs and how to fix it', async () => {
    renderV2(<Apps />, {
      fetch: (url) => {
        if (url.includes('/api/apps')) {
          return {
            count: 1,
            apps: [{
              app_id: 'trail-app',
              version: '2.0.0',
              author: 'acme-labs',
              description: 'Plans hikes.',
              brand: { name: 'Trail' },
              skill_dependencies: ['ghost_skill'],
              missing_skill_dependencies: [{
                skill_id: 'ghost_skill',
                impact: [{ surface_id: 'home', action_id: 'sync_0', description: 'Sync your notes' }],
                remediation: {
                  code: 'not_installed',
                  message: "'ghost_skill' is not installed on this brain.",
                  action: 'Install it on its own from the registry.',
                  command: 'feral install ghost_skill',
                },
              }],
            }],
          };
        }
        return {};
      },
    });

    const box = await screen.findByTestId('v2-apps-missing-trail-app');
    expect(box).toHaveTextContent(/Missing skill: ghost_skill/);
    expect(box).toHaveTextContent(/Sync your notes will not work/);
    expect(box).toHaveTextContent(/Install it on its own from the registry/);
    expect(box).toHaveTextContent('feral install ghost_skill');
  });
});
