/**
 * `role="button" tabIndex={0}` elements must respond to Enter and Space.
 *
 * Six elements declared themselves buttons and were reachable by Tab, but
 * had no key handler: the device detail modal (three card variants plus
 * the paired-device header), the flow detail pane, and every mind-map
 * node. Reachable and inert is worse than not reachable, because the
 * focus indicator tells the user there is something there.
 *
 * These tests fail against `git show HEAD:` copies of Devices.jsx,
 * Flows.jsx and ConsciousnessMindMap.jsx.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { waitFor, fireEvent } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Devices from '../../pages/Devices';
import Flows from '../../pages/Flows';
import ConsciousnessMindMap from '../../components/ConsciousnessMindMap';

afterEach(() => {
  vi.unstubAllGlobals();
});

const ENTER = { key: 'Enter', code: 'Enter' };
const SPACE = { key: ' ', code: 'Space' };

function devicesFetch() {
  return (url) => {
    if (url.includes('/api/devices/connected')) {
      return {
        devices: [{
          node_id: 'feral-iphone-abc',
          name: 'Test iPhone',
          type: 'phone',
          capabilities: ['host_phone'],
          status: 'connected',
        }],
      };
    }
    if (url.includes('/api/devices/paired')) return { devices: [] };
    if (url.includes('/api/hardware/mesh')) return { nodes: [] };
    return null;
  };
}

function flowsFetch() {
  return (url) => {
    if (url.includes('/api/taskflows')) {
      return {
        flows: [{
          id: 'flow-1',
          title: 'Nightly summary',
          status: 'paused',
          current_step: 1,
          steps: [{ step_type: 'llm', status: 'done' }],
        }],
      };
    }
    return null;
  };
}

async function findDeviceCard(container) {
  return waitFor(() => {
    const el = container.querySelector('.v2-device-card[role="button"]');
    if (!el) throw new Error('device card not rendered');
    return el;
  });
}

describe('Devices: keyboard activation of the device card', () => {
  it('opens the detail dialog on Enter', async () => {
    const { container } = renderV2(<Devices />, { fetch: devicesFetch() });
    const card = await findDeviceCard(container);
    expect(document.querySelector('[role="dialog"]')).toBeNull();

    card.focus();
    fireEvent.keyDown(card, ENTER);

    await waitFor(() => {
      expect(document.querySelector('[role="dialog"]')).toBeTruthy();
    });
  });

  it('opens the detail dialog on Space', async () => {
    const { container } = renderV2(<Devices />, { fetch: devicesFetch() });
    const card = await findDeviceCard(container);
    expect(document.querySelector('[role="dialog"]')).toBeNull();

    card.focus();
    fireEvent.keyDown(card, SPACE);

    await waitFor(() => {
      expect(document.querySelector('[role="dialog"]')).toBeTruthy();
    });
  });

  it('ignores keys that do not activate a native button', async () => {
    const { container } = renderV2(<Devices />, { fetch: devicesFetch() });
    const card = await findDeviceCard(container);
    fireEvent.keyDown(card, { key: 'a', code: 'KeyA' });
    fireEvent.keyDown(card, { key: 'ArrowDown', code: 'ArrowDown' });
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it('does not open the card when a nested control is activated', async () => {
    const { container } = renderV2(<Devices />, { fetch: devicesFetch() });
    const card = await findDeviceCard(container);
    const nested = card.querySelector('button');
    if (!nested) return; // this card variant has no nested control
    fireEvent.keyDown(nested, ENTER);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it('carries an accessible name so the card is not an anonymous button', async () => {
    const { container } = renderV2(<Devices />, { fetch: devicesFetch() });
    const card = await findDeviceCard(container);
    expect(card.getAttribute('aria-label')).toBeTruthy();
  });
});

describe('Flows: keyboard activation of the flow title', () => {
  async function findTitle(container) {
    return waitFor(() => {
      const el = container.querySelector('.v2-flow-card-title[role="button"]');
      if (!el) throw new Error('flow title not rendered');
      return el;
    });
  }

  it('opens the flow detail on Enter', async () => {
    const { container } = renderV2(<Flows />, { fetch: flowsFetch() });
    const title = await findTitle(container);
    expect(document.querySelector('[role="dialog"]')).toBeNull();

    fireEvent.keyDown(title, ENTER);

    await waitFor(() => {
      expect(document.querySelector('[role="dialog"]')).toBeTruthy();
    });
  });

  it('opens the flow detail on Space', async () => {
    const { container } = renderV2(<Flows />, { fetch: flowsFetch() });
    const title = await findTitle(container);
    expect(document.querySelector('[role="dialog"]')).toBeNull();

    fireEvent.keyDown(title, SPACE);

    await waitFor(() => {
      expect(document.querySelector('[role="dialog"]')).toBeTruthy();
    });
  });
});

describe('ConsciousnessMindMap: keyboard activation of a node', () => {
  function mindmapFetch() {
    return (url) => {
      if (url.includes('/api/consciousness/state')) {
        return {
          entities: [{
            id: 'ent-1',
            kind: 'flow',
            status: 'active',
            summary: 'Nightly summary flow',
          }],
        };
      }
      return null;
    };
  }

  async function findNode(container) {
    return waitFor(() => {
      const el = container.querySelector('g[role="button"]');
      if (!el) throw new Error('mind-map node not rendered');
      return el;
    });
  }

  it('has a key handler that Enter and Space reach', async () => {
    const { container } = renderV2(<ConsciousnessMindMap />, { fetch: mindmapFetch() });
    const node = await findNode(container);

    // The node navigates on activation, and jsdom + MemoryRouter give no
    // observable side effect on this container. Assert on the contract
    // instead: the handler must exist and must consume Enter/Space, which
    // it signals with preventDefault. Before the fix there was no handler
    // at all, so nothing was ever prevented.
    const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    node.dispatchEvent(enter);
    expect(enter.defaultPrevented, 'Enter was not handled').toBe(true);

    const space = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
    node.dispatchEvent(space);
    expect(space.defaultPrevented, 'Space was not handled').toBe(true);

    const other = new KeyboardEvent('keydown', { key: 'x', bubbles: true, cancelable: true });
    node.dispatchEvent(other);
    expect(other.defaultPrevented).toBe(false);
  });

  it('names each node instead of announcing an anonymous button', async () => {
    const { container } = renderV2(<ConsciousnessMindMap />, { fetch: mindmapFetch() });
    const node = await findNode(container);
    expect(node.getAttribute('aria-label')).toBeTruthy();
  });

  it('drops the SMIL pulse under prefers-reduced-motion', async () => {
    // CSS cannot switch off an SVG <animate>, so the only correct fix is
    // not to render it. Stub the media query before the component mounts.
    vi.stubGlobal('matchMedia', (q) => ({
      matches: q.includes('prefers-reduced-motion'),
      media: q,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }));
    window.matchMedia = globalThis.matchMedia;

    const { container } = renderV2(<ConsciousnessMindMap />, { fetch: mindmapFetch() });
    await findNode(container);
    expect(container.querySelectorAll('animate').length).toBe(0);
  });
});
