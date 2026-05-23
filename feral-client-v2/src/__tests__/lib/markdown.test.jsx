// Lane 12 Wave 3 — MarkdownMessage rendering contract.
//
// Pins the S1 chat-polish acceptance items: code highlighting,
// table rendering, math typesetting, image cap, link safety. A future
// refactor that swaps the markdown stack must keep all of these.

import React from 'react';
import { render } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import MarkdownMessage from '../../lib/markdown.jsx';

function html(text) {
  const { container } = render(<MarkdownMessage text={text} />);
  return container.innerHTML;
}

describe('MarkdownMessage', () => {
  it('renders nothing for empty input', () => {
    const { container } = render(<MarkdownMessage text="" />);
    expect(container.innerHTML).toBe('');
  });

  it('renders fenced code blocks with hljs class', () => {
    const out = html('```python\nfor i in range(3):\n    print(i)\n```');
    expect(out).toContain('class="v2-md-pre"');
    expect(out).toMatch(/class="hljs[^"]*language-python"/);
  });

  it('renders GFM tables inside a scroll container', () => {
    const out = html('| a | b |\n| - | - |\n| 1 | 2 |');
    expect(out).toContain('class="v2-md-table-scroll"');
    expect(out).toContain('<table');
    expect(out).toContain('<th');
  });

  it('renders block math via rehype-katex', () => {
    const out = html('$$E = mc^2$$');
    // katex emits `katex-display` for block-level math.
    expect(out).toMatch(/katex/);
  });

  it('renders images and applies the cap class', () => {
    const out = html('![alt](https://example.com/x.png)');
    expect(out).toContain('v2-md-img');
    expect(out).toContain('src="https://example.com/x.png"');
  });

  it('blocks javascript: URLs in images', () => {
    // rehype's default sanitizer should remove these — render to a
    // span/no-op rather than letting them through.
    const out = html('![](javascript:alert(1))');
    expect(out).not.toContain('javascript:alert');
  });

  it('forces target=_blank rel=noopener on external links', () => {
    const out = html('[ex](https://example.com)');
    expect(out).toContain('target="_blank"');
    expect(out).toContain('rel="noopener noreferrer"');
  });

  it('strips trailing whitespace before render', () => {
    const out = html('hello\n\n\n\n');
    // Just verifies the trim path runs; we mainly check no extra
    // empty <p> blocks pile up at the end.
    const emptyPCount = (out.match(/<p><\/p>/g) || []).length;
    expect(emptyPCount).toBe(0);
  });
});
