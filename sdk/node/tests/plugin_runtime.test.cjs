'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const sdk = require(process.env.FERAL_SDK_PACKAGE_DIR || '../dist/index.js');
const repo = path.resolve(__dirname, '../../..');
const {calculator} = require(path.join(repo, 'examples/sdk-authoring/node_host.cjs'));

test('actual existing registry, ToolRunner, review and SkillExecutor call live SDK loopback host', {timeout: 30000}, async () => {
  // Mandatory runtime fixture: an absent interpreter is a failure, never a skip.
  // Validate before opening a host or passing the disposable service credential.
  const python = process.env.FERAL_TEST_PYTHON === undefined
    ? path.join(repo, '.venv/bin/python') : process.env.FERAL_TEST_PYTHON;
  if (!python || !path.isAbsolute(python) || /[\x00-\x1f\x7f]/.test(python)) {
    throw new Error('FERAL_TEST_PYTHON must be an absolute executable Python path');
  }
  try {
    if (!fs.statSync(python).isFile()) throw new Error("Not an interpreter file");
    fs.accessSync(python, fs.constants.R_OK | fs.constants.X_OK);
  }
  catch { throw new Error('Runtime interpreter unavailable; set FERAL_TEST_PYTHON to the core environment executable'); }
  const {plugin, count} = calculator(sdk);
  const host = await sdk.startPluginHost(plugin);
  try {
    const result = await new Promise((resolve, reject) => {
      const child = spawn(python, [path.join(repo, 'examples/sdk-authoring/node_runtime_check.py')],
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
