/**
 * A chat row survives the trip through the conversation store.
 *
 * Shell autosaves the transcript through serialiseConversationMessages
 * and rehydrates it through normaliseUiMessages. The first of those
 * used to project every row down to `{id, role, content}` (with one
 * hand-written exception for `sdui`), so a turn that ran seven tools
 * was stored as bare prose and came back from GET /api/conversations/:id
 * with only ['content', 'id', 'role'] on it. The cards were on screen
 * until the next reload and gone forever after it.
 *
 * Chat.rich-turn-survives-commit.test.jsx covers the setter side (the
 * row keeps its fields when the turn commits). This file covers the
 * store side: the same fields must still be there after serialise then
 * normalise, with `text` and `content` translated and never both
 * present on one row.
 */
import { describe, it, expect } from 'vitest';
import { normaliseUiMessages, serialiseConversationMessages } from '../../shell/Shell';

const TOOLS = [
  {
    tool: 'vision__screen_capture',
    call_id: 'c1',
    skill_id: 'vision',
    endpoint_id: 'screen_capture',
    display_name: 'Capture screen',
    args_preview: '{"region":"full desktop"}',
    success: true,
    latency_ms: 412,
    result_preview: '1920 x 1200 jpeg',
  },
  {
    tool: 'browser__open',
    call_id: 'c2',
    skill_id: 'browser',
    endpoint_id: 'open',
    display_name: 'Open page',
    args_preview: '{"url":"https://example.com"}',
    success: true,
    latency_ms: 90,
    result_preview: 'ok',
  },
];

const RICH_TURN = {
  id: 'a1',
  role: 'assistant',
  text: 'Three tabs were open.',
  tools: TOOLS,
  reasoning: 'checking the open windows',
  timeline: { steps: [{ label: 'capture' }, { label: 'answer' }] },
  model: 'openai/gpt-5',
  usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
};

const USER_TURN = {
  id: 'u1',
  role: 'user',
  text: 'what is on my screen',
  attachments: [{ id: 'att-1', name: 'shot.png', mime: 'image/png', size: 1234 }],
};

describe('serialiseConversationMessages', () => {
  it('keeps every field and spells text as content', () => {
    const [row] = serialiseConversationMessages([RICH_TURN]);
    expect(row).toEqual({
      id: 'a1',
      role: 'assistant',
      content: 'Three tabs were open.',
      tools: TOOLS,
      reasoning: 'checking the open windows',
      timeline: RICH_TURN.timeline,
      model: 'openai/gpt-5',
      usage: RICH_TURN.usage,
    });
    expect(row).not.toHaveProperty('text');
  });

  it('keeps attachments on a user row', () => {
    const [row] = serialiseConversationMessages([USER_TURN]);
    expect(row.attachments).toEqual(USER_TURN.attachments);
    expect(row.content).toBe('what is on my screen');
    expect(row).not.toHaveProperty('text');
  });

  it('keeps the sdui shape the renderer reads', () => {
    const sdui = { type: 'card', children: [] };
    const [row] = serialiseConversationMessages([
      { id: 's1', role: 'assistant', type: 'sdui', sdui, screen_id: 'screen-9' },
    ]);
    expect(row.type).toBe('sdui');
    expect(row.sdui).toEqual(sdui);
    expect(row.screen_id).toBe('screen-9');
    expect(row.id).toBe('s1');
  });

  it('keeps a notice row with its tool trace', () => {
    const [row] = serialiseConversationMessages([
      { id: 'n1', role: 'assistant', type: 'notice', notice: { kind: 'error', text: 'boom' }, tools: TOOLS },
    ]);
    expect(row.type).toBe('notice');
    expect(row.notice).toEqual({ kind: 'error', text: 'boom' });
    expect(row.tools).toEqual(TOOLS);
  });

  it('accepts a store row that already carries content and no text', () => {
    const [row] = serialiseConversationMessages([
      { id: 'a2', role: 'assistant', content: 'from the store', tools: TOOLS },
    ]);
    expect(row.content).toBe('from the store');
    expect(row.tools).toEqual(TOOLS);
  });

  it('fills a missing id and role and drops non-object entries', () => {
    const rows = serialiseConversationMessages([null, 'junk', { text: 'hi' }]);
    expect(rows).toHaveLength(1);
    expect(rows[0].role).toBe('assistant');
    expect(rows[0].id).toMatch(/^m_/);
    expect(rows[0].content).toBe('hi');
  });
});

describe('serialise then normalise', () => {
  it('round-trips a rich assistant turn field for field', () => {
    const saved = serialiseConversationMessages([USER_TURN, RICH_TURN]);
    // The store is a json.dumps / json.loads pair; mirror that so a
    // field that only survives by object identity would be caught.
    const reloaded = normaliseUiMessages(JSON.parse(JSON.stringify(saved)));

    expect(reloaded).toHaveLength(2);
    const [user, assistant] = reloaded;

    expect(user).toEqual(USER_TURN);
    expect(user).not.toHaveProperty('content');

    expect(assistant).toEqual(RICH_TURN);
    expect(assistant).not.toHaveProperty('content');
    expect(assistant.tools).toHaveLength(2);
    expect(assistant.tools[0].display_name).toBe('Capture screen');
  });

  it('round-trips a tool-only turn instead of dropping it for having no text', () => {
    const saved = serialiseConversationMessages([
      { id: 'a3', role: 'assistant', text: '', tools: TOOLS },
    ]);
    expect(saved[0].content).toBe('');
    const reloaded = normaliseUiMessages(JSON.parse(JSON.stringify(saved)));
    expect(reloaded).toHaveLength(1);
    expect(reloaded[0].id).toBe('a3');
    expect(reloaded[0].tools).toEqual(TOOLS);
  });

  it('round-trips an sdui row', () => {
    const sdui = { type: 'card', children: [] };
    const saved = serialiseConversationMessages([
      { id: 's1', role: 'assistant', type: 'sdui', sdui, screen_id: 'screen-9' },
    ]);
    const [row] = normaliseUiMessages(JSON.parse(JSON.stringify(saved)));
    expect(row.type).toBe('sdui');
    expect(row.sdui).toEqual(sdui);
    expect(row.screen_id).toBe('screen-9');
  });

  it('is stable across a second save', () => {
    const once = serialiseConversationMessages([RICH_TURN]);
    const twice = serialiseConversationMessages(normaliseUiMessages(once));
    expect(twice).toEqual(once);
  });
});
