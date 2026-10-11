import React from 'react';
import { act, cleanup, screen, waitFor } from '@testing-library/react';
import { Route, Routes } from 'react-router-dom';
import { DEFAULT_FETCH_BODY, renderV2 } from '../_helpers/renderV2';
import Shell from '../../shell/Shell';

vi.mock('../../hooks/useMachineVitals', () => ({ useMachineVitals: () => ({
  reachable: true, running: 0, needs: 0, devices: 0, episodes: 0,
  costKnown: false, autonomy: '',
}) }));

const ACTIVE_KEY = 'feral_v2_active_conversation';

function pending() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function draw(responder, strict = false) {
  const tree = <Routes><Route element={<Shell />}>
    <Route path="/chat" element={<p>Conversation space</p>} />
  </Route></Routes>;
  return renderV2(strict ? <React.StrictMode>{tree}</React.StrictMode> : tree,
    { route: '/chat', fetch: responder });
}

beforeEach(() => localStorage.setItem(ACTIVE_KEY, 'remembered-thread'));
afterEach(() => { cleanup(); localStorage.removeItem(ACTIVE_KEY); vi.unstubAllGlobals(); });

it.each([
  ['/api/sessions/primary', { session_id: 'primary-owner' }],
  ['/api/conversations/active/thread', { id: 'retired-thread', messages: [] }],
  ['/api/sessions/primary/transcript', { messages: [{ role: 'assistant', content: 'Old result' }] }],
  ['/api/conversations/new', { id: 'retired-created-thread' }],
])('abandons hydration at %s without continuing requests or changing the active thread', async (path, body) => {
  const delayed = pending();
  const requests = [];
  const view = draw((url) => {
    requests.push(url);
    if (url.includes(path)) return delayed.promise;
    return DEFAULT_FETCH_BODY;
  });
  await waitFor(() => expect(requests.some((url) => url.includes(path))).toBe(true));
  view.unmount();
  const bootRequests = () => requests.filter((url) => /\/api\/(sessions\/primary|conversations\/(active\/thread|new))/.test(url));
  const before = bootRequests();
  await act(async () => { delayed.resolve(body); await delayed.promise; });
  expect(bootRequests()).toEqual(before);
  expect(localStorage.getItem(ACTIVE_KEY)).toBe('remembered-thread');
});

it('hydrates the current StrictMode lifetime and rejects the retired lifetime response', async () => {
  const retired = pending();
  let primaryRequests = 0;
  const view = draw((url) => {
    if (url.endsWith('/api/sessions/primary')) {
      primaryRequests += 1;
      return primaryRequests === 1 ? retired.promise : { session_id: 'current-primary' };
    }
    if (url.includes('/api/conversations/active/thread')) return { id: 'current-thread', messages: [] };
    return DEFAULT_FETCH_BODY;
  }, true);
  await waitFor(() => expect(localStorage.getItem(ACTIVE_KEY)).toBe('current-thread'));
  expect(screen.getByRole('main')).toHaveTextContent('Conversation space');
  view.unmount();
  await act(async () => { retired.resolve({ session_id: 'retired-primary' }); await retired.promise; });
  expect(localStorage.getItem(ACTIVE_KEY)).toBe('current-thread');
});
