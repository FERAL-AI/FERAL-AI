/**
 * ToolResultView rendering contract: a file read, a shell dump, a web
 * search hit list, a screenshot and an API body must not all render
 * identically, and none of them may stretch the page.
 */
import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import ToolResultView from '../../components/ToolResultView';

function shapeOf(container) {
  return container.querySelector('[data-shape]')?.getAttribute('data-shape');
}

describe('ToolResultView', () => {
  it('renders nothing for empty results', () => {
    const { container } = render(<ToolResultView value="" />);
    expect(container.innerHTML).toBe('');
  });

  it('renders a row list as a real table with a row count', () => {
    const { container } = render(
      <ToolResultView value={[
        { title: 'Tahoe notes', url: 'https://a' },
        { title: 'Release', url: 'https://b' },
      ]} />,
    );
    expect(shapeOf(container)).toBe('table');
    const table = screen.getByRole('table');
    expect(within(table).getByText('title')).toBeInTheDocument();
    expect(within(table).getByText('Tahoe notes')).toBeInTheDocument();
    expect(screen.getByText(/2 rows/)).toBeInTheDocument();
  });

  it('unwraps an envelope around the row list', () => {
    const { container } = render(<ToolResultView value={{ results: [{ a: 1 }] }} />);
    expect(shapeOf(container)).toBe('table');
    expect(screen.getByText(/1 row · results/)).toBeInTheDocument();
  });

  it('renders source text as a highlighted code block when a language is known', () => {
    const { container } = render(
      <ToolResultView value={'def f():\n    return 1'} language="python" />,
    );
    expect(shapeOf(container)).toBe('code');
    expect(container.querySelector('.v2-md-pre')).toBeTruthy();
    expect(container.innerHTML).toMatch(/language-python/);
  });

  it('renders a diff without a caller hint', () => {
    const { container } = render(
      <ToolResultView value={'@@ -1,2 +1,2 @@\n-old\n+new'} />,
    );
    expect(shapeOf(container)).toBe('code');
    expect(container.querySelector('[data-lang="diff"]')).toBeTruthy();
  });

  it('renders an image result inline', () => {
    render(<ToolResultView value="https://example.com/shot.png" />);
    const img = screen.getByRole('img');
    expect(img).toHaveAttribute('src', 'https://example.com/shot.png');
  });

  it('pretty-prints a JSON body', () => {
    const { container } = render(<ToolResultView value={{ ok: true, n: 2 }} />);
    expect(shapeOf(container)).toBe('json');
    expect(screen.getByTestId('toolres-pre').textContent).toContain('"ok": true');
  });

  it('clamps long text and expands on demand', () => {
    const long = Array.from({ length: 40 }, (_, i) => `line ${i}`).join('\n');
    render(<ToolResultView value={long} clampLines={5} />);
    const pre = screen.getByTestId('toolres-pre');
    expect(pre.textContent).toContain('line 4');
    expect(pre.textContent).not.toContain('line 30');

    const more = screen.getByRole('button', { name: /Show all 40 lines/ });
    expect(more).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(more);
    expect(screen.getByTestId('toolres-pre').textContent).toContain('line 39');
    fireEvent.click(screen.getByRole('button', { name: /Show less/ }));
    expect(screen.getByTestId('toolres-pre').textContent).not.toContain('line 39');
  });

  it('does not clamp short text', () => {
    render(<ToolResultView value={'one\ntwo'} clampLines={5} />);
    expect(screen.queryByRole('button', { name: /Show all/ })).toBeNull();
  });

  it('offers a copy affordance that can be suppressed', () => {
    const { rerender } = render(<ToolResultView value="hello" />);
    expect(screen.getByTestId('copy-button')).toBeInTheDocument();
    rerender(<ToolResultView value="hello" copyable={false} />);
    expect(screen.queryByTestId('copy-button')).toBeNull();
  });
});
