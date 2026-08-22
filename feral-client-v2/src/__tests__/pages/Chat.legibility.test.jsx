/**
 * Two things a reader has to be able to do without being told.
 *
 *  1. Tell who said what. The two roles used to share one <Orb>,
 *     differing only by its `mode` — two small blue discs — with no name
 *     anywhere in the row. The approved mockup names both speakers and
 *     gives FERAL the branded mark while the user gets a plain disc.
 *  2. Find the thread list and the save control. Both were bare 13px
 *     icons in the pane header with no visible word between them, which
 *     is why the report was that they "sit as two small icons ... and
 *     users do not notice they exist".
 *
 * These are rendering contracts, so they are asserted on the DOM the
 * page produces, not on the stylesheet.
 */
import React from 'react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, cleanup, fireEvent } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Chat, { deriveThreadTitle } from '../../pages/Chat';

const listeners = new Set();

vi.mock('../../hooks/useFeralSocket', () => {
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
    setSession: () => {},
    send: () => true,
    sendOrFail: () => ({ ok: true }),
  };
  return {
    useFeralSocket: () => fakeSocket,
    sendUiEvent: () => {},
  };
});

vi.mock('../../hooks/useConnectionStatus', () => ({
  useConnectionStatus: () => ({ state: 'open' }),
}));

function emit(msg) {
  listeners.forEach((fn) => fn(msg));
}

afterEach(() => {
  listeners.clear();
  cleanup();
});

async function withOneTurn() {
  const { container } = renderV2(<Chat />);
  const input = container.querySelector('.v2-chat-input');
  const form = container.querySelector('form.v2-chat-composer');
  await act(async () => {
    fireEvent.change(input, { target: { value: 'what is on my screen' } });
    fireEvent.submit(form);
  });
  await act(async () => {
    emit({ type: 'text_response', payload: { text: 'Three tabs were open.' } });
  });
  return container;
}

const rowFor = (container, text) => Array.from(
  container.querySelectorAll('.v2-chat-row'),
).find((r) => r.textContent.includes(text));

describe('a glance separates the two speakers', () => {
  it('names both speakers in the row', async () => {
    const container = await withOneTurn();

    const userRow = rowFor(container, 'what is on my screen');
    const botRow = rowFor(container, 'Three tabs were open.');

    expect(userRow.querySelector('.v2-chat-who').textContent).toBe('You');
    expect(botRow.querySelector('.v2-chat-who').textContent).toBe('FERAL');
  });

  it('draws the user with a plain disc and never with FERAL\'s Orb', async () => {
    const container = await withOneTurn();

    const userRow = rowFor(container, 'what is on my screen');
    const botRow = rowFor(container, 'Three tabs were open.');

    // The Orb's own contract: it is the persona anchor, one entity. It
    // belongs to FERAL's rows only.
    expect(userRow.querySelector('.v2-orb')).toBeNull();
    expect(userRow.querySelector('.v2-chat-avatar')).not.toBeNull();
    expect(botRow.querySelector('.v2-orb')).not.toBeNull();
    expect(botRow.querySelector('.v2-chat-avatar')).toBeNull();
  });

  it('gives the user turn a bubble and the assistant turn bare prose', async () => {
    const container = await withOneTurn();

    expect(rowFor(container, 'what is on my screen').querySelector('.v2-chat-bubble'))
      .not.toBeNull();
    expect(rowFor(container, 'Three tabs were open.').querySelector('.v2-chat-bubble'))
      .toBeNull();
  });
});

describe('the thread list and the save control announce themselves', () => {
  it('shows the open thread by name, and the name is the switcher', async () => {
    const container = await withOneTurn();

    const picker = container.querySelector('[data-testid="chat-thread-picker"]');
    expect(picker).not.toBeNull();
    expect(picker.textContent).toContain('what is on my screen');
    // A visible word for what it opens, not an unlabelled glyph.
    expect(picker.textContent).toContain('Threads');

    await act(async () => { fireEvent.click(picker); });
    expect(container.querySelector('.v2-chat-pane--threads')).not.toBeNull();
  });

  it('labels the save control with a word', async () => {
    const container = await withOneTurn();

    const save = container.querySelector('[data-testid="chat-save-toggle"]');
    expect(save).not.toBeNull();
    expect(save.textContent).toContain('Save');

    await act(async () => { fireEvent.click(save); });
    const pane = container.querySelector('.v2-chat-pane');
    expect(pane).not.toBeNull();
    expect(pane.textContent).toContain('Saved points');
  });

  it('names an empty thread the same way the threads list does', () => {
    expect(deriveThreadTitle([])).toBe('New conversation');
    expect(deriveThreadTitle([{ role: 'assistant', text: 'hello' }]))
      .toBe('New conversation');
    expect(deriveThreadTitle([{ role: 'user', text: '  open my mail  ' }]))
      .toBe('open my mail');
  });
});
