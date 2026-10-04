# Native 9.37 browser acceptance

Verified October 4, 2026. This records the existing Mac client and runtime, not a
cloud browser, complete product certification or signed distribution release.

## Artifact identity

| Item | Verified value |
|---|---|
| App | `desktop-native/build/FERAL Native Preview.app` |
| Version / build | `2026.9.37` / `2026100401` |
| Runtime source | `b9afadb236e6e39d798a311d38955bb9e8051e74` |
| Native executable SHA-256 | `a661c5f8ca32b96b12a447224cf8c7f8e1704c789599349b5ad2c253f2f91142` |
| Manifest SHA-256 | `9cea3ad3c137b675a37c32b61073654b552a8edc1ae741dfebcc27dfa8b19003` |
| Input equality | 52 native inputs; 489 packaged Python files |
| Bundled runtime | Python 3.11.15, SQLite 3.53.1 with FTS5, OpenCode 1.18.10 |

Optimized assembly took 218.71 seconds with cached/offline dependencies. Strict
ad-hoc signature, bundled runtime probes and production-source equality passed.
Disk headroom was 28 GiB during the build; new-artifact budget was 2 GiB. Verified
obsolete 9.34/9.35 generated app copies were retired with ownership receipts.
The canonical 9.37 app and one preceding 9.36 rollback copy remain. Personal data,
models, profiles, reports and Git history were preserved.

## Actual app checks

Two fresh profiles used deferred optional vault setup, null keyring, cache-only
embeddings, disabled proactive/learning/vision/sync features, normal shipped tools
and an explicitly identified existing local model. No model download or model
inference was needed for browser viewing. Chrome used fresh synthetic profiles,
owned loopback pages and dynamic CDP ports, never the personal browser or accounts.

| Behavior | Actual observation |
|---|---|
| Avatar and local setup | Companion selection and reviewed provider completion worked; all 22 native destinations remained present |
| Empty Browser screen | No browser was attached and viewing did not launch one |
| Consent | Explicit viewing review preceded delivery of frames |
| Real media | Actual Chrome JPEGs appeared in the native screen; password control and iframe content were hidden |
| Correct field | Synthetic Unicode appeared in the intended field; the other field retained its original value |
| Click outcome | Synthetic button changed its label in the real frame |
| Coordinate marker | Confirmed coordinate click displayed the orange input-location ring on the clicked button |
| Stop viewing | Native Stop immediately removed the frame and returned to the stopped state |
| Navigation | Leaving Browser retired the view; returning required a fresh Start review |
| Expiry and error | Five-minute expiry removed media and displayed an error without crashing |
| Lifecycle | Both native hosts exited normally with code 0; their backend listeners closed |
| Browser cleanup | Owned Chrome exited 0; local/debug listeners closed and its disposable profile was removed |

Typing and selector actions used the existing operator HTTP surface against the
synthetic page. The coordinate click used the registered skill/executor and exact
session-bound approval contract. The decision was completed through the headless
approval API; ambiguous duplicate button labels prevented reliable Computer Use
targeting of the second native approval button. This is not a model-selected task
or a successful native approval-button acceptance claim. The actual rendered
coordinate marker, native consent/Stop/navigation and normal Quit were observed
through Computer Use.

## Failures retained and follow-up cards

- The first standalone Chrome probe sampled a password bullet as its unmasked
  color control. A real diagnostic confirmed the geometry and colors. The repair
  requires a 9-by-9 magenta background patch and requires that same patch to become
  white in the masked frame. Failed receipts remain; the assertion was not removed.
  A private corrected harness passed all 11 real Chrome/controller/route checks.
  The committed corrected harness requires its own exact-source run.
- The first GUI launcher attempt used an interpreter without its dependency; it
  stopped before launch. A subsequent launcher process guard blocked its own
  verification calls before launch. The corrected helper and fresh profiles
  passed; these are acceptance-harness failures, not app crashes.
- Existing REST tool ingress accepts an omitted session and queues an empty-session
  approval. Native approval sends that empty identity and receives HTTP 422 before
  execution. An explicit session-bound request succeeded. Fix ingress before
  mutation/approval dispatch, retain trusted session-less reads, and render legacy
  unbound rows as non-approvable. Do not substitute a primary session or replay.
- Playwright selector click/hover changes the real page but does not currently
  record a marker. Coordinate and CDP selector paths do. Extend markers only from
  actual verified input events bound to the same target/document, with layout,
  navigation, failure and connection-race checks; do not invent bounding-box points.

The runtime implementation passed 1,230 backend checks across 43 suites with one
opt-in real-Chrome case skipped, separate 26 focused/real-Chrome cases, 74 native
Browser assertions, linked provider/setup/model checks and production typecheck.
Counts overlap. Screenshot-to-model wire fixtures passed 44 checks. Ruff passed.
Full mypy retains 809 errors in 233 files with zero normalized additions/removals;
this is not type-clean. All executed required workflows for b9af pass; opt-in
real-brain E2E remains skipped.

Desktop sharing/system cursor, task-owned browser contexts, chat-linked takeover,
paid model computer-use sessions, real accounts/audio/glasses/Messages/payments,
9.37 chat Stop/latency/migration, clean installation and release signing remain
open. Refer to [browser behavior](BROWSER_VIEW_EVIDENCE.md),
[release gates](RELEASE_READINESS.md) and [current checkpoint](WORK_STATE.md).
