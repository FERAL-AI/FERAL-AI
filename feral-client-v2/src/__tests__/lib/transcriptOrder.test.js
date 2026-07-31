/**
 * Voice transcript ordering (operator report 2026-07-28).
 *
 * OpenAI's Realtime docs: `input_audio_transcription.completed` "runs
 * asynchronously with Response creation, so this event may come before
 * or after the Response events". Chat used to blind-append transcripts,
 * so a user transcript that lost the race rendered BELOW the assistant
 * reply that answered it.
 */
import { describe, it, expect } from 'vitest';
import {
  insertTranscriptMessage,
  transcriptRowFromPayload,
} from '../../lib/transcriptOrder';

const userRow = {
  id: 'u1', role: 'user', text: 'what is the weather',
  itemId: 'item_user', previousItemId: null, seq: 1,
};
const assistantRow = {
  id: 'a1', role: 'assistant', text: 'It is sunny',
  itemId: 'item_assistant', previousItemId: 'item_user', seq: 0,
};

describe('insertTranscriptMessage', () => {
  it('places a LATE user transcript above the reply that answered it', () => {
    // The real out-of-order case: the assistant reply arrives first and
    // declares the user item as its predecessor. The user transcript
    // arrives second, with a HIGHER seq (it was emitted later), so seq
    // alone would keep it below. The item link must win.
    let list = [];
    list = insertTranscriptMessage(list, assistantRow);
    list = insertTranscriptMessage(list, userRow);

    expect(list.map((m) => m.role)).toEqual(['user', 'assistant']);
    expect(list.map((m) => m.text)).toEqual([
      'what is the weather', 'It is sunny',
    ]);
  });

  it('keeps in-order arrivals in order', () => {
    let list = [];
    list = insertTranscriptMessage(list, userRow);
    list = insertTranscriptMessage(list, assistantRow);
    expect(list.map((m) => m.role)).toEqual(['user', 'assistant']);
  });

  it('replaces an earlier partial in place when the final arrives', () => {
    const partial = {
      id: 'p1', role: 'user', text: 'what is the', itemId: 'item_user', seq: 0,
    };
    const final = {
      id: 'p2', role: 'user', text: 'what is the weather', itemId: 'item_user', seq: 2,
    };
    let list = insertTranscriptMessage([], partial);
    list = insertTranscriptMessage(list, final);

    expect(list).toHaveLength(1);
    expect(list[0].text).toBe('what is the weather');
    // React key stays stable so the bubble does not remount.
    expect(list[0].id).toBe('p1');
  });

  it('orders by seq when the provider supplies no item ids (Gemini)', () => {
    const a = { id: 'a', role: 'user', text: 'first', itemId: null, seq: 0 };
    const b = { id: 'b', role: 'assistant', text: 'second', itemId: null, seq: 1 };
    let list = insertTranscriptMessage([], b);
    list = insertTranscriptMessage(list, a);
    expect(list.map((m) => m.text)).toEqual(['first', 'second']);
  });

  it('appends rows with no ordering metadata at all', () => {
    const existing = [{ id: 'x', role: 'assistant', text: 'older' }];
    const row = { id: 'y', role: 'user', text: 'newer' };
    expect(insertTranscriptMessage(existing, row).map((m) => m.text))
      .toEqual(['older', 'newer']);
  });

  it('leaves plain chat rows undisturbed', () => {
    const chat = [
      { id: 'c1', role: 'user', text: 'typed question' },
      { id: 'c2', role: 'assistant', text: 'typed answer' },
    ];
    const voice = { id: 'v1', role: 'user', text: 'spoken', itemId: 'i', seq: 0 };
    const out = insertTranscriptMessage(chat, voice);
    expect(out.map((m) => m.text)).toEqual([
      'typed question', 'typed answer', 'spoken',
    ]);
  });

  it('does not mutate the input array', () => {
    const list = [assistantRow];
    const out = insertTranscriptMessage(list, userRow);
    expect(list).toHaveLength(1);
    expect(out).toHaveLength(2);
  });
});

describe('transcriptRowFromPayload', () => {
  it('maps wire snake_case ordering fields onto the row', () => {
    const row = transcriptRowFromPayload({
      text: 'hello', role: 'user',
      item_id: 'i1', previous_item_id: 'i0', seq: 4,
    }, 'id1');
    expect(row).toMatchObject({
      role: 'user', text: 'hello', source: 'voice',
      itemId: 'i1', previousItemId: 'i0', seq: 4,
    });
  });

  it('strips the legacy [user] sentinel and infers the role', () => {
    const row = transcriptRowFromPayload({ text: '[user] hi there' }, 'id2');
    expect(row.role).toBe('user');
    expect(row.text).toBe('hi there');
  });

  it('returns null when there is nothing to render', () => {
    expect(transcriptRowFromPayload({ text: '' }, 'id3')).toBeNull();
  });

  it('defaults missing ordering fields to null rather than undefined', () => {
    const row = transcriptRowFromPayload({ text: 'x', role: 'assistant' }, 'id4');
    expect(row.itemId).toBeNull();
    expect(row.previousItemId).toBeNull();
    expect(row.seq).toBeNull();
  });
});
