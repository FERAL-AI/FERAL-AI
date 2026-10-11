# Native 9.32 candidate acceptance

October 2, 2026. Evidence belongs to this exact artifact; later working source
changes are not included automatically.

| Identity | Verified value |
|---|---|
| App | `desktop-native/build/FERAL Native Preview.app` |
| Version / build | `2026.9.32` / `2026100206` |
| Bundle ID | `ai.feral.native.preview` |
| Packaged production source | `61550e74fbe806305034ccfad28bc96c6953a9d7` |
| Executable SHA-256 | `9e0c96b7fa373c1a20a94ab90e63f209f9098f6a563370d81304bf4f2b9314fb` |
| Source comparison | All 484 production Python files equal that commit |
| Bundle audit | 13,095 files; 265 Mach-O objects; 9 internal symlinks; no issues |
| Runtime | Python 3.11.15; SQLite 3.53.1 with FTS5; OpenCode 1.18.10 |
| Signature | Strict deep ad-hoc verification passed; not Developer-ID/notarized |

## Actual packaged-backend journey

**Passed**, exit 0. The bundled interpreter/core ran authenticated HTTP and
WebSocket requests against a fresh disposable profile and an already installed
local model. This was headless text-only acceptance, not native GUI or audio.
Initial startup and restart both exposed exactly zero tools from all 43 empty
installed fixture manifests. The browser startup correction now preserves that
installed manifest. Explicit authenticated browser reload/readback also passed
before requests on both boots. No production implementation files were altered.

The journey verified an actual IN_PROGRESS turn and exact Stop, foreign-session
and stale-revision recovery refusal without checkpoint mutation, explicit
reviewed recovery, independent read-only recovery status and capability readback,
real local-model recall of the committed fact, clean owned restart, unchanged
checkpoint and a second real recall. The UI transcript projection was unchanged.
Cancelled input was absent from restored context and no task was replayed.
Four durable terminal receipts were present, including exactly one cancellation;
zero tool executions were recorded. No provider fallback or model download ran.

Both owned servers stopped with SIGTERM, exit -15, without forced kill. Their
PIDs and listeners were independently absent. Strict signature and executable
hash were verified again after the run. The preceding 9.31 artifact and its two
failed attempts remain preserved; see [9.31 evidence](NATIVE_9_31_ACCEPTANCE.md).

Local evidence:

- `/private/tmp/feral-native-9-32-recovery-20261002-attempt-1/final-result.json`
- `/private/tmp/feral-native-9-32-source-comparison.json`
- `/private/tmp/feral-native-9-32-bundle-audit.json`
- `/private/tmp/feral-candidate-9-32-manifest.json`

The manifest SHA-256 is
`fdf509e55d13f92bfac9971baa1ace79929a963e88cf820c0706c165fb00b13c`.
The recovery harness SHA-256 during the journey is
`a66a2e8c24702b3a4f24c2b3bd263edda2352a2c37effad2e11a401ade2a0357`.
Existing attempt directories must never be reused or reset.

## Exact-source remote validation

The [PR CI run](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37097443569)
passed: backend 12,754 tests, 83 skipped, 74.89% coverage; Python SDK 112;
installed Node-plugin runtime 1; web 1,358 and stubbed Playwright 107.
The previously failing authenticated vault-status route sweep passed.
Mypy retained 811 existing diagnostics against the unchanged 812 ceiling; this
is a passing count ratchet, not zero type errors.
[Native checks](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37097443556),
docs, naming and version checks also passed. These workflows tested the synthetic
PR merge of exact source 61550e7 into unchanged main 452a012. Live-brain and
main-only Linux matrix checks were skipped; stubbed browser results do not
replace actual integration.

## Remaining gates

Computer Use still fails with `Sky Computer Use native pipe startup failed`;
actual 9.32 GUI acceptance has not run. This is not an observed app crash.
The ad-hoc artifact refuses fresh cloud-vault initialization until genuine
version-bound signed OS acceptance exists. Real cloud-provider/Keychain,
microphone/audio, migration including macOS preferences, clean installation,
update/rollback, sleep/wake, Intel/Linux and physical glasses/iPhone/account
journeys remain separate gates. Speech-output interruption and default archive
coverage corrections made after source 61550e7 are not part of this candidate.
No real messages, purchases, account login, Keychain reset, merge or release ran.
