'use strict';
const {test, beforeEach, afterEach} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {randomUUID} = require('node:crypto');
const {FeralClient, ChatTurnError, ChatTurnTimeout, FeralHTTPError, definePlugin, FeralNode} =
  require(path.join(process.env.FERAL_SDK_DIST || path.join(__dirname, '../dist'), 'index.js'));
const originalFetch = globalThis.fetch;
const originalWS = globalThis.WebSocket;
let sockets;
let onCommand;
let onCapability;

class Wire {
  constructor(url) {
    this.url = url; this.sid = new URL(url).searchParams.get('session_id');
    this.turn = randomUUID(); this.sent = []; this.closed = false;
    sockets.push(this);
    queueMicrotask(() => this.onopen?.());
  }
  send(text) {
    const frame = JSON.parse(text); this.sent.push(frame);
    if (frame.type === 'req') {
      if (onCapability) onCapability(this, frame);
      else this.emit('res', {turn_contract_versions: [1], durable_receipts: true,
        whole_turn_terminal: true, session_id: this.sid}, {id: frame.id, ok: true});
    } else if (frame.type === 'text_command') onCommand(this, frame);
  }
  emit(type, payload = {}, fields = {}) {
    this.onmessage?.({data: JSON.stringify({type, session_id: this.sid, payload, ...fields})});
  }
  accept(command, changes = {}) {
    this.emit('chat_turn_accepted', {contract_version: 1, request_id: command.msg_id,
      turn_id: this.turn, session_id: this.sid, status: 'accepted', durable: true, replayed: false, ...changes});
  }
  terminal(command, changes = {}) {
    this.emit('chat_turn_terminal', {contract_version: 1, request_id: command.msg_id,
      turn_id: this.turn, session_id: this.sid, processing_outcome: 'completed', final_text: '42',
      action_outcome: 'not_asserted', approval_request_ids: [], durable: true, replayed: false, ...changes});
  }
  close() { this.closed = true; this.onclose?.({code: 1000}); }
}
beforeEach(() => {
  sockets = []; onCapability = undefined;
  onCommand = (wire, command) => {
    wire.accept(command);
    wire.emit('text_response', {text: 'Purchase approved?'});
    wire.emit('stream_delta', {delta: 'model-round prose', is_final: true});
    wire.terminal(command);
  };
  globalThis.WebSocket = Wire;
});
afterEach(() => { globalThis.WebSocket = originalWS; globalThis.fetch = originalFetch; });
const commands = (wire) => wire.sent.filter(frame => frame.type === 'text_command');

test('public exports, auth-first negotiation, proxy/session and exact terminal', async () => {
  assert.equal(typeof definePlugin, 'function'); assert.equal(typeof FeralNode, 'function');
  const client = new FeralClient('https://fixture.invalid/proxy', {bearerToken: 'fixture-secret'});
  assert.equal(await client.chat('31 + 11', {sessionId: 'project/+&二'}), '42');
  const wire = sockets[0];
  assert.equal(new URL(wire.url).pathname, '/proxy/v1/session');
  assert.equal(new URL(wire.url).searchParams.get('session_id'), 'project/+&二');
  assert.ok(!wire.url.includes('fixture-secret'));
  assert.deepEqual(wire.sent.map(frame => frame.type), ['auth', 'req', 'text_command']);
  assert.deepEqual(wire.sent[0], {type: 'auth', token: 'fixture-secret'});
  assert.deepEqual(commands(wire)[0].payload, {text: '31 + 11', turn_contract_version: 1});
  assert.ok(wire.closed);
});

for (const outcome of ['awaiting_approval', 'failed', 'cancelled', 'outcome_unknown', 'unavailable', 'refused', 'budget_exceeded']) {
  test(`typed ${outcome} receipt is not convenience chat success`, async () => {
    onCommand = (wire, command) => {
      wire.accept(command);
      wire.terminal(command, {processing_outcome: outcome, approval_request_ids: ['review-fixture']});
    };
    const client = new FeralClient();
    const receipt = await client.chatTurn('fixture');
    assert.equal(receipt.processing_outcome, outcome);
    assert.deepEqual(receipt.approval_request_ids, ['review-fixture']);
    await assert.rejects(client.chat('fixture'), error => error instanceof ChatTurnError
      && error.code === outcome && error.receipt.processing_outcome === outcome);
    assert.equal(sockets.length, 2); // Two explicit caller invocations, never an SDK retry.
  });
}

for (const mode of ['unknown_method', 'greeting_only', 'wrong_session', 'missing_whole_turn', 'malformed']) {
  test(`legacy ${mode} sends zero user commands`, async () => {
    onCapability = (wire, request) => {
      if (mode === 'unknown_method') wire.emit('res', {}, {id: request.id, ok: false, error: {code: 'METHOD_NOT_FOUND'}});
      else if (mode === 'greeting_only') wire.emit('text_response', {text: 'How can I help?'});
      else {
        const payload = {turn_contract_versions: [1], durable_receipts: true, whole_turn_terminal: true, session_id: wire.sid};
        if (mode === 'wrong_session') payload.session_id = 'other-thread';
        if (mode === 'missing_whole_turn') delete payload.whole_turn_terminal;
        if (mode === 'malformed') payload.turn_contract_versions = [true];
        wire.emit('res', payload, {id: request.id, ok: true});
      }
    };
    await assert.rejects(new FeralClient('http://fixture', {chatTimeoutMs: 25}).chat('effectful task'), ChatTurnError);
    assert.equal(commands(sockets[0]).length, 0);
    assert.ok(sockets[0].closed);
  });
}

for (const [changes, code] of [
  [{turn_id: randomUUID()}, 'uncorrelated_terminal'], [{session_id: 'wrong'}, 'invalid_turn_receipt'],
  [{contract_version: true}, 'invalid_turn_receipt'], [{durable: false}, 'invalid_turn_receipt'],
  [{replayed: null}, 'invalid_turn_receipt'], [{processing_outcome: 'success'}, 'invalid_turn_outcome'],
  [{approval_request_ids: [null]}, 'invalid_turn_receipt'], [{action_outcome: 'verified'}, 'invalid_turn_receipt'],
]) {
  test(`invalid terminal ${JSON.stringify(changes)} rejects`, async () => {
    onCommand = (wire, command) => { wire.accept(command); wire.terminal(command, changes); };
    await assert.rejects(new FeralClient().chat('fixture'), error => error.code === code);
    assert.equal(commands(sockets[0]).length, 1);
  });
}

test('terminal before acceptance rejects', async () => {
  onCommand = (wire, command) => wire.terminal(command);
  await assert.rejects(new FeralClient().chat('fixture'), error => error.code === 'uncorrelated_terminal');
});

for (const mode of ['partial', 'mismatched_request', 'no_terminal']) {
  test(`whole deadline with ${mode} never returns prose as success`, async () => {
    onCommand = (wire, command) => {
      wire.accept(command);
      if (mode === 'partial') {
        wire.emit('stream_delta', {delta: 'partial', is_final: true});
        wire.emit('text_response', {text: 'Approval notification'});
      } else if (mode === 'mismatched_request') wire.terminal(command, {request_id: randomUUID()});
    };
    await assert.rejects(new FeralClient('http://fixture', {chatTimeoutMs: 25}).chat('fixture'), ChatTurnTimeout);
    assert.equal(commands(sockets[0]).length, 1);
  });
}

for (const [raw, code] of [['not-json-private', 'invalid_json'], ['[]', 'invalid_frame'],
  ['{"type":"event","payload":[]}', 'invalid_payload']]) {
  test(`malformed ${code} is a redacted error`, async () => {
    onCommand = wire => wire.onmessage({data: raw});
    await assert.rejects(new FeralClient('http://fixture', {bearerToken: 'fixture-secret'}).chat('private prompt'),
      error => error.code === code && !error.message.includes('private') && !error.message.includes('fixture-secret'));
  });
}

for (const [closeCode, expected] of [[4001, 'unauthorized'], [1000, 'connection_closed']]) {
  test(`close ${closeCode} before negotiation is not success`, async () => {
    onCapability = wire => wire.onclose({code: closeCode, reason: 'fixture-secret'});
    await assert.rejects(new FeralClient().chat('fixture'), error => error.code === expected);
    assert.equal(commands(sockets[0]).length, 0);
  });
}

test('client cancellation, busy identity and close never send server abort', async () => {
  onCommand = (wire, command) => wire.accept(command);
  const client = new FeralClient();
  const controller = new AbortController();
  const first = client.chat('fixture', {sessionId: 'same', signal: controller.signal});
  await new Promise(resolve => setImmediate(resolve));
  await assert.rejects(client.chat('second', {sessionId: 'same'}), error => error.code === 'session_busy');
  controller.abort();
  await assert.rejects(first, error => error.code === 'transport_cancelled');
  assert.ok(sockets[0].closed);
  assert.ok(!sockets[0].sent.some(frame => frame.method === 'chat.abort'));
});

test('separate sessions can run concurrently without crossover', async () => {
  const client = new FeralClient();
  const receipts = await Promise.all([client.chatTurn('one', {sessionId: 'one'}), client.chatTurn('two', {sessionId: 'two'})]);
  assert.deepEqual(receipts.map(receipt => receipt.session_id), ['one', 'two']);
  assert.notEqual(receipts[0].request_id, receipts[1].request_id);
});

for (const sessionId of ['', ' x', 'x ', 'x\u0000', 'x\n', 'x'.repeat(1025)]) {
  test(`invalid session ${JSON.stringify(sessionId.slice(0, 5))} refuses before connect`, () => {
    assert.throws(() => new FeralClient('http://fixture', {sessionId}));
    assert.equal(sockets.length, 0);
  });
}
for (const url of ['http://user:secret@fixture', 'http://fixture?token=secret', 'file:///tmp/brain']) {
  test('URL credentials/protocol rejected', () => { assert.throws(() => new FeralClient(url)); assert.equal(sockets.length, 0); });
}

test('HTTP paths, optional bearer, invocation body and application denial parity', async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push([url, init]);
    const route = new URL(url).pathname;
    const data = route === '/health' ? {service_reachable: true, agent_ready: false}
      : route === '/skills' ? {skills: [{skill_id: 'notes_memory'}]}
      : route === '/api/memory/search' ? {results: [{id: 'fixture'}]}
      : route === '/api/conversations' ? {conversations: [{id: 'thread'}]}
      : {success: false, status_code: 412, error: 'approval required'};
    return new Response(JSON.stringify(data), {status: 200});
  };
  const client = new FeralClient('http://fixture', {bearerToken: 'fixture-secret'});
  assert.equal((await client.health()).agent_ready, false);
  assert.equal((await client.listSkills())[0].skill_id, 'notes_memory');
  await client.searchMemory('a+b&c', 3); await client.listConversations(4);
  const result = await client.createNote('fixture', ['tag'], {sessionId: 'thread'});
  assert.equal(result.success, false); assert.equal(result.status_code, 412);
  assert.deepEqual(JSON.parse(calls.at(-1)[1].body), {skill_id: 'notes_memory', endpoint: 'save_note',
    args: {content: 'fixture', tags: ['tag']}, confirm: false, session_id: 'thread'});
  assert.equal(new URL(calls[2][0]).searchParams.get('q'), 'a+b&c');
  assert.ok(calls.every(([, init]) => init.headers.Authorization === 'Bearer fixture-secret'
    && init.headers.Accept === 'application/json' && init.redirect === 'manual'));
  assert.equal(calls.length, 5);
});

for (const status of [302, 401, 403, 500]) {
  test(`HTTP ${status} raises status error before JSON`, async () => {
    globalThis.fetch = async () => new Response('private invalid JSON', {status});
    await assert.rejects(new FeralClient().health(), error => error instanceof FeralHTTPError && error.status === status);
  });
}
for (const data of ['malformed', '[]']) {
  test('malformed HTTP object is a bounded error', async () => {
    globalThis.fetch = async () => new Response(data, {status: 200});
    await assert.rejects(new FeralClient().health(), error => !error.message.includes(data));
  });
}
test('missing HTTP list field rejects', async () => {
  globalThis.fetch = async () => new Response('{}');
  await assert.rejects(new FeralClient().listSkills(), /invalid record list/);
});
test('HTTP transport deadline rejects without retry', async () => {
  let count = 0;
  globalThis.fetch = async (_url, init) => {
    count++;
    return new Promise((_resolve, reject) => init.signal.addEventListener('abort', () => reject(new Error('private transport'))));
  };
  await assert.rejects(new FeralClient('http://fixture', {timeoutMs: 25}).health(), /deadline expired/);
  assert.equal(count, 1);
});

for (const code of ['chat_turn_receipt_unavailable', 'private arbitrary code']) {
  test('correlated request error preserves only known safe code', async () => {
    onCommand = (wire, command) => wire.emit('error', {request_id: command.msg_id, code, message: 'private diagnostic'});
    await assert.rejects(new FeralClient().chat('fixture'), error =>
      error.code === (code.startsWith('chat_turn_') ? code : 'request_rejected') && !error.message.includes('private'));
    assert.equal(commands(sockets[0]).length, 1);
  });
}
test('default thread persists per client and differs across clients', async () => {
  const client = new FeralClient();
  const first = await client.chatTurn('one');
  const second = await client.chatTurn('two');
  const third = await new FeralClient().chatTurn('three');
  assert.equal(first.session_id, second.session_id);
  assert.notEqual(first.request_id, second.request_id);
  assert.notEqual(first.session_id, third.session_id);
});
