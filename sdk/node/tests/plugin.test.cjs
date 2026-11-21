'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {definePlugin, startPluginHost} = require('../dist/index.js');
function fixture(handler = async args => ({sum: args.a + args.b})) {
  return definePlugin({name: 'sdk_node_math', tools: [{name: 'calculate', description: 'Add integers',
    parameters: {a: {type: 'integer', description: 'First'}, b: {type: 'integer', description: 'Second', required: false, default: 29}}, handler}]});
}
async function post(host, body, extras = {}) {
  return fetch(host.manifest.endpoints[0].url, {method: 'POST',
    headers: {'Authorization': `Bearer ${host.bearerToken}`, 'Content-Type': 'application/json', ...extras.headers},
    body: typeof body === 'string' ? body : JSON.stringify(body), ...extras});
}
test('explicit HTTP manifest, detached source terms, defaults and no token', async () => {
  const plugin = fixture();
  assert.throws(() => plugin.toManifest(), /explicit/);
  const host = await startPluginHost(plugin);
  try {
    assert.equal(host.manifest.auth.type, 'bearer');
    const ep = host.manifest.endpoints[0];
    assert.equal(ep.method, 'POST'); assert.equal(ep.safety_tier, 'confirm');
    assert.equal(ep.requires_user_approval, true); assert.equal(ep.params[1].default, 29);
    assert.equal(ep.params[0].type, 'integer');
    assert.ok(!JSON.stringify(host.manifest).includes(host.bearerToken));
    plugin.tools[0].parameters.a.type = 'string';
    assert.equal(plugin.toManifest({baseUrl: host.baseUrl}).endpoints[0].params[0].type, 'integer');
    assert.deepEqual(await (await post(host, {a: 13, b: 29})).json(), {sum: 42});
  } finally { await host.close(); await host.close(); }
});
test('auth and malformed requests never execute handler', async () => {
  let calls = 0;
  const host = await startPluginHost(fixture(async () => { calls++; return {}; }), {maxBodyBytes: 32});
  try {
    const url = host.manifest.endpoints[0].url;
    for (const [opts, status] of [
      [{method: 'POST'}, 401],
      [{method: 'POST', headers: {'Authorization': 'Bearer ' + 'x'.repeat(32)}}, 401],
      [{method: 'GET', headers: {'Authorization': `Bearer ${host.bearerToken}`}}, 405],
    ]) assert.equal((await fetch(url, opts)).status, status);
    assert.equal((await fetch(url + '/missing', {method: 'POST', headers: {'Authorization': `Bearer ${host.bearerToken}`}})).status, 404);
    assert.equal((await post(host, 'broken')).status, 400);
    assert.equal((await post(host, [])).status, 400);
    assert.equal((await post(host, 'x'.repeat(40))).status, 413);
    assert.equal((await post(host, '{}', {headers: {'Authorization': `Bearer ${host.bearerToken}`, 'Content-Type': 'text/plain'}})).status, 415);
    assert.equal(calls, 0);
  } finally { await host.close(); }
});
test('loopback and URL authority restrictions', async () => {
  const plugin = fixture();
  for (const baseUrl of ['https://127.0.0.1:9000', 'http://localhost:9000', 'http://example.com',
    'http://127.0.0.1:9000/path', 'http://u:p@127.0.0.1:9000', 'http://127.0.0.1:9000/?token=a']) {
    assert.throws(() => plugin.toManifest({baseUrl}), /loopback/);
  }
  await assert.rejects(startPluginHost(plugin, {host: '0.0.0.0'}), /loopback/);
  await assert.rejects(startPluginHost(plugin, {bearerToken: 'short'}), /credential/);
  for (const options of [{port: -1}, {maxBodyBytes: 1.2}, {maxConcurrent: 0}, {handlerTimeoutMs: Infinity}]) {
    await assert.rejects(startPluginHost(plugin, options), /bound/);
  }
});
test('duplicate and invalid definitions fail before host', () => {
  const def = {name: 'valid', tools: [{name: 'a', description: '', parameters: {}, handler: async () => 1}]};
  assert.throws(() => definePlugin({...def, tools: [...def.tools, ...def.tools]}), /Duplicate/);
  assert.throws(() => definePlugin({...def, name: 'bad__id'}), /identifiers/);
  assert.throws(() => definePlugin({...def, tools: []}), /tools/);
  for (const p of [{type: 'binary'}, {type: 'integer', required: 1}, {type: 'integer', default: NaN},
                   {type: 'integer', default: undefined}, {type: 'integer', default: () => 1}]) {
    assert.throws(() => definePlugin({...def, tools: [{...def.tools[0], parameters: {a: {description: '', ...p}}}]}));
  }
});
test('unknown endpoint and invalid args do not call handler', async () => {
  let calls = 0;
  const p = fixture(async () => { calls++; return 1; });
  await assert.rejects(p.execute('missing', {}), /Unknown/);
  await assert.rejects(p.execute('calculate', []), /object/);
  assert.equal(calls, 0);
});
test('handler failures and oversized responses are truthful non-success', async () => {
  const host = await startPluginHost(fixture(async () => { throw new Error('SECRET_PRIVATE_DETAIL'); }));
  try {
    const res = await post(host, {}); assert.equal(res.status, 500);
    const body = await res.text(); assert.ok(!body.includes('SECRET')); assert.match(body, /unknown/);
  } finally { await host.close(); }
  const large = await startPluginHost(fixture(async () => 'x'.repeat(100)), {maxResponseBytes: 16});
  try { assert.equal((await post(large, {})).status, 502); } finally { await large.close(); }
});
test('timeout occupies bound until underlying handler settles, never retries', async () => {
  let resolve; let calls = 0;
  const host = await startPluginHost(fixture(async () => { calls++; return new Promise(r => { resolve = r; }); }),
    {handlerTimeoutMs: 20, maxConcurrent: 1});
  try {
    assert.equal((await post(host, {})).status, 500);
    assert.equal((await post(host, {})).status, 503);
    assert.equal(calls, 1);
    resolve({sum: 42}); await new Promise(r => setTimeout(r, 10));
  } finally { await host.close(); }
});
test('close releases listener; duplicate port refuses; no auto restart', async () => {
  const host = await startPluginHost(fixture());
  const port = Number(new URL(host.baseUrl).port);
  await assert.rejects(startPluginHost(fixture(), {port}));
  await host.close(); await assert.rejects(fetch(host.baseUrl));
  const replacement = await startPluginHost(fixture(), {port}); await replacement.close();
});


test('invalid UTF8 JSON and nonfinite result are not silently converted', async () => {
  let calls = 0;
  const host = await startPluginHost(fixture(async () => { calls++; return {n: NaN}; }));
  try {
    const bytes = Buffer.from([123, 34, 97, 34, 58, 34, 255, 34, 125]);
    const bad = await fetch(host.manifest.endpoints[0].url, {method: 'POST',
      headers: {'Authorization': `Bearer ${host.bearerToken}`, 'Content-Type': 'application/json'}, body: bytes});
    assert.equal(bad.status, 400); assert.equal(calls, 0);
    const invalidResult = await post(host, {});
    assert.equal(invalidResult.status, 500); assert.equal(calls, 1);
    assert.equal((await invalidResult.json()).outcome, 'unknown');
  } finally { await host.close(); }
});
