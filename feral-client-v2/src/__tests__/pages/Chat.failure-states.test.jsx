/**
 * Chat failure-state + live-tool contract.
 *
 * The maintainer's #1 complaint was silent failure: a turn that
 * errored, was refused, or died mid-stream left either a blank log or
 * a spinner that never resolved. Every path below must end in a
 * visible, readable row.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, cleanup, fireEvent, screen } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Chat from '../../pages/Chat';

const listeners = new Set();

vi.mock('../../hooks/useFeralSocket', async () => {
  const fakeSocket = {
    state: 'open',
    subscribe: (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    onState: (fn) => {
      try { fn('open'); } catch { /* test stub */ }
      return () => {};
    },
    send: vi.fn(),
  };
  return {
    useFeralSocket: () => fakeSocket,
    sendUiEvent: vi.fn(),
  };
});

function emit(msg) {
  listeners.forEach((fn) => fn(msg));
}

async function sendTurn(container, text = 'go') {
  const input = container.querySelector('.v2-chat-input');
  const form = container.querySelector('form.v2-chat-composer');
  await act(async () => {
    fireEvent.change(input, { target: { value: text } });
    fireEvent.submit(form);
  });
}

afterEach(() => {
  listeners.clear();
  cleanup();
});

describe('Chatfailure states are visible', () => {
  it('renders an inline notice for an error frame and stops the spinner', async () => {
    const { container } = renderV2(<Chat />);
    await sendTurn(container);
    expect(container.querySelector('[data-testid="chat-working"]')).toBeTruthy();

    await act(async () => {
      emit({
        type: 'error',
        payload: { code: 'llm_timeout', message: 'provider timed out', recoverable: true },
      });
    });

    const notice = screen.getByTestId('chat-notice');
    expect(notice).toHaveAttribute('data-kind', 'error');
    expect(notice.textContent).toContain('provider timed out');
    expect(notice.textContent).toContain('llm_timeout');
    // No eternal spinner.
    expect(container.querySelector('[data-testid="chat-working"]')).toBeNull();
    expect(container.querySelector('.v2-chat-cursor')).toBeNull();
  });

  it('renders a refusal with its retry hint', async () => {
    const { container } = renderV2(<Chat />);
    await sendTurn(container);
    await act(async () => {
      emit({
        type: 'refusal',
        payload: {
          reason: 'supervisor is paused',
          retry_hint: 'resume supervisor in Settings → Oversight',
          source: 'supervisor',
        },
      });
    });
    const notice = screen.getByTestId('chat-notice');
    expect(notice).toHaveAttribute('data-kind', 'refusal');
    expect(notice.textContent).toContain('supervisor is paused');
    expect(notice.textContent).toContain('resume supervisor');
    expect(container.querySelector('[data-testid="chat-working"]')).toBeNull();
  });

  it('keeps the tool trace attached to the failing turn', async () => {
    const { container } = renderV2(<Chat />);
    await sendTurn(container);
    await act(async () => {
      emit({ type: 'tool_start', payload: { tool: 'computer_use__bash', call_id: 'c1', args_preview: '{"command":"pytest"}' } });
      emit({ type: 'tool_result', payload: { tool: 'computer_use__bash', call_id: 'c1', success: false, error: 'exit 1', latency_ms: 40 } });
      emit({ type: 'error', payload: { code: 'tool_failed', message: 'the command failed' } });
    });
    expect(screen.getByTestId('chat-notice').textContent).toContain('the command failed');
    const card = screen.getByTestId('tool-call-card');
    expect(card).toHaveAttribute('data-status', 'failed');
    expect(card.textContent).toContain('Run local command');
  });

  it('surfaces a stream that finalises with nothing at all', async () => {
    const { container } = renderV2(<Chat />);
    await sendTurn(container);
    await act(async () => {
      emit({ type: 'stream_delta', payload: { delta: '', is_final: true } });
    });
    const notice = screen.getByTestId('chat-notice');
    expect(notice).toHaveAttribute('data-kind', 'stalled');
    expect(notice.textContent).toContain('without sending any content');
    expect(container.querySelector('[data-testid="chat-working"]')).toBeNull();
  });

  it('ignores idle-socket error frames when no turn is in flight', async () => {
    renderV2(<Chat />);
    await act(async () => {
      emit({ type: 'error', payload: { code: 'ws_blip', message: 'transient' } });
    });
    expect(screen.queryByTestId('chat-notice')).toBeNull();
  });

  it('does not emit a stalled notice for an idle finalisation', async () => {
    renderV2(<Chat />);
    await act(async () => {
      emit({ type: 'stream_delta', payload: { delta: '', is_final: true } });
    });
    expect(screen.queryByTestId('chat-notice')).toBeNull();
  });
});

describe('Chatlive tool rendering', () => {
  it('renders an in-flight tool call before the turn commits', async () => {
    const { container } = renderV2(<Chat />);
    await sendTurn(container);
    await act(async () => {
      emit({
        type: 'tool_start',
        payload: { tool: 'computer_use__read_file', call_id: 'c1', args_preview: '{"path":"src/App.jsx"}' },
      });
    });
    const card = screen.getByTestId('tool-call-card');
    expect(card).toHaveAttribute('data-status', 'running');
    expect(card.textContent).toContain('Read file');
    expect(card.textContent).toContain('src/App.jsx');
    // Working indicator names the tool rather than a bare "thinking".
    expect(container.querySelector('[data-testid="chat-working"]').textContent)
      .toContain('using Read file');
  });

  it('keeps the working chip on a still-running call when a sibling returns', async () => {
    const { container } = renderV2(<Chat />);
    await sendTurn(container);
    await act(async () => {
      emit({ type: 'tool_start', payload: { tool: 'web_search__run', call_id: 'a', args_preview: '{"q":"x"}' } });
      emit({ type: 'tool_start', payload: { tool: 'computer_use__bash', call_id: 'b', args_preview: '{"command":"ls"}' } });
      emit({ type: 'tool_result', payload: { tool: 'web_search__run', call_id: 'a', success: true, latency_ms: 10 } });
    });
    const group = screen.getByTestId('tool-call-list');
    expect(group).toHaveAttribute('data-status', 'running');
    expect(group.textContent).toContain('2 tool calls');
    expect(container.querySelector('[data-testid="chat-working"]').textContent)
      .toContain('using Run local command');
  });

  it('clears live tool cards when the turn commits', async () => {
    const { container } = renderV2(<Chat />);
    await sendTurn(container);
    await act(async () => {
      emit({ type: 'tool_start', payload: { tool: 'web_search__run', call_id: 'a', args_preview: '{"q":"x"}' } });
      emit({ type: 'tool_result', payload: { tool: 'web_search__run', call_id: 'a', success: true, latency_ms: 33 } });
      emit({ type: 'text_response', payload: { text: 'Answer.' } });
    });
    // Exactly one card: the committed one, not a live duplicate.
    expect(screen.getAllByTestId('tool-call-card')).toHaveLength(1);
    expect(container.querySelector('[data-testid="chat-working"]')).toBeNull();
    expect(container.textContent).toContain('Answer.');
  });
});

describe('Chatmessage structure', () => {
  it('gives the user turn a bubble and the assistant turn a copy action', async () => {
    const { container } = renderV2(<Chat />);
    await sendTurn(container, 'hello there');
    expect(container.querySelector('.v2-chat-bubble').textContent).toBe('hello there');

    await act(async () => {
      emit({ type: 'text_response', payload: { text: 'Hi back.' } });
    });
    const copy = screen.getAllByRole('button', { name: 'Copy message' });
    expect(copy.length).toBeGreaterThan(0);
  });
});
