/**
 * CommandPalette behaviour.
 *
 * The predecessor, HubLauncher, was a fifteen-tile grid with a search
 * box that could not find Chat, Devices, Home, Flows, Apps, Canvas or
 * Settings, because those were Dock tiles and the popup only knew about
 * its own fifteen. Seven of the eight things a person uses most were
 * invisible to the only search field in the app.
 *
 * These tests drive the palette itself. The index-versus-router mirror
 * lives in navigation.index.test.jsx.
 */
import React from 'react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import CommandPalette from '../../shell/CommandPalette';
import { DOCK_ITEMS, DOCK_PATHS } from '../../shell/navigation';

afterEach(() => {
  vi.unstubAllGlobals();
});

const rows = (container) => [...container.querySelectorAll('.v2-cmdk-row')];
const labels = (container) => rows(container)
  .map((el) => el.querySelector('.v2-cmdk-row-label')?.textContent);

function type(container, value) {
  const input = container.querySelector('.v2-cmdk-search');
  fireEvent.change(input, { target: { value } });
  return input;
}

describe('CommandPalette rendering', () => {
  it('renders nothing when closed', () => {
    const { container } = renderV2(<CommandPalette open={false} onClose={() => {}} />);
    expect(container.querySelector('.v2-cmdk')).toBeNull();
  });

  it('opens as a labelled modal dialog', () => {
    renderV2(<CommandPalette open onClose={() => {}} />);
    const dialog = screen.getByRole('dialog', { name: /command palette/i });
    expect(dialog.getAttribute('aria-modal')).toBe('true');
  });
});

describe('the palette can find the Dock primaries', () => {
  // One test per Dock destination. The old Hub failed seven of these.
  //
  // The query comes from DOCK_ITEMS itself rather than a hardcoded
  // path -> label map. That map went stale the moment the Dock's
  // composition changed to the approved design's eight, and every case
  // then asserted on `undefined`. Deriving it is the same rule the
  // navigation index follows: restate nothing you can read.
  const labelFor = Object.fromEntries(DOCK_ITEMS.map((d) => [d.to, d.label]));

  for (const to of DOCK_PATHS) {
    it(`matches "${labelFor[to]}" for ${to}`, () => {
      const { container, unmount } = renderV2(<CommandPalette open onClose={() => {}} />);
      type(container, labelFor[to]);
      expect(labels(container)).toContain(labelFor[to]);
      unmount();
    });
  }
});

describe('the palette offers Settings sections as deep links', () => {
  it('matches a section by name', () => {
    const { container } = renderV2(<CommandPalette open onClose={() => {}} />);
    type(container, 'Providers');
    expect(labels(container)).toContain('Settings: Providers');
  });
});

describe('the palette offers verbs, not just destinations', () => {
  it('lists a Do group before Go', () => {
    const { container } = renderV2(<CommandPalette open onClose={() => {}} />);
    const groups = [...container.querySelectorAll('.v2-cmdk-group-label')]
      .map((el) => el.textContent);
    expect(groups[0]).toBe('Do');
    expect(groups).toContain('Go');
  });

  it('finds the voice verb by name', () => {
    const { container } = renderV2(<CommandPalette open onClose={() => {}} />);
    type(container, 'voice');
    expect(labels(container).some((l) => /voice session/i.test(l))).toBe(true);
  });

  it('finds the ambient layer, which was only ever bound to an undocumented chord', () => {
    const { container } = renderV2(<CommandPalette open onClose={() => {}} />);
    type(container, 'ambient');
    expect(labels(container)).toContain('Reveal the ambient layer');
  });

  it('dispatches the ambient expand event when that verb runs', () => {
    const seen = [];
    const handler = () => seen.push(1);
    window.addEventListener('v2:ambient-expand', handler);
    const { container } = renderV2(<CommandPalette open onClose={() => {}} />);
    type(container, 'ambient');
    fireEvent.click(rows(container)[0]);
    window.removeEventListener('v2:ambient-expand', handler);
    expect(seen).toHaveLength(1);
  });
});

describe('the Ask row', () => {
  it('appears only once something has been typed', () => {
    const { container } = renderV2(<CommandPalette open onClose={() => {}} />);
    const groupsBefore = [...container.querySelectorAll('.v2-cmdk-group-label')]
      .map((el) => el.textContent);
    expect(groupsBefore).not.toContain('Ask');

    type(container, 'why is the brain offline');
    const groupsAfter = [...container.querySelectorAll('.v2-cmdk-group-label')]
      .map((el) => el.textContent);
    expect(groupsAfter).toContain('Ask');
    expect(labels(container)).toContain('Ask FERAL: why is the brain offline');
  });

  it('survives a query that matches no page at all', () => {
    // The old popup rendered a bare "No matches." here. A palette that
    // can hand any string to the brain always has one thing to offer.
    const { container } = renderV2(<CommandPalette open onClose={() => {}} />);
    type(container, 'zzzzz not a page');
    expect(container.querySelector('.v2-cmdk-empty')).toBeNull();
    expect(labels(container)).toEqual(['Ask FERAL: zzzzz not a page']);
  });
});

describe('keyboard control', () => {
  it('moves the cursor with the arrow keys and wraps', () => {
    const { container } = renderV2(<CommandPalette open onClose={() => {}} />);
    type(container, 'memory');
    const total = rows(container).length;
    expect(total).toBeGreaterThan(1);

    expect(rows(container)[0].className).toContain('is-cursor');
    fireEvent.keyDown(window, { key: 'ArrowDown' });
    expect(rows(container)[1].className).toContain('is-cursor');
    fireEvent.keyDown(window, { key: 'ArrowUp' });
    fireEvent.keyDown(window, { key: 'ArrowUp' });
    expect(rows(container)[total - 1].className).toContain('is-cursor');
  });

  it('closes on Escape', () => {
    const onClose = vi.fn();
    renderV2(<CommandPalette open onClose={onClose} />);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });

  it('closes when the backdrop is clicked but not when the dialog is', () => {
    const onClose = vi.fn();
    const { container } = renderV2(<CommandPalette open onClose={onClose} />);
    fireEvent.click(container.querySelector('.v2-cmdk'));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(container.querySelector('.v2-cmdk-backdrop'));
    expect(onClose).toHaveBeenCalled();
  });
});

describe('the device CTA carried over from the Hub', () => {
  it('shows the pair CTA when the brain really reports zero paired', async () => {
    const { container } = renderV2(<CommandPalette open onClose={() => {}} />, {
      fetch: (url) => (url.includes('/api/dashboard')
        ? { paired_count: 0, online_count: 0, device_count: 0 }
        : {}),
    });
    await waitFor(() => {
      expect(container.querySelector('.v2-cmdk-cta-title')?.textContent)
        .toMatch(/Pair a device/);
    });
  });

  it('hides the CTA once the user starts typing', async () => {
    const { container } = renderV2(<CommandPalette open onClose={() => {}} />, {
      fetch: (url) => (url.includes('/api/dashboard')
        ? { paired_count: 0, online_count: 0, device_count: 0 }
        : {}),
    });
    await waitFor(() => {
      expect(container.querySelector('.v2-cmdk-cta')).not.toBeNull();
    });
    type(container, 'devices');
    expect(container.querySelector('.v2-cmdk-cta')).toBeNull();
  });
});
