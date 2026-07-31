/**
 * Per-turn attribution footer: which model answered, and what it cost.
 *
 * The contract that matters here is the negative one. Providers differ in
 * whether they report usage at all (the chat-completions streaming path
 * reports none unless `stream_options.include_usage` is set, which this
 * repo never sets), so the component has to distinguish "nothing was
 * reported" from "the turn cost zero". Rendering a fabricated `0 tokens`
 * would look exactly like a measurement and would quietly destroy trust in
 * the meter -- which is the entire reason the footer exists.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { TurnMeta } from '../../pages/Chat.jsx';

const USAGE = { input_tokens: 10341, output_tokens: 275, total_tokens: 10616 };

describe('TurnMeta', () => {
  it('shows the answering model and the total token count', () => {
    render(<TurnMeta model="gpt-5.6-sol" usage={USAGE} />);
    expect(screen.getByText('gpt-5.6-sol')).toBeInTheDocument();
    expect(screen.getByText(/10,616\s*tokens/)).toBeInTheDocument();
  });

  it('breaks the count into input and output in the tooltip', () => {
    render(<TurnMeta model="gpt-5.6-sol" usage={USAGE} />);
    expect(screen.getByTestId('chat-turn-meta')).toHaveAttribute(
      'title',
      '10,341 in + 275 out = 10,616 tokens',
    );
  });

  it('renders nothing when the provider reported neither', () => {
    const { container } = render(<TurnMeta model="" usage={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing rather than "0 tokens" for an all-zero usage block', () => {
    const { container } = render(
      <TurnMeta model="" usage={{ input_tokens: 0, output_tokens: 0, total_tokens: 0 }} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the model alone when usage is unavailable', () => {
    render(<TurnMeta model="kimi-k2" usage={null} />);
    expect(screen.getByText('kimi-k2')).toBeInTheDocument();
    expect(screen.queryByText(/tokens/)).not.toBeInTheDocument();
  });

  it('shows tokens alone when the model name is unavailable', () => {
    render(<TurnMeta model="" usage={USAGE} />);
    expect(screen.getByText(/10,616\s*tokens/)).toBeInTheDocument();
  });

  it('derives the total when the provider reports only the two halves', () => {
    render(<TurnMeta model="" usage={{ input_tokens: 100, output_tokens: 25 }} />);
    expect(screen.getByText(/125\s*tokens/)).toBeInTheDocument();
  });
});
