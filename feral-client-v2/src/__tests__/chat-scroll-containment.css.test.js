/**
 * Chat scroll containment, as a stylesheet contract.
 *
 * READ THIS BEFORE ASSUMING IT IS A BEHAVIOUR TEST. It is not, and it
 * cannot be. The bug is a layout bug and jsdom has no layout engine:
 * `scrollHeight`, `clientHeight` and every `getBoundingClientRect()` are
 * hard-coded zeros, and vitest never loads `src/styles/*.css` into the
 * document at all, so there is nothing in jsdom that could tell a
 * contained scroller from an uncontained one. A test that rendered <Chat />
 * and asserted on scroll offsets would pass identically before and after
 * the fix, which is worse than no test. The real verification was done in
 * Chrome against the built bundle (see the report accompanying this
 * change): `.v2-shell-main` scrolled 3942px on a 60-message transcript,
 * carrying the pane and the composer to y = -3288, and scrolls 0px now.
 *
 * What this file does guard is the three declarations that fix is made
 * of, each of which reads like a stylistic nicety and is not:
 *
 *   .v2-copy-btn { position: relative }
 *       CopyButton renders its "Copied" live region as an inline-styled
 *       `position: absolute` sr-only span. With the button static, that
 *       span's containing block resolved past `.v2-chat-log` to `.v2-pane`
 *       (`.v2-glass` is relative). An absolutely positioned box is clipped
 *       only by ancestors between it and its containing block, so the
 *       log's `overflow-y: auto` did not clip it, and every assistant turn
 *       pushed its own transcript offset of layout overflow up into the
 *       pane, then `.v2-chat`, then `.v2-shell-main`.
 *
 *   .v2-chat-log { position: relative }
 *       makes the scroller its own containing block, so the next
 *       positioned descendant anyone adds cannot repeat that.
 *
 *   .v2-chat-log { overscroll-behavior: contain }
 *       stops the wheel chaining out of the transcript into the shell
 *       once the last message is reached.
 *
 * Deleting any of them is the regression. That is what fails here.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const STYLES = join(dirname(fileURLToPath(import.meta.url)), '..', 'styles');

function read(file) {
  return readFileSync(join(STYLES, file), 'utf8');
}

/** The declaration block of the first rule whose selector list is exactly `selector`. */
function ruleBody(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`(^|[}\\n])\\s*${escaped}\\s*\\{([^}]*)\\}`));
  expect(match, `no rule found for ${selector}`).not.toBeNull();
  return match[2];
}

describe('chat scroll containment (stylesheet contract)', () => {
  it('.v2-chat-log is a scroll container', () => {
    const body = ruleBody(read('pages.css'), '.v2-chat-log');
    expect(body).toMatch(/overflow-y:\s*auto/);
  });

  it('.v2-chat-log is its own containing block for positioned descendants', () => {
    const body = ruleBody(read('pages.css'), '.v2-chat-log');
    expect(
      /position:\s*relative/.test(body),
      'the transcript scroller must establish a containing block, or an absolutely '
      + 'positioned descendant escapes its clip and pushes layout overflow onto '
      + '.v2-pane, which makes .v2-shell-main scroll the whole chat off screen',
    ).toBe(true);
  });

  it('.v2-chat-log does not chain its scroll into the shell', () => {
    const body = ruleBody(read('pages.css'), '.v2-chat-log');
    expect(body).toMatch(/overscroll-behavior:\s*contain/);
  });

  it('.v2-copy-btn anchors its own absolutely positioned live region', () => {
    const body = ruleBody(read('markdown.css'), '.v2-copy-btn');
    expect(
      /position:\s*relative/.test(body),
      'CopyButton positions its sr-only status span absolutely; without a '
      + 'positioned button the span is laid out against .v2-pane and is not '
      + 'clipped by the transcript scroller',
    ).toBe(true);
  });

  it('.v2-shell sizes to the dynamic viewport, with a vh fallback', () => {
    const body = ruleBody(read('ui.css'), '.v2-shell');
    // vh first so engines without dvh still get a full-height shell; dvh
    // second so mobile Safari does not size the shell to the toolbar-
    // retracted viewport and hand the document a scrollable strip.
    expect(body).toMatch(/height:\s*100vh/);
    expect(body).toMatch(/height:\s*100dvh/);
    expect(body.indexOf('100vh')).toBeLessThan(body.indexOf('100dvh'));
  });
});
