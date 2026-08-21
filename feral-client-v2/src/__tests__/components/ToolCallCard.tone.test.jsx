/**
 * Tool card: outcome tone, family glyph, duration, default disclosure.
 *
 * The three defects under test:
 *   1. Outcome was drawn on two different scales. A failure took a
 *      full-strength border AND a tinted background; a success took a
 *      border at 18% alpha and no tint. The two states could not be
 *      compared at a glance because they were not the same treatment.
 *   2. Five glyphs covered forty-one skills, and all five encoded the
 *      outcome, so every card looked the same.
 *   3. All seven result shapes rendered collapsed, so an image, a diff
 *      and a row list were each one grey line.
 */
import React from 'react';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import ToolCallCard, { ToolCallList } from '../../components/ToolCallCard';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CSS = fs.readFileSync(path.resolve(HERE, '../../styles/markdown.css'), 'utf8');

/** Every declaration block whose selector list mentions `selector`. */
function ruleFor(selector) {
  const out = [];
  let from = 0;
  for (;;) {
    const i = CSS.indexOf(selector, from);
    if (i < 0) break;
    const open = CSS.indexOf('{', i);
    const close = CSS.indexOf('}', open);
    out.push(CSS.slice(open + 1, close));
    from = close + 1;
  }
  return out.join(' ');
}

describe('outcome tone is one scale', () => {
  it('draws the rail at full strength for EVERY outcome', () => {
    // The defect: `--ok` used the 18%-alpha soft token as its border
    // while `--failed` used the full hue.
    const rail = ruleFor('.v2-tool-card {');
    expect(rail).toContain('border-left: 2px solid var(--v2-tool-tone)');
    for (const selector of ['--running', '--ok', '--failed', '--refused']) {
      expect(ruleFor(`.v2-tool-card${selector}`)).not.toContain('border-left');
    }
  });

  it('reserves the wash for the two states that want the reader to stop', () => {
    const washed = ruleFor('.v2-tool-card--failed,\n.v2-tool-card--refused');
    expect(washed).toContain('background: var(--v2-tool-tone-soft)');
    // Success and running are on the same side of the rule as each
    // other, so a normal turn is not a wall of green.
    expect(CSS).not.toMatch(/\.v2-tool-card--ok[^{]*\{[^}]*background:/);
  });

  it('declares a hue and a wash for every outcome', () => {
    const tones = {
      '.v2-tool-card--running': ['--v2-accent', '--v2-accent-soft'],
      '.v2-tool-card--ok': ['--v2-state-live', '--v2-state-live-soft'],
      '.v2-tool-card--failed': ['--v2-state-error', '--v2-state-error-soft'],
      '.v2-tool-card--refused': ['--v2-state-warn', '--v2-state-warn-soft'],
    };
    for (const [selector, [hue, soft]] of Object.entries(tones)) {
      const rule = ruleFor(selector);
      expect(rule, `${selector} missing`).toContain(`--v2-tool-tone: var(${hue})`);
      expect(rule, `${selector} missing`).toContain(`--v2-tool-tone-soft: var(${soft})`);
    }
  });

  it('never re-introduces the soft token as a BORDER on one state only', () => {
    // The original defect, literally: `border-left-color:
    // var(--v2-state-live-soft)` on ok while failed used the full hue.
    expect(CSS).not.toContain('border-left-color: var(--v2-state-live-soft)');
  });

  it('tags the card with its status and family for the tone rules', () => {
    render(<ToolCallCard trace={{ key: 'k', label: 'Run local command', tool: 'coding_tools__bash', success: true, latency_ms: 12 }} />);
    const card = screen.getByTestId('tool-call-card');
    expect(card).toHaveAttribute('data-status', 'ok');
    expect(card).toHaveAttribute('data-family', 'code');
    expect(card.className).toContain('v2-tool-card--ok');
  });
});

describe('a glyph per skill family', () => {
  const cases = [
    ['web_search__search', 'search'],
    ['browser__navigate', 'browser'],
    ['coding_tools__read_file', 'code'],
    ['gui_computer_use__click', 'computer'],
    ['screen_capture__grab', 'vision'],
    ['email__send', 'comms'],
    ['calendar_google__list_events', 'schedule'],
    ['feral_workflows__run', 'tasks'],
    ['notion__append', 'notes'],
    ['image_gen__create', 'media'],
    ['cutebot__drive', 'hardware'],
    ['health_data__summary', 'health'],
    ['system_settings__get', 'system'],
  ];

  it('renders a distinct family per skill, not one status icon for all', () => {
    const seen = new Set();
    for (const [tool, family] of cases) {
      const { unmount } = render(<ToolCallCard trace={{ key: tool, label: tool, tool, success: true, latency_ms: 5 }} />);
      const card = screen.getByTestId('tool-call-card');
      expect(card).toHaveAttribute('data-family', family);
      const glyph = card.querySelector('.v2-tool-card__icon');
      expect(glyph).toBeTruthy();
      // The path data is what actually differs between lucide icons.
      seen.add(glyph.querySelector('path,circle,rect,line')?.getAttribute('d') || glyph.innerHTML);
      unmount();
    }
    expect(seen.size).toBe(cases.length);
  });

  it('keeps the shield for a refusal, because no family glyph can say that', () => {
    render(<ToolCallCard trace={{
      key: 'k', label: 'Create reminder', tool: 'feral_reminders__create',
      success: false, error_code: 'plan_mode_blocked', error: 'Plan mode is active.',
    }} />);
    const card = screen.getByTestId('tool-call-card');
    expect(card).toHaveAttribute('data-status', 'refused');
    expect(card.querySelector('.v2-tool-card__icon--refused')).toBeTruthy();
  });

  it('keeps the spinner while a call is in flight', () => {
    render(<ToolCallCard trace={{ key: 'k', label: 'Search web', tool: 'web_search__search', success: null, started_at: Date.now() }} />);
    expect(screen.getByTestId('tool-call-card').querySelector('.v2-spin')).toBeTruthy();
  });
});

describe('duration on the card head', () => {
  it('shows the settled latency in the head', () => {
    render(<ToolCallCard trace={{ key: 'k', label: 'Search web', tool: 'web_search__search', success: true, latency_ms: 1240 }} />);
    expect(screen.getByTestId('tool-card-duration')).toHaveTextContent('1.24s');
  });

  it('states an outcome word for success too, not only for failure', () => {
    render(<ToolCallCard trace={{ key: 'k', label: 'Search web', tool: 'web_search__search', success: true, latency_ms: 5 }} />);
    expect(screen.getByTestId('tool-card-status')).toHaveTextContent('done');
  });

  it('omits the duration rather than inventing 0ms when none was reported', () => {
    render(<ToolCallCard trace={{ key: 'k', label: 'Search web', tool: 'web_search__search', success: true, latency_ms: 0 }} />);
    expect(screen.queryByTestId('tool-card-duration')).toBeNull();
  });

  it('names outcome and duration in the accessible label, since tone is invisible to a reader', () => {
    render(<ToolCallCard trace={{ key: 'k', label: 'Search web', tool: 'web_search__search', success: false, error: 'boom', latency_ms: 300 }} />);
    expect(screen.getByRole('button', { name: /Search, failed in 300ms/ })).toBeInTheDocument();
  });

  it('totals the group duration on the group head', () => {
    render(<ToolCallList traces={[
      { key: 'a', label: 'A', tool: 'web_search__search', success: true, latency_ms: 400 },
      { key: 'b', label: 'B', tool: 'browser__navigate', success: true, latency_ms: 600 },
    ]} />);
    expect(screen.getByTestId('tool-group-duration')).toHaveTextContent('1.00s');
  });

  it('holds the summary column even when a call has no arguments', () => {
    // Without the spacer the head collapsed to "Grab DONE 90ms" while
    // every neighbouring card right-aligned its status word.
    render(<ToolCallCard trace={{ key: 'k', label: 'Grab', tool: 'screen_capture__grab', success: true, latency_ms: 90 }} />);
    expect(screen.getByTestId('tool-call-card').querySelector('.v2-tool-card__summary')).toBeTruthy();
  });
});

describe('group summary counts a refusal as a refusal', () => {
  const traces = [
    { key: 'a', label: 'A', tool: 'web_search__search', success: true, latency_ms: 5 },
    { key: 'b', label: 'B', tool: 'coding_tools__bash', success: false, error: 'exit 1', latency_ms: 5 },
    { key: 'c', label: 'C', tool: 'feral_reminders__create', success: false, error_code: 'plan_mode_blocked', latency_ms: 5 },
  ];

  it('never folds a held boundary into the failure count', () => {
    render(<ToolCallList traces={traces} />);
    const head = screen.getByTestId('tool-call-list').querySelector('.v2-tool-group__meta');
    expect(head.textContent).toContain('1 failed');
    expect(head.textContent).toContain('1 refused');
    expect(head.textContent).not.toContain('2 failed');
  });

  it('reports refused as the group tone when nothing actually failed', () => {
    render(<ToolCallList traces={[traces[0], traces[2]]} />);
    expect(screen.getByTestId('tool-call-list')).toHaveAttribute('data-status', 'refused');
  });
});

describe('informative result shapes are not collapsed', () => {
  const openFor = (result_preview, args_preview = { path: 'src/App.jsx' }) => {
    const { unmount } = render(<ToolCallCard trace={{
      key: 'k', label: 'Read file', tool: 'coding_tools__read_file',
      args_preview, result_preview, success: true, latency_ms: 10,
    }} />);
    const open = screen.getByRole('button', { name: /Read file/ }).getAttribute('aria-expanded');
    unmount();
    return open === 'true';
  };

  it('opens image, table, code, markdown and json results', () => {
    expect(openFor('https://example.com/shot.png')).toBe(true);
    expect(openFor(JSON.stringify([{ a: 1, b: 2 }, { a: 3, b: 4 }]))).toBe(true);
    expect(openFor('diff --git a/x b/x\n@@ -1,2 +1,2 @@\n-a\n+b')).toBe(true);
    expect(openFor('# heading\n\n```js\nconst a = 1;\n```')).toBe(true);
    expect(openFor(JSON.stringify({ status: 'ok', rows: 3 }))).toBe(true);
  });

  it('opens a plain file read, because the args name a language', () => {
    // `languageHint` turns a read of src/App.jsx into a `code` shape,
    // which is exactly the case a collapsed card destroys.
    expect(openFor('const a = 1;\n')).toBe(true);
  });

  it('leaves plain stdout and empty results collapsed', () => {
    // No path in the args, so no language hint: this is the `text`
    // shape, the bucket for stdout and one-word statuses.
    expect(openFor('done', { command: 'make test' })).toBe(false);
    expect(openFor('', { command: 'make test' })).toBe(false);
    expect(openFor(null, { command: 'make test' })).toBe(false);
  });

  it('opens a failure and a refusal, whose whole payload is the error', () => {
    render(<ToolCallCard trace={{ key: 'f', label: 'Run local command', tool: 'coding_tools__bash', success: false, error: 'exit 1' }} />);
    expect(screen.getByRole('button', { name: /Run local command/ })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('exit 1')).toBeInTheDocument();
  });

  it('does not fan a group of parallel calls open', () => {
    render(<ToolCallList traces={[
      { key: 'a', label: 'A', tool: 'web_search__search', success: true, latency_ms: 5, result_preview: JSON.stringify([{ a: 1 }]) },
      { key: 'b', label: 'B', tool: 'web_search__search', success: true, latency_ms: 5, result_preview: JSON.stringify([{ a: 2 }]) },
    ]} />);
    for (const name of [/^A/, /^B/]) {
      expect(screen.getByRole('button', { name })).toHaveAttribute('aria-expanded', 'false');
    }
  });

  it('still opens a FAILURE inside a group', () => {
    render(<ToolCallList traces={[
      { key: 'a', label: 'A', tool: 'web_search__search', success: true, latency_ms: 5 },
      { key: 'b', label: 'B', tool: 'web_search__search', success: false, error: 'nope', latency_ms: 5 },
    ]} />);
    expect(screen.getByRole('button', { name: /^B/ })).toHaveAttribute('aria-expanded', 'true');
  });

  it('respects a card the reader closed, even after the result lands', () => {
    const running = {
      key: 'k', label: 'Read file', tool: 'coding_tools__read_file',
      success: null, started_at: Date.now(),
    };
    const { rerender } = render(<ToolCallCard trace={running} />);
    const head = screen.getByRole('button', { name: /Read file/ });
    expect(head).toHaveAttribute('aria-expanded', 'false');

    // Result lands: the card adopts the open default.
    const settled = { ...running, success: true, latency_ms: 40, result_preview: JSON.stringify({ lines: 3 }) };
    rerender(<ToolCallCard trace={settled} />);
    expect(screen.getByRole('button', { name: /Read file/ })).toHaveAttribute('aria-expanded', 'true');

    // The reader closes it; a re-render must not reopen it.
    fireEvent.click(screen.getByRole('button', { name: /Read file/ }));
    rerender(<ToolCallCard trace={{ ...settled, latency_ms: 41 }} />);
    expect(screen.getByRole('button', { name: /Read file/ })).toHaveAttribute('aria-expanded', 'false');
  });
});
