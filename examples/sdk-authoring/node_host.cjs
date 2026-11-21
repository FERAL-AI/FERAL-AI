'use strict';
// Use after installing the built SDK into your own Node project.
const fs = require('node:fs');
function calculator(sdk) {
  let invocations = 0;
  const plugin = sdk.definePlugin({name: 'sdk_node_math', version: '1.0.0',
    description: 'Add two bounded integers locally.', tools: [{name: 'calculate',
      description: 'Add two integers between -1000000 and 1000000.',
      parameters: {a: {type: 'integer', description: 'First integer'}, b: {type: 'integer', description: 'Second integer'}},
      async handler({a, b}) {
        if (!Number.isSafeInteger(a) || !Number.isSafeInteger(b) || Math.abs(a) > 1000000 || Math.abs(b) > 1000000) {
          throw new Error('Bounded integer arguments required');
        }
        invocations++; return {sum: a + b};
      }}]});
  return {plugin, count: () => invocations};
}
module.exports = {calculator};
if (require.main === module) {
  (async () => {
    const sdk = require('@feral/sdk');
    if (!process.argv[2] || !process.env.FERAL_PLUGIN_TOKEN) throw new Error('Supply a new manifest file path and FERAL_PLUGIN_TOKEN');
    const {plugin} = calculator(sdk);
    const host = await sdk.startPluginHost(plugin, {bearerToken: process.env.FERAL_PLUGIN_TOKEN});
    try {
      fs.writeFileSync(process.argv[2], JSON.stringify(host.manifest, null, 2) + '\n', {flag: 'wx'});
    } catch (error) { await host.close(); throw error; }
    // No credentials in normal output. A manifest is not installation or authorization.
    console.log(`Plugin listening at ${host.baseUrl}; manifest written, not installed.`);
    for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => { void host.close(); });
  })().catch(() => { console.error('Plugin host setup failed. Check SDK, credential and new manifest path.'); process.exitCode = 1; });
}
