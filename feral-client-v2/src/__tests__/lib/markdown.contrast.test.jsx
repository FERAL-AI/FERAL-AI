/**
 * Markdown contrast contract.
 *
 * Demo-blocker repro: bare single-backtick words like `badr` and
 * `CHANGELOG.md` rendered as washed-out, low-contrast spans because
 * react-markdown 9 dropped the `inline` prop and our `code` component
 * was routing every <code> through the highlight.js path. Also,
 * unlabeled fenced ```blocks``` were being auto-detected by hljs and
 * common English words ("and", "on", "in") were painted orange/yellow.
 *
 * Contract:
 *   - inline code uses the legible `v2-md-code-inline` class.
 *   - inline code does NOT get the hljs class (no syntax palette).
 *   - unlabeled fenced blocks do NOT trigger language detection.
 *   - language-tagged fenced blocks still highlight.
 */
import React from 'react';
import { render } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import MarkdownMessage from '../../lib/markdown.jsx';

function html(text) {
  const { container } = render(<MarkdownMessage text={text} />);
  return container.innerHTML;
}

describe('MarkdownMessage — contrast and highlight gating', () => {
  it('renders inline single-backtick code with v2-md-code-inline (not hljs)', () => {
    const out = html('hello `badr` world');
    expect(out).toContain('class="v2-md-code-inline"');
    // The inline span must not pick up the hljs palette.
    expect(out).not.toMatch(/<code[^>]*class="hljs[^"]*"[^>]*>badr<\/code>/);
  });

  it('renders inline code for path-like tokens too', () => {
    const out = html('see `CHANGELOG.md`');
    expect(out).toMatch(/<code class="v2-md-code-inline"[^>]*>CHANGELOG\.md<\/code>/);
  });

  it('language-tagged fenced blocks still highlight', () => {
    const out = html('```python\nfor i in range(3):\n    print(i)\n```');
    expect(out).toContain('class="v2-md-pre"');
    expect(out).toMatch(/class="hljs[^"]*language-python"/);
  });

  it('unlabeled fenced blocks are NOT auto-detected by hljs', () => {
    // English prose dumped into ```...``` should not get a language-X
    // class — that was painting common words orange.
    const out = html('```\nfocus today on the demo and ship it\n```');
    expect(out).toContain('class="v2-md-pre"');
    expect(out).not.toMatch(/language-\w+/);
  });
});
