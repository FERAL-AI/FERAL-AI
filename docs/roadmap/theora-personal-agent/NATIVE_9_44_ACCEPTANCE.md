# Mac 9.44 exact-source acceptance

Updated October 5, 2026. This is bounded source, packaged-method and backend
acceptance. It does not certify the complete product or signed distribution.
[Source behavior](LOCAL_READINESS_AND_TASK_STATUS_EVIDENCE.md) and
[full remaining scope](WORK_STATE.md) distinguish those gates.

## Identity and assembly

- Version `2026.9.44`, build `2026100408`, arm64/macOS 13 minimum.
- Runtime source `4c8498bf0a8457c905598a2bf3f423fed44abf73`, published in
  [draft PR310](https://github.com/FERAL-AI/FERAL-AI/pull/310).
- Native binary SHA256
  `33f35e12ba2c5ffe7ae40d5eea387bd49a27013b6edf9a16f46113b85635d3c2`.
- Manifest SHA256
  `b8416f024ba3b84d8bb1dcaf7e47d7a0f17cfee598f4f67f76cc3d069d33ec31`.
- All 496 packaged production Python files match committed source. All 68
  assembly inputs match Git and remain frozen, including the core/SDK dependency
  manifests and uv bootstrap script. No uncommitted production package input is
  admitted. The audit records 12,934 entries, 265 Mach-O files, nine contained
  symlinks, zero issues and passing strict deep ad-hoc signature verification.
- Independent artifact review repeats exact source/input/brand comparisons,
  contained runtime/import probes and strict signatures for both candidate and
  rollback. It records 23,920,640,000 bytes free and no repository/bundle changes.
- Identical 52 Swift inputs and compiler script reuse verified 9.43 optimized
  output. Original fresh compilation remains source `6947bbec1`; no new Swift
  compile or GUI acceptance is claimed. The predecessor manifest and native
  binary hashes are recorded in compile provenance.
- Staging uses cached uv 0.12.3, offline resolution, the unchanged dependency lock
  and the existing desktop extra. All 11 supported desktop imports pass in the
  bundled interpreter. Python 3.11.15, SQLite 3.53.1/FTS5, Python SDK and pinned
  OpenCode 1.18.10 remain contained. Imports are not physical control acceptance.

Canonical app: `desktop-native/build/FERAL Native Preview.app`. The sole generated
rollback is immutable 9.43 at the owned task retention path. Only inspected unused
9.42 is retired; historical manifests and acceptance evidence remain. The 9.43
status-envelope failure also remains recorded and is not retroactively passed.

Assembly starts with 23,995,965,440 bytes free and ends with 23,951,286,272 bytes,
with 44,679,168 bytes net growth in 26.33 seconds. It satisfies the 10 GiB initial,
5 GiB final and estimated 2 GiB artifact budget. Net growth after retention cleanup
is measured; peak usage is not measured. Personal Git, models and profiles are
outside cleanup.

## Frozen source verification

Parent integration passes 2,337 tests across 106 suites, including every manifest
endpoint contract, with two opt-in skips and 55 warnings in 51.51 seconds. All
1,327 Python inputs stay unchanged; source digest
`b7ae5e4288ab3502b11e8ba030abc0b5968b5f95ba8a5c35abcc8354433c80a8`.
Full local typing has 798 existing errors, zero normalized additions including
multiplicity checks and two previous object-type errors removed. Ruff, 24 script
tests, shell syntax, whitespace and documentation navigation pass.

The initial source gate and typing failure remain separate receipts. The preceding
remote backend failure is traced to delegated endpoint contract inspection and a
timing-dependent cancellation fixture. This source makes supported endpoints
explicit and holds a fixture worker until retention is checked. The global
contract checker and action guards remain. New-source CI is still in progress at
this checkpoint; preceding results are not assigned to this build.

## Executed packaged checks

The app's own isolated Python imports actual packaged SkillExecutor,
BackgroundTaskSkill, tracked ChatTurnManager, MemoryStore and TaskFlow. Stable
origin/call replay creates one flow; changed terms refuse; foreign origins cannot
claim inspection. Queued and genuinely failed fixture jobs both have successful
status reads, with failed-job errors retained inside data and action outcome
explicitly not asserted. The active supervisor remains responsive under an inert
competing writer; a 300 ms event-loop observer sees a maximum gap of 11.072 ms,
with the independent writer released after 800 ms. This is one fixture observation,
not a performance guarantee. Cancellation before insertion under a held writer
lock creates zero flow rows, and retained creation workers drain.

Actual packaged public provider routes use inert HTTP transports. For Ollama and
LM Studio, a malformed inventory preserves prior IDs and a warning; a valid empty
inventory clears adapter/cache/default and warning. Cold passive reads and empty
cache reopen make zero live calls. The reopened check uses the emptied adapter;
separate repository regressions cover stale adapter defaults.

Actual packaged preset/configuration routes refuse missing, unreachable, text-only,
unsupported, wrong-exact-tag and exception fixtures without modifying file bytes,
configuration, selected provider or client. An installed exact `llava:7b` fixture
saves the selection and reports presence/classification with
`inference_verified=false`. No inference request is issued. Both packaged probes
record zero network/subprocess attempts and complete owned cleanup. The task probe
uses an inert admission collaborator; it does not establish full approval handling.

Actual bundled backend startup uses an isolated empty provider fixture. Health is
reachable in 1.85 seconds; setup returns HTTP 200 with `setup_complete=false`.
Closing the host lifeline stops the exact owned process with SIGTERM and closes
its listener without fallback termination. This is one lifecycle check, not model
inference or a crash/sleep/wake soak. No native GUI is launched by these checks.

## Remaining acceptance and retained evidence

Exact pending-approval origin transfer, durable result subscriptions and service
ownership remain next task cards. Trusted desktop targets/watch/Stop and physical
drain remain next computer-control cards. Local model/speech provisioning, physical
continuous voice, real accounts/Messages/checkout, glasses/iOS, fresh installation,
migration and signed/notarized updates retain separate acceptance gates. Coding
expansion remains last. No personal account, microphone, screenshot, message,
payment, model download or service registration is performed in this wave.

Small private receipts: `feral-native-9-44-build-20261004/{inputs,result,bundle,core}.json`,
its stage/native logs, `feral-candidate-9-44-manifest.json`,
`feral-944-task-handoff-packaged-evidence.json`,
`feral-944-local-readiness-packaged-evidence.json` and
`feral-944-backend-startup-evidence.json`. The
independent receipt is `feral-944-independent-artifact-evidence.json`. The
[preceding 9.43 ledger](NATIVE_9_43_ACCEPTANCE.md) preserves its failed probe and
historical evidence.
