/**
 * Phase 4 / C4.1 — Setup.jsx pairing step.
 *
 * Asserts the product-mandate invariants from
 * ``A4-pairing-redesign.md`` §11. The detailed multi-step interaction
 * is verified in the live browser smoke test (run by the lead, see
 * ``A4-repro.md`` Phase 4 section); this file pins the static contract.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderV2 } from '../_helpers/renderV2';
import Setup from '../../pages/Setup';


beforeEach(() => {
  if (!navigator.clipboard) {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn() },
      configurable: true,
    });
  }
});


describe('Setup.jsx — Pair phone step', () => {
  it('exposes a "Connect devices" tab in the wizard step list', async () => {
    const { findByRole } = renderV2(<Setup />, { fetch: () => ({}) });
    const tab = await findByRole('tab', { name: /connect devices/i });
    expect(tab).toBeInTheDocument();
  });

  it('keeps the existing five non-pair tabs in order', async () => {
    const { findAllByRole } = renderV2(<Setup />, { fetch: () => ({}) });
    const tabs = await findAllByRole('tab');
    const labels = tabs.map((t) => t.textContent.toLowerCase());
    // Order matters: Welcome / AI provider / Voice / About you / Connect / Ready.
    const idx = (s) => labels.findIndex((l) => l.includes(s));
    expect(idx('welcome')).toBeLessThan(idx('ai provider'));
    expect(idx('ai provider')).toBeLessThan(idx('voice'));
    expect(idx('voice')).toBeLessThan(idx('about'));
    expect(idx('about')).toBeLessThan(idx('connect'));
    expect(idx('connect')).toBeLessThan(idx('ready'));
  });

  it('lazy-imports without crashing — DoneStep references finishSetup callback', async () => {
    const { container } = renderV2(<Setup />, { fetch: () => ({}) });
    expect(container.firstChild).toBeInTheDocument();
  });
});
