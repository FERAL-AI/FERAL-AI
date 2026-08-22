import { describe, it, expect } from 'vitest';
import { fireEvent } from '@testing-library/react';
import { renderV2, DEFAULT_FETCH_BODY } from '../_helpers/renderV2';
import App from '../../App';

describe('Chat persistence', () => {
  it('rehydrates the active thread and keeps transcript after route changes', async () => {
    const fetchResponder = (url, init) => {
      if (url.includes('/api/conversations/active/thread')) {
        return {
          id: 'thread-1',
          messages: [
            { id: 'u1', role: 'user', content: 'persist this message' },
            { id: 'a1', role: 'assistant', content: 'restored reply' },
          ],
        };
      }
      if (url.includes('/api/consciousness/state')) return { entities: [] };
      if (url.includes('/api/conversations/save')) return { ok: true };
      if (url.includes('/api/llm/providers')) return { providers: [] };
      if (url.includes('/api/llm/presets')) return { presets: [] };
      if (url.includes('/api/llm/health')) return {};
      if (url.includes('/api/llm/status')) return { available: true, provider: 'openai', model: 'gpt-5.5' };
      if (url.includes('/api/config')) return { version: '2026.5.2', features: {} };
      return DEFAULT_FETCH_BODY;
    };

    const { findByText, findAllByText, getByRole } = renderV2(<App />, {
      route: '/chat',
      fetch: fetchResponder,
    });

    // Two matches on purpose: the transcript bubble, and the thread
    // picker in the pane header, which names the open thread after its
    // first user message. Assert the transcript one specifically.
    const inTranscript = async () => {
      const hits = await findAllByText('persist this message');
      return hits.filter((el) => el.closest('.v2-chat-log'));
    };

    expect(await inTranscript()).toHaveLength(1);

    fireEvent.click(getByRole('link', { name: /Settings/i }));
    await findByText('Settings');

    fireEvent.click(getByRole('link', { name: /^Chat$/i }));
    expect(await inTranscript()).toHaveLength(1);
  });
});
