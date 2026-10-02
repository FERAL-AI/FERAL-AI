'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {spawn} = require('node:child_process');
const sdk = require(process.env.FERAL_SDK_PACKAGE_DIR || '../dist/index.js');
const repo = path.resolve(__dirname, '../../..');
const {calculator} = require(path.join(repo, 'examples/sdk-authoring/node_host.cjs'));

test('actual existing registry, ToolRunner, review and SkillExecutor call live SDK loopback host', {timeout: 30000}, async () => {
  const {plugin, count} = calculator(sdk);
  const host = await sdk.startPluginHost(plugin);
  try {
    const result = await new Promise((resolve, reject) => {
      const child = spawn(path.join(repo, '.venv/bin/python'), [path.join(repo, 'examples/sdk-authoring/node_runtime_check.py')],
        {cwd: repo, stdio: ['pipe', 'pipe', 'pipe']});
      let out = ''; let err = '';
      child.stdout.on('data', chunk => { out += chunk; });
      child.stderr.on('data', chunk => { err += chunk; });
      child.once('error', reject);
      child.once('close', code => { if (code !== 0) reject(new Error(`Disposable runtime check failed (${code}): ${err}`)); else resolve(JSON.parse(out.trim().split('\n').at(-1))); });
      // Ephemeral service token travels only over the private pipe, never the command or logs.
      child.stdin.end(JSON.stringify({manifest: host.manifest, credential: host.bearerToken}));
    });
    assert.deepEqual(result, {contract: 'sdk-node-runtime-v1', validated: true, installed: true,
      registered_via_existing_route: true, reviewed_sum: 42, single_use_review: true,
      denied: true, foreign_review_refused: true, domain_denied: true, uninstalled: true, stale_review_refused: true});
    assert.equal(count(), 1);
  } finally { await host.close(); }
});
