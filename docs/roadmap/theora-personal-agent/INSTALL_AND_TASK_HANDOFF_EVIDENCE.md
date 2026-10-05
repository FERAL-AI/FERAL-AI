# Mac dependencies and conversational task handoff

Updated October 4, 2026. This record separates source verification from packaged,
physical and account acceptance. Current publication/artifact identity is in
[WORK_STATE](WORK_STATE.md). Coding expansion remains last.

## Mac desktop dependency closure

The Mac staging path uses the existing `feral-core[llm,desktop]` extra with the
unchanged lock constraints. Linux staging continues to use `llm`; this change
does not establish Linux input/display support. The additional dependency closure
is PyAutoGUI, MouseInfo, PyGetWindow, PyMsgBox, pyperclip, PyRect, PyScreeze,
PyTweening and rubicon-objc. The lock fixes their versions; no model weights or
local speech engines are included by this change.

`scripts/check_native_bundle.py` checks contained module sources and distribution
metadata before an explicit import probe. Mac staging runs that probe against
the staged interpreter. The probe uses an isolated task directory, empty
development PATH and task-specific FERAL_HOME/FERAL_DATA_HOME. It imports Pillow,
Quartz, AppKit and the supported Mac input closure, without capture, clicking,
typing, clipboard access or launching an application. PyGetWindow explicitly
does not implement macOS; its source/metadata are checked without importing it.

The child import code sets a 16 KiB file-output cap before third-party imports.
This cap starts after interpreter startup; it does not bound an invalid
interpreter emitting output before that code executes. The parent reads at most
2,049 receipt bytes and withholds process diagnostics from public output. The
20-second deadline and exact receipt shape refuse timeout, malformed output and
import failure. This is dependency readiness, not permission or physical action
acceptance.

Executed source checks: 24 script tests, including missing/duplicate/corrupt or
external dependency metadata, Mac adapter absence, private failure reporting,
timeout and actual post-startup output flooding. Independent review reproduces
the interpreter-startup qualification above. The development interpreter passes
all 11 imports. Immutable 9.42 fails the new static capability gate because it
lacks the additional closure; its historical acceptance has not been erased or
changed. A new exact-source package must establish bundled imports separately.

## Passive provider discovery

Cold-cache model discovery previously invoked a provider refresh even when the
native/shared API or default CLI requested `live=False`. The catalog now returns
existing cache or fallback inventory without provider I/O, including when force
is set. Explicit `live=True` refresh remains available. Empty fallback inventory
does not imply installed models, and previous refresh warnings remain visible.
Four inert prepatch cases reproduced the defect; 14 focused postpatch tests pass
through the actual catalog, shared API and CLI paths. The web settings mount
still explicitly requests live discovery; it is not claimed passive here.

## Existing background-task transport

The registered Python implementation invokes the existing TaskFlow control-plane
helper in process, preserving SkillExecutor admission. It no longer depends on
an HTTP call to fixed port 9090 or loses task-local context across that transport.
Each accepted goal uses the existing bounded TaskFlow runner and a dedicated
execution session. Creation grants no permission for later actions.

Tracked handoff attribution comes from an actual live ChatTurnManager record and
its persisted accepted/running receipt. Session, request, turn and tool-call
identity determine one durable creation key; the exact title, goal/subtasks and
surface determine its terms digest. An additive unique SQLite index prevents
duplicate creation across runtime instances/reopen. A changed request with the
same identity refuses. Flow and steps commit atomically; failed readback remains
unknown. This is exact-call duplicate prevention, not general semantic deduping
of differently planned tasks.

Origin session is stored in a creation-owned indexed column. Agent inspection
queries this scope directly, so a large number of unrelated jobs does not hide
older owned work. A body/context field cannot claim tracked ownership or select
another execution session. The explicit existing local-operator REST entry point
remains a compatibility path and is labeled unverified attribution.

Input is bounded to 32 subtasks and 64 KiB of task text. Retained, bounded worker
tasks keep creation/read operations off the event loop. Cancellation before
transaction admission must create zero jobs; cancellation after commit does not
undo a stored job. Original execution surface must survive handoff, so remote or
voice work cannot obtain websocket privileges by becoming a background task.

Creation also uses nonblocking SQLite transaction admission while holding the
runtime mutex, with bounded worker backoff outside that mutex. This keeps the
active supervisor from blocking chat while a competing writer prevents creation.
The existing synchronous operator CRUD paths are not claimed fully asynchronous.

Executed worker verification passes 208 tests across 12 suites, including 40 new
origin cases. The active-supervisor contention case holds a separate writer for
300 ms and observes loop gaps below 150 ms. Cancellation before insertion creates
zero jobs; two runtime instances and reopen reuse one exact handoff; malformed
and foreign origins, unknown surfaces and raw context spoofing refuse. Actual
SkillExecutor dispatch reaches the Python adapter without HTTP and preserves plan
refusal. These are inert boundary checks, not physical or provider acceptance.
Independent final review passes 86 tests across four scoped suites with network
connections prohibited, confirms the frozen hashes and finds no blocking defect
within this bounded creation contract. Existing synchronous database-writer
contention, approval-origin transfer and result delivery remain outside that gate.

Frozen parent integration passes 1,962 tests across 100 suites, with two opt-in
skips and 55 warnings in 51.55 seconds. All 1,325 Python inputs remain unchanged;
digest `241e37798db9e41638ae1debf19e9bc26c6b47d6890ca93fdd610f3420be1891`.
Full local typing completes with 800 existing errors, no normalized additions,
and one optional-return diagnostic removed. Ruff, 24 bundle script tests, shell
syntax and documentation navigation pass. Packaging must establish these changes
inside a new candidate separately.

## Remaining acceptance gates

Default approval-resume attribution, committed input revision, durable result
subscriptions, user-visible delivery/reconnect and supervised background service
mode are still separate gates. Accepted/stored does not mean started, approved,
completed or result delivered. Existing immediate-turn disconnect cancellation,
unknown-effect reconciliation, bounded execution and hard surface denials remain.

Desktop target ownership is also incomplete. Independent inert review reproduced
overlapping GUI threads, cancellation leaving a dispatched input thread active,
and an AX reference usable by another session. Extend existing executor admission
with trusted selected-target binding and retain the foreground lane until
physical work drains. Dependencies alone do not implement watch/Stop/takeover.

Clean installation, provider/local model and speech provisioning, genuine accounts,
physical voice/desktop, Messages, checkout, glasses and signed distribution retain
their own gates in [release readiness](RELEASE_READINESS.md) and the
[complete requirement map](REQUEST_COVERAGE.md).
