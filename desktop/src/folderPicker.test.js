import test from 'node:test';
import assert from 'node:assert/strict';
import { registerFolderPicker } from './folderPicker.js';

test('folder chooser rejects other origins and windows and replies to the exact frame', async () => {
  let handler;
  const replies = [];
  let calls = 0;
  const frame = { src: 'http://127.0.0.1:9090/', isConnected: true,
    contentWindow: { postMessage: (...args) => replies.push(args) } };
  const target = { addEventListener: (_, fn) => { handler = fn; }, removeEventListener() {} };
  registerFolderPicker(async () => { calls++; return '/tmp/project with spaces'; }, () => frame, target);
  const event = { origin: 'http://127.0.0.1:9090', source: frame.contentWindow,
    data: { type: 'feral:pick-working-directory', requestId: 'request-1' } };
  await handler({ ...event, origin: 'https://evil.invalid' });
  await handler({ ...event, source: {} });
  await handler({ ...event, data: { ...event.data, requestId: {} } });
  assert.equal(calls, 0);
  await handler(event);
  assert.equal(calls, 1);
  assert.deepEqual(replies, [[{ type: 'feral:working-directory-picked', requestId: 'request-1', path: '/tmp/project with spaces', error: null }, event.origin]]);
});

test('a removed frame never receives a delayed native folder response', async () => {
  let handler;
  let resolve;
  const frame = { src: 'http://localhost:9090', isConnected: true, contentWindow: { postMessage: () => assert.fail('stale frame') } };
  const target = { addEventListener: (_, fn) => { handler = fn; }, removeEventListener() {} };
  registerFolderPicker(() => new Promise(r => { resolve = r; }), () => frame, target);
  const pending = handler({ origin: 'http://localhost:9090', source: frame.contentWindow, data: { type: 'feral:pick-working-directory', requestId: 'a' } });
  frame.isConnected = false;
  resolve('/tmp/project');
  await pending;
});
