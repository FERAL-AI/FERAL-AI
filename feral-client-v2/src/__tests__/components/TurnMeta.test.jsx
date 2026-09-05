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

/**
 * The count is a SUM, and had to stop pretending otherwise.
 *
 * Observed live: "241,240 tokens" under one assistant message, with the
 * header's context indicator reading 12.5k at the same moment. Both
 * numbers were correct. `agents/turn_attribution.py accumulate_turn_usage`
 * folds every LLM round of the turn into one running total, and a turn
 * that ran eight tool rounds re-sends the whole conversation on each one,
 * so the footer was roughly eight contexts added together while looking
 * exactly like a measurement of one. The fix is the label, not the
 * number: the total stays exact, and now says what it is a total OF.
 */
describe('TurnMeta says what the number counts', () => {
  it('names the round count when the brain reports one', () => {
    render(<TurnMeta model="gpt-5.6-sol" usage={{ ...USAGE, rounds: 8 }} />);
    expect(screen.getByText(/10,616 tokens across 8 rounds/)).toBeInTheDocument();
  });

  it('does not say "rounds" for a single round', () => {
    render(<TurnMeta model="" usage={{ ...USAGE, rounds: 1 }} />);
    expect(screen.getByText(/10,616 tokens across 1 round$/)).toBeInTheDocument();
  });

  it('still says the number spans the whole turn when no count is on the wire', () => {
    // `rounds` is not a field the brain sends today. Silence about the
    // scope is what made the number look like one context, so the
    // fallback states the scope without inventing a count.
    render(<TurnMeta model="gpt-5.6-sol" usage={USAGE} />);
    expect(screen.getByText(/10,616 tokens \(all rounds\)/)).toBeInTheDocument();
  });

  it('keeps the exact in/out split in the tooltip', () => {
    render(<TurnMeta model="gpt-5.6-sol" usage={{ ...USAGE, rounds: 8 }} />);
    expect(screen.getByTestId('chat-turn-meta')).toHaveAttribute(
      'title',
      '10,341 in + 275 out = 10,616 tokens',
    );
  });

  it('still renders nothing when the provider reported no usage at all', () => {
    const { container } = render(<TurnMeta model="" usage={{ rounds: 3 }} />);
    expect(container).toBeEmptyDOMElement();
  });
});
