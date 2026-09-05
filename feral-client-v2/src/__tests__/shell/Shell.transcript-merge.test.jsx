/**
 * Boot hydration must not delete history it did not write.
 *
 * The primary-session transcript (/api/sessions/primary/transcript) and
 * the conversation store (/api/conversations/*) overlap, so the client
 * folds one into the other. The old fold deduped on a `role|text`
 * signature held in ONE growing set, which meant the transcript was
 * deduped against ITSELF as well as against what was already loaded.
 *
 * Repetition is normal in a real thread, so this silently rewrote it. An
 * operator who sent the same message three times got one row back, and
 * two identical assistant replies collapsed into one. That is why a
 * thread appeared to hold two near-duplicate answers when what actually
 * happened was three sends with a reply deleted by the client on the way
 * to the screen.
 */
import { describe, it, expect } from 'vitest';
import { mergeTranscriptIntoMessages } from '../../shell/Shell';

describe('mergeTranscriptIntoMessages', () => {
  it('keeps both of two identical user rows', () => {
    // The operator really did send it twice. ts_ms is the row's position
    // in the transcript (1-based), so the two rows are distinguishable
    // even though their text is byte-identical.
    const merged = mergeTranscriptIntoMessages([], [
      { role: 'user', text: 'can you flash the light screen?', ts_ms: 1 },
      { role: 'user', text: 'can you flash the light screen?', ts_ms: 2 },
    ]);
    expect(merged).toHaveLength(2);
    expect(merged.map((m) => m.text)).toEqual([
      'can you flash the light screen?',
      'can you flash the light screen?',
    ]);
    expect(new Set(merged.map((m) => m.id)).size).toBe(2);
  });

  it('keeps two identical assistant replies', () => {
    const merged = mergeTranscriptIntoMessages([], [
      { role: 'assistant', text: 'Which room did you mean?', ts_ms: 4 },
      { role: 'assistant', text: 'Which room did you mean?', ts_ms: 5 },
    ]);
    expect(merged).toHaveLength(2);
  });

  it('does not re-add a row the conversation store already loaded', () => {
    const prev = [{ id: 'c1', role: 'user', text: 'hello' }];
    const merged = mergeTranscriptIntoMessages(prev, [
      { role: 'user', text: 'hello', ts_ms: 1 },
    ]);
    expect(merged).toBe(prev);
  });

  it('adds only the copies prev does not already hold', () => {
    // prev holds one "hello"; the transcript holds two. Exactly one is
    // new. The old set-based fold added none, which is the bug.
    const prev = [{ id: 'c1', role: 'user', text: 'hello' }];
    const merged = mergeTranscriptIntoMessages(prev, [
      { role: 'user', text: 'hello', ts_ms: 1 },
      { role: 'user', text: 'hello', ts_ms: 2 },
    ]);
    expect(merged).toHaveLength(2);
  });

  it('is idempotent when hydration runs twice', () => {
    const rows = [
      { role: 'user', text: 'hello', ts_ms: 1 },
      { role: 'user', text: 'hello', ts_ms: 2 },
    ];
    const once = mergeTranscriptIntoMessages([], rows);
    const twice = mergeTranscriptIntoMessages(once, rows);
    expect(twice).toHaveLength(2);
    expect(twice).toBe(once);
  });

  it('preserves transcript order and appends after prev', () => {
    const prev = [{ id: 'greet', role: 'assistant', text: 'Hi.' }];
    const merged = mergeTranscriptIntoMessages(prev, [
      { role: 'user', text: 'one', ts_ms: 1 },
      { role: 'assistant', text: 'two', ts_ms: 2 },
      { role: 'user', text: 'three', ts_ms: 3 },
    ]);
    expect(merged.map((m) => m.text)).toEqual(['Hi.', 'one', 'two', 'three']);
  });

  it('drops rows with no role or no text rather than rendering blanks', () => {
    const merged = mergeTranscriptIntoMessages([], [
      { role: 'user', text: '   ', ts_ms: 1 },
      { role: '', text: 'orphan', ts_ms: 2 },
      { role: 'user', text: 'real', ts_ms: 3 },
    ]);
    expect(merged.map((m) => m.text)).toEqual(['real']);
  });
});

