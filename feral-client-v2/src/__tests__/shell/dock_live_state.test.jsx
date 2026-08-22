/**
 * Dock tiles report the state of what they lead to.
 *
 * The approved design: "Tiles report live state. Jobs breathes with a
 * fill and a ring while something runs." The point is that the machine
 * is legible from the dock without opening anything.
 *
 * Two things this pins beyond the rendering:
 *
 *  - Only tiles with something to report get a state. A dock where
 *    every tile pulses is a dock that says nothing.
 *  - The animation is suppressed under prefers-reduced-motion while the
 *    state itself still reads. Motion is the decoration; the fill and
 *    the ring are the information.
 */
import fs from 'node:fs';
import path from 'node:path';
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

vi.mock('../../hooks/useMachineVitals', () => ({
  useMachineVitals: vi.fn(),
}));
vi.mock('../../shell/PaletteContext', () => ({
  useCommandPalette: () => ({ open: false, togglePalette: () => {} }),
}));

import { useMachineVitals } from '../../hooks/useMachineVitals';
import Dock from '../../shell/Dock';

const UI_CSS = path.resolve(__dirname, '../../styles/ui.css');
const css = fs.readFileSync(UI_CSS, 'utf8');

const draw = () => render(<MemoryRouter><Dock /></MemoryRouter>);
const states = (c) =>
  Object.fromEntries([...c.querySelectorAll('.v2-dock-btn')]
    .map((el) => [el.getAttribute('title')?.split(' (')[0], el.getAttribute('data-state') || '']));

beforeEach(() => {
  useMachineVitals.mockReturnValue({
    running: 0, shells: 0, needs: 0, devices: 0, tokens: 0, cost: 0, autonomy: '', reachable: true,
  });
});

describe('a quiet machine shows a quiet dock', () => {
  it('gives no tile a state when nothing is running or waiting', () => {
    const { container } = draw();
    expect(Object.values(states(container)).every((s) => s === '')).toBe(true);
  });

  it('renders no count badge at zero', () => {
    const { container } = draw();
    expect(container.querySelectorAll('.v2-dock-count')).toHaveLength(0);
  });
});

describe('state appears only on the tile it belongs to', () => {
  it('Jobs breathes while something runs, and nothing else does', () => {
    useMachineVitals.mockReturnValue({ running: 3, shells: 1, needs: 0, devices: 0, tokens: 0, cost: 0, autonomy: '', reachable: true });
    const { container } = draw();
    const s = states(container);
    expect(s.Jobs).toBe('busy');
    expect(Object.entries(s).filter(([, v]) => v).map(([k]) => k)).toEqual(['Jobs']);
  });

  it('Needs you fills while a call is blocked, and nothing else does', () => {
    useMachineVitals.mockReturnValue({ running: 0, shells: 0, needs: 2, devices: 0, tokens: 0, cost: 0, autonomy: '', reachable: true });
    const { container } = draw();
    const s = states(container);
    expect(s['Needs you']).toBe('needs');
    expect(Object.entries(s).filter(([, v]) => v).map(([k]) => k)).toEqual(['Needs you']);
  });

  it('both can be live at once', () => {
    useMachineVitals.mockReturnValue({ running: 1, shells: 0, needs: 4, devices: 0, tokens: 0, cost: 0, autonomy: '', reachable: true });
    const { container } = draw();
    const s = states(container);
    expect(s.Jobs).toBe('busy');
    expect(s['Needs you']).toBe('needs');
  });

  it('puts the count in the title so it is not colour-only', () => {
    useMachineVitals.mockReturnValue({ running: 0, shells: 0, needs: 7, devices: 0, tokens: 0, cost: 0, autonomy: '', reachable: true });
    const { container } = draw();
    const el = [...container.querySelectorAll('.v2-dock-btn')]
      .find((e) => (e.getAttribute('title') || '').startsWith('Needs you'));
    expect(el.getAttribute('title')).toBe('Needs you (7)');
    expect(el.querySelector('.v2-dock-count').textContent).toBe('7');
  });
});

describe('the stylesheet honours reduced motion', () => {
  it('suppresses the animation but keeps the state visible', () => {
    expect(css).toMatch(/prefers-reduced-motion: reduce/);
    const block = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
    expect(block).toMatch(/v2-dock-fill[\s\S]{0,120}animation:\s*none/);
  });

  it('defines the keyframes the design specifies', () => {
    // breathe 52% -> 78%, and a rotating ring.
    expect(css).toMatch(/@keyframes v2-dock-breathe\s*\{[^}]*48%/);
    expect(css).toMatch(/@keyframes v2-dock-spin/);
  });
});
