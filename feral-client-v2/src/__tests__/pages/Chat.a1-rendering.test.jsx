import { describe, it, expect, afterEach } from 'vitest';
import { act, cleanup, fireEvent } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Chat, { sanitizeAssistantText } from '../../pages/Chat';

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

afterEach(() => {
  listeners.clear();
  cleanup();
});

describe('Chat — A1 rendering fixes', () => {
  it('sanitizer strips control tokens and tool-call tags', () => {
    expect(sanitizeAssistantText('hi<|eom|>')).toBe('hi');
    expect(sanitizeAssistantText('before</tool_calls>')).toBe('before');
    expect(sanitizeAssistantText('x <tool_calls>junk</tool_calls> y')).toBe('x  y');
    expect(sanitizeAssistantText('talking invoke[{"name":"q"}] done')).toContain('done');
    expect(sanitizeAssistantText('normal prose')).toBe('normal prose');
  });

  it('stream_delta residue never reaches the UI buffer', async () => {
    const { container } = renderV2(<Chat />);
    await act(async () => {
      emit({ type: 'stream_delta', payload: { delta: 'Hello<|eom|>', stream_id: 's1', is_final: false } });
    });
    const body = container.querySelectorAll('.v2-chat-body');
    const rendered = Array.from(body).map((n) => n.textContent).join(' ');
    expect(rendered).toContain('Hello');
    expect(rendered).not.toContain('<|eom|>');
  });

  it('tool events render friendly expandable traces', async () => {
    const { container } = renderV2(<Chat />);
    // Put the UI into the thinking state the way a real submit would:
    // the chip row only renders under `thinking && !streamingText`.
    const input = container.querySelector('.v2-chat-input');
    const form = container.querySelector('form.v2-chat-composer');
    await act(async () => {
      fireEvent.change(input, { target: { value: 'go' } });
      fireEvent.submit(form);
    });

    await act(async () => {
      emit({
        type: 'tool_start',
        payload: { tool: 'web_search__run', call_id: 'c1', args_preview: '{"q":"hi"}' },
      });
    });
    const texts = Array.from(container.querySelectorAll('.v2-chat-body'))
      .map((n) => n.textContent).join(' | ');
    expect(texts).toContain('using Search web');
    expect(texts).not.toContain('web_search__run');

    await act(async () => {
      emit({ type: 'tool_result', payload: { tool: 'web_search__run', call_id: 'c1', success: true, latency_ms: 17 } });
      emit({ type: 'text_response', payload: { text: 'Done.' } });
    });
    // Lane 12 rebuild: tool traces now render as ToolCallCard rows
    // attached to the assistant turn (one card per call). The legacy
    // aggregate "used N tools" summary chip + global trace toggle was
    // replaced with per-card collapse so power users can inspect each
    // call independently. Assert on the per-card surface contract:
    // friendly label + latency, no raw `provider__action` id.
    const after = Array.from(container.querySelectorAll('.v2-chat-body'))
      .map((n) => n.textContent).join(' | ');
    expect(after).toContain('Search web');
    expect(after).toContain('17ms');
    expect(after).not.toContain('web_search__run');

    // Each tool card is independently expandable via its head button.
    const toolHead = container.querySelector('.v2-tool-card__head');
    expect(toolHead).toBeTruthy();
    await act(async () => {
      fireEvent.click(toolHead);
    });
    const expanded = Array.from(container.querySelectorAll('.v2-chat-body'))
      .map((n) => n.textContent).join(' | ');
    expect(expanded).toContain('Search web');
    expect(expanded).toContain('17ms');
    // Args are pretty-printed in the details pane (a JSON-shaped
    // `args_preview` string is re-indented rather than shown as one
    // long line), so assert on the indented form.
    expect(expanded).toContain('"q": "hi"');
    expect(expanded).not.toContain('web_search__run');
  });
});
