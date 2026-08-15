/**
 * Navigation reachability.
 *
 * Two things used to be reachable from exactly one place each: two
 * buttons in the header of pages/GlassBrain.jsx. One of them is
 * /oversight, which holds the kill switch. Neither was in the Dock nor
 * among HubLauncher's items, and neither was in `Dock.HUB_ROUTES`, so
 * arriving at either one left every button in the Dock unlit and the
 * operator with no indication of where they were.
 *
 * /ambient was the other half of the same problem from the opposite
 * direction: a route with no link pointing at it, mounting a second
 * copy of Home.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { renderV2 } from '../_helpers/renderV2';
import Dock from '../../shell/Dock';

afterEach(() => {
  vi.unstubAllGlobals();
});

function dockButton(container, label) {
  return [...container.querySelectorAll('.v2-dock-btn')]
    .find((el) => el.querySelector('.v2-dock-label')?.textContent === label);
}

describe('Dock reachability', () => {
  it('puts the kill switch in primary navigation', () => {
    const { container } = renderV2(<Dock />);
    const oversight = dockButton(container, 'Oversight');
    expect(oversight).toBeTruthy();
    expect(oversight.getAttribute('href')).toBe('/oversight');
  });

  it('lights the Oversight item, not the Hub, when the operator is on /oversight', () => {
    const { container } = renderV2(<Dock />, { route: '/oversight' });
    expect(dockButton(container, 'Oversight').className).toContain('is-active');
    expect(dockButton(container, 'Hub').className).not.toContain('is-active');
  });

  it('lights the Hub button on /memory/context', () => {
    // The Hub is where /memory/context is reachable from, and the
    // membership check is exact-pathname, so the nested route needed
    // its own entry. Without it the Dock rendered fully unlit.
    const { container } = renderV2(<Dock />, { route: '/memory/context' });
    expect(dockButton(container, 'Hub').className).toContain('is-active');
  });

  it('still lights the Hub button on a plain hub route', () => {
    const { container } = renderV2(<Dock />, { route: '/glass-brain' });
    expect(dockButton(container, 'Hub').className).toContain('is-active');
  });

  it('leaves the Hub unlit on a primary route', () => {
    const { container } = renderV2(<Dock />, { route: '/devices' });
    expect(dockButton(container, 'Hub').className).not.toContain('is-active');
    expect(dockButton(container, 'Devices').className).toContain('is-active');
  });
});
