# Security dependency validation

October 2, 2026. Parent updated the three package manifests/locks and ran
`npm ci --ignore-scripts`. This worker independently checked every relevant
nested lock entry against the installed package and ran the existing checks.
No package changes, test assertion changes, GitHub alert dismissal or merge
were performed by this worker.

## Advisory source and resolved versions

The maintained upstream advisories establish these fixed versions:

- [Undici GHSA-w293-vg96-wgc3](https://github.com/nodejs/undici/security/advisories/GHSA-w293-vg96-wgc3):
  the affected 7.x TLS callback handling is fixed in 7.29.1.
- [Browserslist GHSA-73wf-gq98-2v4g](https://github.com/browserslist/browserslist/security/advisories/GHSA-73wf-gq98-2v4g):
  the untrusted custom statistics failure is fixed in 4.28.7.
- [Baseline browser mapping 2.11.0 release](https://github.com/web-platform-dx/baseline-browser-mapping/releases/tag/v2.11.0):
  invalid options throw rather than terminating the process; this addresses
  [GHSA-w5vr-8v7q-w6rv](https://github.com/advisories/GHSA-w5vr-8v7q-w6rv).
- [Vitest GHSA-82fw-gwwq-j7x9](https://github.com/vitest-dev/vitest/security/advisories/GHSA-82fw-gwwq-j7x9):
  redirect mock file access is fixed in both Vitest and its mocker at 4.1.11.

All nine matching package copies in these three assigned projects passed the
floor comparison, and their installed versions exactly matched their lock entries.

| Project | Package | Lock and installed version | Fixed floor |
|---|---|---|---|
| `feral-client-v2` | `undici` | 7.30.0 | 7.29.1 |
| `feral-client-v2` | `browserslist` | 4.29.3 | 4.28.7 |
| `feral-client-v2` | `baseline-browser-mapping` | 2.11.27 | 2.11.0 |
| `feral-client-v2` | `vitest`, `@vitest/mocker` | 4.1.11 each | 4.1.11 |
| `feral-extension` | `vitest`, `@vitest/mocker` | 4.1.11 each | 4.1.11 |
| `feral-nodes/ts-node-sdk` | `vitest`, `@vitest/mocker` | 4.1.11 each | 4.1.11 |

The inventory checks all matching entries in `package-lock.json.packages`, including
nested `node_modules` paths. These packages have one installed copy apiece in
their listed projects. This is version/lock evidence for these advisories, not
a claim that all software is vulnerability-free or that GitHub has closed alerts
on its default branch.

## Commands and actual results

Host: Node `v25.4.0`, npm `11.7.0`. Initial free space was 6.3 GiB. Test worker
count was bounded to two and at most two test commands ran concurrently.
Logs are local QA artifacts in `/private/tmp`, not committed runtime data.

From `feral-nodes/ts-node-sdk`:

```sh
npm run typecheck > /private/tmp/feral-security-qa-node-typecheck-20261002.log 2>&1
npm run build > /private/tmp/feral-security-qa-node-build-20261002.log 2>&1
NODE_OPTIONS=--max-old-space-size=2048 npm test -- --maxWorkers=2 > /private/tmp/feral-security-qa-node-tests-20261002.log 2>&1
```

All exited 0. Typecheck and TypeScript build passed. Vitest: **4 files, 39 tests
passed in 821 ms**.

From `feral-extension`:

```sh
NODE_OPTIONS=--max-old-space-size=2048 npm test -- --maxWorkers=2 > /private/tmp/feral-security-qa-extension-20261002.log 2>&1
```

Exit 0: **3 files, 19 tests passed in 1.37 seconds**. Vite emitted a future
native-config-loader warning about the existing CommonJS/ESM configuration.
No warning suppression or configuration/assertion change was made.

From `feral-client-v2`:

```sh
NODE_OPTIONS=--max-old-space-size=3072 npm run test:coverage -- --maxWorkers=2 > /private/tmp/feral-security-qa-web-coverage-20261002.log 2>&1
```

Exit 0: **171 files, 1,358 tests passed in 139.28 seconds**. Coverage met the
unchanged gate: statements 65.45%, branches 57.92%, functions 59.61%, lines
68.75%. Node 25 emitted local-storage-path warnings; they did not change test
assertions or results.

```sh
NODE_OPTIONS=--max-old-space-size=3072 npm run build > /private/tmp/feral-security-qa-web-build-20261002.log 2>&1
```

Build passed in 4.18 seconds. From repository root:

```sh
NODE_OPTIONS=--max-old-space-size=3072 bash scripts/build_webui_v2.sh > /private/tmp/feral-security-qa-web-sync-20261002.log 2>&1
.venv/bin/python scripts/check_webui_v2_contract.py > /private/tmp/feral-security-qa-web-contract-20261002.log 2>&1
```

Both exited 0. The build/sync script was inspected first: it rebuilds the web
source and replaces only the generated `feral-core/webui_v2` directory, adding
its existing Python package markers. Dependencies were already present; this
run did not invoke its optional install branch. The model-picker contract check
passed. Vite reported the existing large-chunk warning; no threshold was altered.

A separate byte comparison checked all **67** `dist` files against the synced
bundle and every local `src`/`href` asset in `index.html`: no mismatch or missing
reference. Index SHA-256:
`3b2a162a30ca988f8ef03f637445e30fd9d5b19423819292f1010e3b576b9f71`.
The rebuild produces no tracked web bundle changes against this checkout.
Owned package/lock and bundle whitespace checks passed. Final free space was
still 6.3 GiB.

The installed/lock inventory is recorded locally in
`/private/tmp/feral-security-qa-resolved-versions-20261002.log`; independent
generated-file/reference checks are in
`/private/tmp/feral-security-qa-web-coherence-20261002.log`.

## Acceptance boundary

These are existing package tests, compiler checks and generated web bundle
coherence checks. They do not exercise a live user deployment, external accounts,
physical devices, a newly assembled Mac candidate or clean-machine Linux/Node
version acceptance. Node 16 runtime compatibility is not established by running
the developer tests on Node 25.
