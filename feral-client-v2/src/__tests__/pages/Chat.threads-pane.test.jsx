/**
 * Threads pane: search, rename, pin, pagination, preview/message_count.
 *
 * The fetch mock here deliberately implements `clone()` and `text()`,
 * unlike the shared `renderV2` stub. `apiFetch` inspects even a 200
 * body and throws an `ApiError` when it carries a non-empty `error`
 * string, and that inspection is guarded on `typeof response.clone ===
 * 'function'`. A `{ok, json}` stub skips the whole branch, so a test
 * built on one cannot observe the toast-on-success-envelope class of
 * bug at all. The rename and pin routes both answer 200 with
 * `{"error": "Not found"}` for a thread deleted in another tab
 * (verified against the real routes), so that branch is the one under
 * test.
 */
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import Chat from '../../pages/Chat';

// Real route payloads, captured from the live store behind the real
// FastAPI app (see feral-core/tests/test_conversation_threads_w3.py).
function threadRow(i, over = {}) {
  return {
    id: `thread-${String(i).padStart(2, '0')}`,
    title: `question ${i} about pytest and sqlite`,
    preview: `question ${i} about pytest and sqlite`,
    message_count: 2,
    created_at: 1787290000 + i,
    updated_at: 1787290000 + i,
    pinned: false,
    title_custom: false,
  };
}

let ALL;
let calls;
let overrides;

/** Title cell only. The preview repeats the title, so a bare getByText matches twice. */
function titleCells(text) {
  return screen.getAllByRole('button', { name: text })
    .filter((el) => el.classList.contains('v2-flow-card-title'));
}

function respond(url, init) {
  const [pathPart, queryPart = ''] = String(url).split('?');
  const params = new URLSearchParams(queryPart);
  const key = `${(init?.method || 'GET').toUpperCase()} ${pathPart.replace(/^.*\/api/, '/api')}`;
  calls.push({ key, url: String(url), body: init?.body ? JSON.parse(init.body) : null });

  if (overrides[key]) return overrides[key];

  if (pathPart.endsWith('/api/conversations')) {
    const q = params.get('q') || '';
    const limit = Number(params.get('limit') || 25);
    const offset = Number(params.get('offset') || 0);
    const matched = q
      ? ALL.filter((t) => t.title.includes(q) || t.preview.includes(q))
      : ALL;
    const sorted = [...matched].sort((a, b) => (b.pinned - a.pinned) || (b.updated_at - a.updated_at));
    const items = sorted.slice(offset, offset + limit);
    return {
      conversations: items,
      total: matched.length,
      limit,
      offset,
      has_more: offset + items.length < matched.length,
      query: q,
    };
  }
  const idMatch = pathPart.match(/\/api\/conversations\/([^/]+)\/(rename|pin)$/);
  if (idMatch) {
    // Mutate the fixture the way the real store does, so the refresh
    // the pane fires after the mutation reads back the new state.
    const row = ALL.find((t) => t.id === idMatch[1]);
    const sent = JSON.parse(init.body);
    if (idMatch[2] === 'rename') {
      if (row) { row.title = sent.title; row.title_custom = true; }
      return { ok: true, id: idMatch[1], title: sent.title, title_custom: true };
    }
    if (row) row.pinned = !!sent.pinned;
    return { ok: true, id: idMatch[1], pinned: !!sent.pinned };
  }
  if (pathPart.includes('/api/conversations/active/thread')) {
    return { id: 'thread-00', title: 'q', messages: [] };
  }
  return { ok: true, items: [], results: [] };
}

function installFetch() {
  vi.stubGlobal('fetch', vi.fn((input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const body = respond(url, init);
    const make = () => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: new Map(),
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body)),
      clone: make,
    });
    return Promise.resolve(make());
  }));
}

class StubWS {
  constructor() {
    this.readyState = 1;
    this.send = vi.fn();
    this.close = vi.fn();
    this.addEventListener = vi.fn();
    this.removeEventListener = vi.fn();
  }
}

function openPane() {
  const utils = render(<MemoryRouter initialEntries={['/chat']}><Chat /></MemoryRouter>);
  fireEvent.click(screen.getByTitle('Threads'));
  return utils;
}

beforeEach(() => {
  ALL = Array.from({ length: 61 }, (_, i) => threadRow(i));
  calls = [];
  overrides = {};
  installFetch();
  vi.stubGlobal('WebSocket', StubWS);
  if (!window.matchMedia) {
    window.matchMedia = vi.fn().mockImplementation((q) => ({
      matches: false, media: q, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
  }
});

describe('threads pane paging', () => {
  it('asks for an explicit page instead of fetching with no limit', async () => {
    openPane();
    await waitFor(() => expect(calls.some((c) => c.key === 'GET /api/conversations')).toBe(true));
    const list = calls.find((c) => c.key === 'GET /api/conversations');
    const params = new URLSearchParams(list.url.split('?')[1]);
    expect(params.get('limit')).toBe('25');
    expect(params.get('offset')).toBe('0');
  });

  it('renders the real total, not just what fit on one page', async () => {
    openPane();
    expect(await screen.findByTestId('thread-count')).toHaveTextContent('25 of 61');
  });

  it('reaches thread 51, which was previously unreachable', async () => {
    openPane();
    await screen.findByTestId('threads-load-more');

    fireEvent.click(screen.getByTestId('threads-load-more'));
    await waitFor(() => expect(screen.getByTestId('thread-count')).toHaveTextContent('50 of 61'));
    const second = calls.filter((c) => c.key === 'GET /api/conversations').at(-1);
    expect(new URLSearchParams(second.url.split('?')[1]).get('offset')).toBe('25');

    fireEvent.click(screen.getByTestId('threads-load-more'));
    await waitFor(() => expect(screen.getByTestId('thread-count')).toHaveTextContent('61 of 61'));
    expect(screen.queryByTestId('threads-load-more')).toBeNull();
    // thread-10 is the 51st row in updated_at DESC order.
    expect(titleCells('question 10 about pytest and sqlite')).toHaveLength(1);
  });
});

describe('threads pane rows', () => {
  it('renders preview and message_count, which the route already sent', async () => {
    openPane();
    await screen.findByTestId('thread-count');
    // preview is rendered in its own element, separate from the title.
    expect(document.querySelectorAll('.v2-thread-preview').length).toBeGreaterThan(0);
    expect(screen.getAllByText('2 messages').length).toBe(25);
  });
});

describe('threads pane search', () => {
  it('debounces to one request and passes q through', async () => {
    openPane();
    await screen.findByTestId('thread-count');
    const before = calls.filter((c) => c.key === 'GET /api/conversations').length;

    const box = screen.getByLabelText('Search threads');
    fireEvent.change(box, { target: { value: 'q' } });
    fireEvent.change(box, { target: { value: 'qu' } });
    fireEvent.change(box, { target: { value: 'question 42' } });

    await waitFor(() => expect(screen.getByTestId('thread-count')).toHaveTextContent('1 match'));
    const listCalls = calls.filter((c) => c.key === 'GET /api/conversations');
    expect(listCalls.length).toBe(before + 1);
    expect(new URLSearchParams(listCalls.at(-1).url.split('?')[1]).get('q')).toBe('question 42');
  });

  it('says so when nothing matches instead of showing an empty list', async () => {
    openPane();
    await screen.findByTestId('thread-count');
    fireEvent.change(screen.getByLabelText('Search threads'), { target: { value: 'zzzz' } });
    expect(await screen.findByText('No threads match "zzzz"')).toBeInTheDocument();
  });
});

describe('threads pane rename', () => {
  it('posts to /rename, the only route that makes a title sticky', async () => {
    openPane();
    await screen.findByTestId('thread-count');

    fireEvent.click(screen.getAllByLabelText('Rename thread')[0]);
    const input = screen.getByLabelText('Thread title');
    fireEvent.change(input, { target: { value: 'Renamed by hand' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => expect(screen.getByText('Renamed by hand')).toBeInTheDocument());
    const renameCall = calls.find((c) => c.key.endsWith('/rename'));
    expect(renameCall.key).toBe('POST /api/conversations/thread-60/rename');
    expect(renameCall.body).toEqual({ title: 'Renamed by hand' });
    // Never through /save, whose title the store treats as derived.
    expect(calls.some((c) => c.key === 'POST /api/conversations/save' && c.body?.title === 'Renamed by hand')).toBe(false);
  });

  it('cancels on Escape without calling the route', async () => {
    openPane();
    await screen.findByTestId('thread-count');
    fireEvent.click(screen.getAllByLabelText('Rename thread')[0]);
    const input = screen.getByLabelText('Thread title');
    fireEvent.change(input, { target: { value: 'nope' } });
    fireEvent.keyDown(input, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByLabelText('Thread title')).toBeNull());
    expect(calls.some((c) => c.key.endsWith('/rename'))).toBe(false);
  });

  it('shows the error envelope inline rather than firing a global toast', async () => {
    openPane();
    await screen.findByTestId('thread-count');
    overrides['POST /api/conversations/thread-60/rename'] = { error: 'Not found' };

    fireEvent.click(screen.getAllByLabelText('Rename thread')[0]);
    fireEvent.change(screen.getByLabelText('Thread title'), { target: { value: 'x' } });
    fireEvent.keyDown(screen.getByLabelText('Thread title'), { key: 'Enter' });

    expect(await screen.findByText('Not found')).toBeInTheDocument();
    // apiFetch's global toast host must not have been used.
    expect(document.querySelector('[data-testid="error-toast"]')).toBeNull();
  });
});

describe('threads pane pin', () => {
  it('posts the toggled value and re-reads the list', async () => {
    openPane();
    await screen.findByTestId('thread-count');

    fireEvent.click(screen.getAllByLabelText('Pin thread')[0]);
    await waitFor(() => expect(calls.some((c) => c.key.endsWith('/pin'))).toBe(true));
    const pinCall = calls.find((c) => c.key.endsWith('/pin'));
    expect(pinCall.key).toBe('POST /api/conversations/thread-60/pin');
    expect(pinCall.body).toEqual({ pinned: true });
    await waitFor(() => expect(screen.getByLabelText('Unpin thread')).toBeInTheDocument());
  });

  it('reverts the optimistic pin when the route refuses', async () => {
    openPane();
    await screen.findByTestId('thread-count');
    overrides['POST /api/conversations/thread-60/pin'] = { error: 'Not found' };

    fireEvent.click(screen.getAllByLabelText('Pin thread')[0]);
    expect(await screen.findByText('Not found')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByLabelText('Unpin thread')).toBeNull());
  });

  it('groups pinned threads above the rest', async () => {
    ALL[3].pinned = true;
    openPane();
    expect(await screen.findByText('Pinned')).toBeInTheDocument();
    expect(screen.getByText('Everything else')).toBeInTheDocument();
    const pinnedList = screen.getByText('Pinned').nextElementSibling;
    expect(within(pinnedList).getAllByRole('listitem')).toHaveLength(1);
    expect(within(pinnedList).getByRole('button', { name: 'question 3 about pytest and sqlite' })).toBeInTheDocument();
  });
});
