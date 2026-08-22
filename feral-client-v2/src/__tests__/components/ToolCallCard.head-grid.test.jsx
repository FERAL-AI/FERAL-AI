/**
 * The tool card's head is a grid, and a grid only lines a stack of calls
 * up if the number of cells matches the number of tracks.
 *
 * It did not. The stylesheet declared three tracks, to the mockup's
 * `19px minmax(0,1fr) auto`, while the component put SIX children in the
 * head: chevron, glyph, name, argument summary, status word, duration.
 * Grid auto-placement wrapped them onto two implicit rows, so every tool
 * NAME landed in the third track and rendered hard right, under the
 * duration, while the argument line was squeezed into the first track
 * and clipped to two characters and an ellipsis ("re…", "ap…", "in…").
 * Screenshotted against a live brain at 1440px before the fix.
 *
 * jsdom computes no grid layout, so asserting on the rendered geometry
 * is not available here. What IS checkable, and what actually broke, is
 * the agreement between the two files: count the tracks the stylesheet
 * declares and the cells the component renders.
 */
import fs from 'node:fs';
import path from 'node:path';
import React from 'react';
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import ToolCallCard from '../../components/ToolCallCard';

const CSS = fs.readFileSync(
  path.resolve(__dirname, '../../styles/markdown.css'),
  'utf8',
);

/** Split a grid-template-columns value into tracks, respecting minmax(). */
function countTracks(value) {
  let depth = 0;
  let current = '';
  const tracks = [];
  for (const ch of value) {
    if (ch === '(') depth += 1;
    if (ch === ')') depth -= 1;
    if (/\s/.test(ch) && depth === 0) {
      if (current) tracks.push(current);
      current = '';
    } else {
      current += ch;
    }
  }
  if (current) tracks.push(current);
  return tracks.length;
}

/** The winning declaration is the last one in the cascade. */
function declaredTracks() {
  const blocks = [...CSS.matchAll(
    /\.v2-tool-card__head\s*\{([^}]*)\}/g,
  )];
  const withGrid = blocks
    .map((m) => /grid-template-columns:\s*([^;]+);/.exec(m[1]))
    .filter(Boolean);
  expect(withGrid.length).toBeGreaterThan(0);
  return countTracks(withGrid[withGrid.length - 1][1].trim());
}

const TRACE = {
  key: 'c1',
  label: 'Capture screen',
  tool: 'vision__screen_capture',
  skill_id: 'vision',
  endpoint_id: 'screen_capture',
  args_preview: '{"region":"full desktop"}',
  result_preview: '1920 x 1200 jpeg',
  success: true,
  error: '',
  error_code: '',
  latency_ms: 412,
};

describe('the tool card head lines up', () => {
  it('renders exactly one cell per declared grid track', () => {
    const { container } = render(<ToolCallCard trace={TRACE} />);
    const head = container.querySelector('.v2-tool-card__head');
    expect(head).not.toBeNull();
    expect(head.children.length).toBe(declaredTracks());
  });

  it('keeps the name and its argument in one cell, on one baseline', () => {
    const { container } = render(<ToolCallCard trace={TRACE} />);
    const name = container.querySelector('.v2-tool-card__name');
    expect(name).not.toBeNull();
    expect(name.querySelector('.v2-tool-card__label').textContent)
      .toBe('Capture screen');
    expect(name.querySelector('.v2-tool-card__summary')).not.toBeNull();
  });

  it('keeps the status word and the duration together in the last cell', () => {
    const { container } = render(<ToolCallCard trace={TRACE} />);
    const head = container.querySelector('.v2-tool-card__head');
    const meta = head.children[head.children.length - 1];
    expect(meta.className).toContain('v2-tool-card__meta');
    expect(meta.querySelector('[data-testid="tool-card-status"]').textContent)
      .toBe('done');
    expect(meta.querySelector('[data-testid="tool-card-duration"]').textContent)
      .toBe('412ms');
  });

  /**
   * At <=560px pages.css hides the argument line, which leaves the tool
   * name as the only thing that can give ground. It is `flex-shrink: 0`
   * at full width by design, so a long label ("Read accessibility tree")
   * ran under the status word and the duration. The override has to live
   * in THIS file: index.css imports markdown.css after pages.css, so a
   * same-specificity rule written next to the other chat media queries
   * loses the cascade and silently does nothing.
   */
  it('lets the tool name shrink at narrow widths, from this stylesheet', () => {
    const narrow = /@media\s*\(max-width:\s*560px\)\s*\{([\s\S]*?)\n\}/g;
    const blocks = [...CSS.matchAll(narrow)].map((m) => m[1]);
    const rule = blocks.find((b) => b.includes('.v2-tool-card__label'));
    expect(rule, 'no narrow-width .v2-tool-card__label rule in markdown.css').toBeTruthy();
    expect(rule).toMatch(/flex-shrink:\s*1/);
    expect(rule).toMatch(/text-overflow:\s*ellipsis/);
  });

  it('captions each body block with the class the design styles', () => {
    const { container } = render(<ToolCallCard trace={TRACE} defaultOpen />);
    const caps = [...container.querySelectorAll('.v2-tool-card__cap')]
      .map((n) => n.textContent);
    expect(caps).toContain('args');
    expect(caps).toContain('result');
  });
});
