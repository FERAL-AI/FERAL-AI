# Native 9.40 acceptance ledger

Updated October 4, 2026. `2026.9.40/build2026100404` is packaged and passes
bounded actual Mac acceptance for saved-provider probing, exact browser-request
approval, observed page outcome, disconnect and normal Quit. This is not full
product or personal-profile acceptance.
The predecessor is recorded in [9.39 acceptance](NATIVE_9_39_ACCEPTANCE.md).

## Scope

The catalogue must probe the local endpoint actually selected by the runtime at
boot and activation, including no-key local activation. Ollama's native endpoints
must remove only the terminal OpenAI `/v1` suffix and preserve proxy prefixes.
Inactive scoped provider overrides remain intact. Repeated unchanged binding
must retain its cache; a changed binding invalidates it.

A probe finishing against an adapter replaced during the request must not publish
readiness or restore stale model inventory. It returns a typed configuration-change
refusal; the client reviews again rather than automatically retrying.

The original wrong destinations were reproduced in observed-request fixtures.
The actual 9.39 local catalogue contained the pinned model, while its native probe
used another endpoint. Positive native probe acceptance therefore requires the
backend correction; a parser fixture alone cannot close this gate.

## Packaging and actual gates

Reuse of 9.39's optimized native executable is allowed only with verified artifact
and manifest identity, all 52 compiled Swift inputs unchanged, and identical build
script/compiler flags. Reused compile provenance must be recorded explicitly.
The new backend still requires committed-source equality, bundled-runtime and
strict ad-hoc signature audits. Otherwise, compile fresh.

Keep canonical plus one preceding app, at least 10 GiB free before packaging and
at most 2 GiB of new artifacts. The fresh GUI uses an owned synthetic Chrome,
isolated default-path discovery and no personal accounts or model inference.
Require positive saved-provider probe, exact selected-chat approval, observed page
outcome, Stop/disconnect and normal Quit with exit 0 and closed listeners.

Real personal Chrome permission, whole-desktop sharing, independent concurrent
browser resources, audio/cloud accounts/glasses/Messages/commerce, migration,
clean installation and signed distribution remain separate release gates.

## Frozen source verification

- Integrated backend: 1,801 passed across 68 suites; three opt-in skips and 229
  warnings; 43.19 seconds. All 1,308 Python inputs unchanged, digest
  `9099e667c936591ca2f0321ca0d378245260d0f9d99441dc1f89e0ce76ade7de`.
- Focused observed-request/restart fixtures: 17 passed. Related provider suites:
  140 passed, overlapping those focused cases. Original three failures retained.
- Ruff and independent source review pass. Full mypy remains 809 errors in 233 files
  with zero normalized additions/removals; it is not type-clean.
- Swift inputs are unchanged from the 39 source tested by Onboarding 73,
  Oversight 16 groups, Browser 115, linked model 28 groups, desktop 39 and errors 5.

Private evidence: `/private/tmp/feral-chrome-native-940-integration-20261004-tests-se_sw9rz/receipt.json`,
`/private/tmp/feral-native-940-typing-delta.json` and
`/private/tmp/feral-local-probe-endpoint-{baseline,final,related}.log`.
The actual BrainState bootstrap tests use persisted disposable settings and scoped
constructor environment, stopping before credentials; HTTP is controlled. No
real inference or accounts were exercised. Pre-existing empty-model bootstrap
selection from a provider-only stale cache is outside this correction; native
acceptance selects an explicit cached model.

## Immutable artifact

| Property | Verified value |
| --- | --- |
| Runtime source | `250787b2d1512d2cef98ff77f8ceda607875ee24` |
| Version/build | `2026.9.40 / 2026100404` |
| Native executable SHA256 | `27c35c02915c9c34cb14c0c20a207946b088557f2d76d39f19906ec3e097b135` |
| Manifest SHA256 | `074f8756515a6b9652f9089c9d5ff4a68dee8ae43a631db52c7c7f09de933d71` |
| Audited payload | 52 native inputs and 493 packaged Python files equal committed source |
| Compile provenance | Reused verified 9.39 optimized executable; all 52 Swift inputs, build script and compiler flags identical |
| Assembly/audits | 22.14 seconds; bundled-runtime and strict ad-hoc signature audits pass |

The executable was not freshly compiled for 9.40. The changed backend was packaged
from the exact committed source, and the assembled app was signed again. Canonical
9.40 plus one preserved 9.39 rollback remain. Before packaging, free space exceeded
the 10 GiB floor; the new-artifact budget was 2 GiB. No model was downloaded.

## Actual native GUI observations

Computer Use operated the immutable app against a fresh disposable profile and an
owned installed-Chrome process serving a synthetic local page. Default-path
discovery used only the two-line port file in isolated HOME: no personal profile
was copied and no `FERAL_CDP_*` endpoint was injected.

- First-use avatar and reviewed local-provider activation/readback/completion
  passed. A catalogue change invalidated the first probe review before dispatch;
  a refreshed explicit review succeeded. The UI reported the saved provider
  reachable and explicitly said a successful model response remained unverified.
- Connection and exact tab attachment were reviewed separately. Viewing was
  disabled before connection. The selected chat owned the connection.
- One synthetic click was queued through the central tool path. The native
  Oversight row, confirmation and receipt said request-only approval and displayed
  the chat/connection/tab scope. The actual native approval executed the request
  once, with no ongoing tool permission. No resolve-API shortcut was used.
- The native live page visibly showed `Synthetic click confirmed`, with both
  original field values unchanged. Password and embedded-frame content were not
  visible. This establishes the synthetic click outcome, not a merchant transaction.
- Stop viewing removed the live frame. Disconnect removed the connection and
  disabled Start viewing. Normal Cmd-Q exited the host with code 0; the backend
  listener closed. Owned Chrome then exited with code 0 and both debug/page
  listeners closed. The source remained unchanged throughout the run.

The click marker had expired before the final page observation. No new 9.40
marker observation is claimed; 9.38 marker evidence remains historical. The
passive provider badge still said it had not been probed after the successful
explicit probe; reconcile that status projection in a follow-up. Neither a real
model turn nor a personal Chrome permission prompt was exercised in this run.

Private receipts:
`/private/tmp/feral-native-9-40-build-20261004/result.json`,
`/private/tmp/feral-native-populated-9-40-fresh-20261004-attempt-existing-browser1/existing-chrome-gui-wave-result.json`,
`native-exit.json` and `synthetic-click-queue-result.json` in that GUI evidence root.
The wrapper reports success, normal Quit and closed listeners; the page and
native approval observations were made separately through Computer Use.
Independent read-only process and listener checks confirm all four owned
processes absent and no listeners on their recorded ports. Exact-source general,
native, desktop, docs, version and naming workflows all completed successfully;
the opt-in real-brain workflow was skipped. Public tracked-document leakage and
navigation checks pass. Ignored local dossiers are not publication inputs.
