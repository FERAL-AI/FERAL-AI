/**
 * Chat-surface primitives: CopyButton, ReasoningSection, ChatNotice,
 * and the fenced-code chrome added to MarkdownMessage.
 *
 * Accessibility is part of the contract here: every disclosure is a
 * real button with aria-expanded + aria-controls, and every failure
 * state carries role="alert".
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import CopyButton from '../../ui/CopyButton';
import ReasoningSection from '../../components/ReasoningSection';
import ChatNotice from '../../components/ChatNotice';
import MarkdownMessage from '../../lib/markdown.jsx';

describe('CopyButton', () => {
  let writeText;

  beforeEach(() => {
    writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('copies the value and confirms', async () => {
    render(<CopyButton value="payload" />);
    const btn = screen.getByRole('button', { name: 'Copy' });
    fireEvent.click(btn);
    expect(writeText).toHaveBeenCalledWith('payload');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Copied' })).toBeInTheDocument());
  });

  it('accepts a lazy value and renders optional text', async () => {
    render(<CopyButton value={() => 'lazy'} withText label="Copy code" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy code' }));
    expect(writeText).toHaveBeenCalledWith('lazy');
    await waitFor(() => expect(screen.getByText('Copied')).toBeInTheDocument());
  });

  it('never throws when the clipboard is unavailable', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    document.execCommand = vi.fn(() => false);
    render(<CopyButton value="x" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    // Stays in the un-copied state rather than lying about success.
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument();
  });

  it('does nothing for an empty payload', () => {
    render(<CopyButton value="" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(writeText).not.toHaveBeenCalled();
  });
});

describe('ReasoningSection', () => {
  it('renders nothing without text', () => {
    const { container } = render(<ReasoningSection text="   " />);
    expect(container.innerHTML).toBe('');
  });

  it('is collapsed by default and toggles with a proper disclosure contract', () => {
    render(<ReasoningSection text="step one" />);
    const head = screen.getByRole('button');
    expect(head).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('step one')).toBeNull();
    fireEvent.click(head);
    expect(head).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('step one')).toBeInTheDocument();
    expect(document.getElementById(head.getAttribute('aria-controls'))).toBeTruthy();
  });

  it('reads as "Thinking" while streaming and "Reasoning" once settled', () => {
    const { rerender } = render(<ReasoningSection text="x" streaming />);
    expect(screen.getByText('Thinking')).toBeInTheDocument();
    rerender(<ReasoningSection text="x" durationMs={4200} />);
    expect(screen.getByText('Reasoning')).toBeInTheDocument();
    expect(screen.getByText('thought for 4.20s')).toBeInTheDocument();
  });
});

describe('ChatNotice', () => {
  it('renders an error with code, message and hint as an alert', () => {
    render(<ChatNotice kind="error" code="llm_timeout" message="upstream timed out" hint="try again" />);
    const notice = screen.getByRole('alert');
    expect(notice).toHaveAttribute('data-kind', 'error');
    expect(notice.textContent).toContain('That turn failed');
    expect(notice.textContent).toContain('llm_timeout');
    expect(notice.textContent).toContain('upstream timed out');
    expect(notice.textContent).toContain('try again');
  });

  it('renders a refusal with its own title and styling hook', () => {
    render(<ChatNotice kind="refusal" message="supervisor paused" />);
    const notice = screen.getByRole('alert');
    expect(notice).toHaveAttribute('data-kind', 'refusal');
    expect(notice).toHaveClass('v2-chat-notice--refusal');
    expect(notice.textContent).toContain('FERAL declined this request');
  });

  it('renders a stalled turn and an optional retry action', () => {
    const onRetry = vi.fn();
    render(<ChatNotice kind="stalled" message="no content" onRetry={onRetry} />);
    expect(screen.getByRole('alert').textContent).toContain('The turn ended without a reply');
    fireEvent.click(screen.getByRole('button', { name: /Retry/ }));
    expect(onRetry).toHaveBeenCalled();
  });

  it('falls back to the error kind for an unknown kind', () => {
    render(<ChatNotice kind="nonsense" message="?" />);
    expect(screen.getByRole('alert').textContent).toContain('That turn failed');
  });
});

describe('MarkdownMessage code chrome', () => {
  it('labels the language and exposes a copy button', () => {
    const { container } = render(
      <MarkdownMessage text={'```python\nprint(1)\n```'} />,
    );
    expect(container.querySelector('.v2-md-codeblock')).toHaveAttribute('data-lang', 'python');
    expect(screen.getByText('python')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Copy code' })).toBeInTheDocument();
    // The <pre> contract older tests rely on is preserved.
    expect(container.querySelector('.v2-md-pre')).toBeTruthy();
  });

  it('labels untagged fences as plain text without inventing a language', () => {
    const { container } = render(<MarkdownMessage text={'```\nhello\n```'} />);
    expect(screen.getByText('plain text')).toBeInTheDocument();
    expect(container.querySelector('.v2-md-codeblock')).not.toHaveAttribute('data-lang');
  });

  it('copies the raw source, not the highlighted spans', async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(<MarkdownMessage text={'```js\nconst a = 1;\n```'} />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy code' }));
    expect(writeText).toHaveBeenCalledWith('const a = 1;');
  });
});
