'use strict';
const {test, beforeEach, afterEach} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {randomUUID} = require('node:crypto');
const {FeralClient, ChatTurnError, ChatTurnTimeout} =
  require(path.join(process.env.FERAL_SDK_DIST || path.join(__dirname, '../dist'), 'index.js'));
const originalWS = globalThis.WebSocket;
let sockets, onCommand, clients, autoOpen;
class Wire {
  constructor(url) {
    this.url = url; this.sid = new URL(url).searchParams.get('session_id');
    this.sent = []; this.closed = false; sockets.push(this);
    if (autoOpen) queueMicrotask(() => this.onopen?.());
  }
  send(raw) {
    const frame = JSON.parse(raw); this.sent.push(frame);
    if (frame.type === 'req') this.emit('res', {turn_contract_versions: [1], durable_receipts: true,
      whole_turn_terminal: true, session_id: this.sid}, {id: frame.id, ok: true});
    else if (frame.type === 'text_command') { this.turn = randomUUID(); onCommand(this, frame); }
  }
  emit(type, payload, fields = {}) {
    this.onmessage?.({data: JSON.stringify({type, session_id: this.sid, payload, ...fields})});
  }
  accept(frame) {
    this.emit('chat_turn_accepted', {contract_version: 1, request_id: frame.msg_id,
      turn_id: this.turn, session_id: this.sid, status: 'accepted', durable: true, replayed: false});
  }
  terminal(frame, text = '42') {
    this.emit('chat_turn_terminal', {contract_version: 1, request_id: frame.msg_id,
      turn_id: this.turn, session_id: this.sid, processing_outcome: 'completed', final_text: text,
      action_outcome: 'not_asserted', approval_request_ids: [], durable: true, replayed: false});
  }
  close() { this.closed = true; this.onclose?.({code: 1000}); }
}
const commands = wire => wire.sent.filter(frame => frame.type === 'text_command');
const client = options => { const value = new FeralClient('http://fixture', options); clients.push(value); return value; };
beforeEach(() => {
  sockets = []; clients = []; autoOpen = true; globalThis.WebSocket = Wire;
  onCommand = (wire, frame) => { wire.accept(frame); wire.terminal(frame); };
});
afterEach(() => { clients.forEach(value => value.close()); globalThis.WebSocket = originalWS; });

test('three turns share one negotiated live socket with distinct request and turn IDs', async () => {
  const sdk = client({bearerToken: 'fixture-key', sessionId: 'A'});
  const receipts = [];
  for (const text of ['one', 'two', 'three']) receipts.push(await sdk.chatTurn(text));
  assert.equal(sockets.length, 1); assert.equal(sockets[0].closed, false);
  assert.deepEqual(sockets[0].sent.map(frame => frame.type), ['auth', 'req', 'text_command', 'text_command', 'text_command']);
  assert.equal(new Set(receipts.map(receipt => receipt.request_id)).size, 3);
  assert.equal(new Set(receipts.map(receipt => receipt.turn_id)).size, 3);
  assert.ok(!sockets[0].url.includes('fixture-key'));
  sdk.close(); assert.ok(sockets[0].closed);
  assert.equal(sockets[0].onmessage, null); assert.equal(sockets[0].onclose, null);
});

test('simultaneous sessions use separate sockets; same session overlap sends zero second command', async () => {
  const sdk = client();
  onCommand = (wire, frame) => wire.accept(frame);
  const a = sdk.chatTurn('alpha', {sessionId: 'A'});
  const b = sdk.chatTurn('beta', {sessionId: 'B'});
  await new Promise(resolve => setImmediate(resolve));
  await assert.rejects(sdk.chat('overlap', {sessionId: 'A'}), error => error.code === 'session_busy');
  assert.equal(sockets.length, 2);
  for (const wire of sockets) wire.terminal(commands(wire)[0], wire.sid);
  const receipts = await Promise.all([a, b]);
  assert.deepEqual(receipts.map(receipt => receipt.final_text), ['A', 'B']);
  assert.ok(sockets.every(wire => commands(wire).length === 1 && !wire.closed));
});

test('idle close loses old context and refuses before any reconnect or user command', async () => {
  const sdk = client({sessionId: 'old'});
  await sdk.chat('establish'); sockets[0].onclose({code: 1001});
  await assert.rejects(sdk.chat('do not replay'), error => error.code === 'context_lost');
  assert.equal(sockets.length, 1); assert.equal(commands(sockets[0]).length, 1);
  assert.equal(sockets[0].onmessage, null);
  assert.equal(await sdk.chat('new task', {sessionId: 'NEW'}), '42');
  assert.equal(sockets.length, 2);
});

for (const interruption of ['timeout', 'abort', 'closeThread', 'close']) {
  test(`active ${interruption} cleans resources and old context refuses without replay`, async () => {
    const sdk = client({sessionId: 'old', chatTimeoutMs: 20});
    await sdk.chat('establish');
    onCommand = (wire, frame) => wire.accept(frame);
    const controller = new AbortController();
    const task = sdk.chatTurn('waiting', {signal: controller.signal});
    await new Promise(resolve => setImmediate(resolve));
    const check = assert.rejects(task, error => interruption === 'timeout' ? error instanceof ChatTurnTimeout :
      error.code === (interruption === 'abort' ? 'transport_cancelled' : interruption === 'close' ? 'client_closed' : 'context_lost'));
    if (interruption === 'abort') controller.abort();
    if (interruption === 'closeThread') sdk.closeThread();
    if (interruption === 'close') sdk.close();
    await check;
    assert.ok(sockets[0].closed); assert.equal(sockets[0].onmessage, null);
    await assert.rejects(sdk.chat('no retry'), error => error.code === (interruption === 'close' ? 'client_closed' : 'context_lost'));
    assert.equal(sockets.length, 1); assert.equal(commands(sockets[0]).length, 2);
    assert.ok(!sockets[0].sent.some(frame => frame.method === 'chat.abort'));
    if (interruption !== 'close') {
      onCommand = (wire, frame) => { wire.accept(frame); wire.terminal(frame); };
      assert.equal(await sdk.chat('new task', {sessionId: 'NEW'}), '42');
    }
  });
}

test('stale terminal cannot resolve later turn', async () => {
  const sdk = client();
  const first = await sdk.chatTurn('first');
  const old = commands(sockets[0])[0];
  onCommand = (wire, frame) => { wire.terminal(old, 'stale'); wire.accept(frame); wire.terminal(frame, 'correct'); };
  const next = await sdk.chatTurn('next');
  assert.equal(next.final_text, 'correct'); assert.notEqual(next.request_id, first.request_id);
  assert.equal(sockets.length, 1);
});

test('quota refuses without evicting live history; explicit close frees capacity', async () => {
  const sdk = client({maxChatThreads: 1});
  await sdk.chat('first', {sessionId: 'A'});
  await assert.rejects(sdk.chat('cannot evict A', {sessionId: 'B'}), error => error.code === 'thread_quota');
  assert.equal(sockets.length, 1); assert.equal(sockets[0].closed, false);
  await sdk.chat('still A', {sessionId: 'A'});
  sdk.closeThread('A');
  await assert.rejects(sdk.chat('no reopening', {sessionId: 'A'}), error => error.code === 'context_lost');
  assert.equal(await sdk.chat('new B', {sessionId: 'B'}), '42');
  assert.equal(sockets.length, 2);
});

for (const maximum of [0, -1, 65, true, 1.5]) {
  test(`invalid channel limit ${maximum} refuses before connection`, () => {
    assert.throws(() => client({maxChatThreads: maximum}), /maxChatThreads/); assert.deepEqual(sockets, []);
  });
}

test('identity tombstones remain bounded without reopening', async () => {
  const sdk = client();
  for (let index = 0; index < 1024; index++) sdk.lostThreads.add(`closed-${index}`);
  await assert.rejects(sdk.chat('no submission', {sessionId: 'NEW'}), error => error.code === 'thread_identity_quota');
  await assert.rejects(sdk.chat('no reopening', {sessionId: 'closed-0'}), error => error.code === 'context_lost');
  assert.deepEqual(sockets, []);
});

for (const operation of ['closeThread', 'close', 'timeout', 'abort']) {
  test(`cold connection ${operation} cleans listeners and sends zero prompts`, async () => {
    autoOpen = false;
    const sdk = client({sessionId: 'old', chatTimeoutMs: 20});
    const controller = new AbortController();
    const task = sdk.chat('do not submit', {signal: controller.signal});
    const check = assert.rejects(task, error => operation === 'timeout' ? error instanceof ChatTurnTimeout :
      error.code === (operation === 'abort' ? 'transport_cancelled' : operation === 'close' ? 'client_closed' : 'context_lost'));
    if (operation === 'closeThread') sdk.closeThread();
    if (operation === 'close') sdk.close();
    if (operation === 'abort') controller.abort();
    await check;
    assert.ok(sockets[0].closed); assert.equal(sockets[0].onopen, null);
    assert.equal(sockets[0].onmessage, null); assert.deepEqual(sockets[0].sent, []);
    await assert.rejects(sdk.chat('do not reconnect'), error => error.code === (operation === 'close' ? 'client_closed' : 'context_lost'));
    assert.equal(sockets.length, 1);
    if (operation !== 'close') {
      autoOpen = true;
      assert.equal(await sdk.chat('new deliberate task', {sessionId: 'NEW'}), '42');
    }
  });
}
