# Native Forge proposal, generation and statistics parity

Date: 2026-10-02. This source slice is separate from the immutable 2026.9.28
candidate. It changes only `NativeCapabilitiesFeature.swift`, its existing
fixture harness and this evidence file. No backend, installed application,
staged resources, Git state or external accounts were mutated by this worker.

## Supported contracts inspected

The actual router is `feral-core/api/routes/tool_genesis.py`; its implementation
is `feral-core/agents/tool_genesis.py`.

| Existing endpoint | Actual contract |
| --- | --- |
| GET `/api/tool-genesis/proposals` | `proposals` rows contain `sequence_id`, name, ordered tools, `seen_count` and description. These are observed repeated sequences, not generated code. |
| GET `/api/tool-genesis/stats` | Four integer counters: sequences_tracked, proposals_ready, tools_generated and total_uses. Uninitialized service returns `{}`. |
| POST `/api/tool-genesis/propose` | Body `{intent}`. Success returns `success`, `tool_id`, and at most 800 code points of preview. |
| POST `/api/tool-genesis/generate` | Body `{sequence_id}` referring to a tracked sequence. Success returns `success` and nested tool_id/name/description/preview. It does not accept a freeform prompt. |
| GET `/api/tool-genesis/pending` | Only currently unapproved in-memory generated records, with at most 400 code points of preview and `source_sequence`. |
| GET `/api/tool-genesis/list` | Generated inventory with tool_id/name/description/source/use_count and timestamps. Inclusion does not prove approval or live registration. |

Generation calls the configured LLM, checks Python syntax/AST and records an
unapproved draft. It does not sandbox-run, register, import or execute that
draft. The existing web Generate explanation overstates sandbox execution;
this native slice uses the actual source behavior. The independent approve
route marks approval and promotes files, possibly hot-reloading code. Its
existing reviewed native registration controls remain separate.

## Implemented native behavior

Forge now reads tracked proposals and actual statistics alongside existing
draft/generated inventories. Missing or malformed counters do not become
zeroes. Empty statistics explicitly mean no counters were returned and prevent
new drafting while availability is unconfirmed. Proposal rows require their
exact sequence identity, ordered tools and observation count; duplicated or
malformed rows are unavailable rather than confirmed empty.

A user can enter an exact intent of at most 8,000 UTF-8 bytes or select a current
sequence. Each draft action requires a connection-bound, single-use review
valid for 120 seconds. The review discloses exact input or proposal terms,
configured-model traffic/cost, AST-only checking, separate registration and
the deterministic-ID limitation. The intent is not silently trimmed. Input,
proposal, connection or expiry drift during preflight prevents POST.

Intent IDs mirror the engine's MD5 of `intent::` plus the first 200 Python code
points, truncated to 12 hexadecimal characters. The native implementation uses
Unicode scalars rather than Swift grapheme clusters. Sequence generation uses
`genesis_<sequence_id>`.

All known deterministic-ID replacement paths are disabled. Before review and
again immediately before generation, the expected ID must be absent from
generated inventory, pending drafts and loaded skills. An existing draft is
also blocked: the supported reads expose neither complete code nor a reliable
approved-state/CAS contract suitable for reviewing replacement. There is no
override that silently replaces a known approved or registered record.

After a successful POST, native Forge requires exact response identity and
preview shape, then reads pending and generated inventories. The pending
preview must equal the returned preview's first 400 code points; ordered
source tools, name and description must match inventory, and sequence-generated
name/description must also match the response. Only then does it report a
draft present in the unapproved queue. The returned 800-character preview is
retained for inspection. It does not claim durable restart recovery, full-code
safety, approval, live registration or action execution.

A lost/refused/malformed POST outcome or mismatched/missing readback becomes
`outcome_unknown`. The consumed review cannot retry. An uncertain same-ID
target remains blocked in this model even if a later inventory read is empty;
absence cannot establish that an in-flight generation did not later complete.
This refusal also survives connection-generation drift in the same model.
Late responses never publish into another connection. A different exact intent
can still be separately reviewed. No draft action sends approve, promote,
execute or fallback mutation requests.

## Actually performed checks

Shared layout source was frozen by its owner before the final compile. From
`ASOS/desktop-native`:

```sh
xcrun swiftc -swift-version 5 -target arm64-apple-macosx13.0 -module-cache-path /private/tmp/theora-native-feature-tests-cache -parse-as-library NativeRichText.swift NativePlainTextEditor.swift NativeReviewSummary.swift NativeCapabilitiesFeature.swift tests/NativeCapabilitiesFeatureTests.swift -o /private/tmp/feral-native-forge-parity-tests > /private/tmp/feral-native-forge-compile-20261002.log 2>&1
/private/tmp/feral-native-forge-parity-tests > /private/tmp/feral-native-forge-fixtures-20261002.log 2>&1
```

Compile and execution exited **0**; **86 assertions passed**. Existing registry,
registration, permission-preview, source identity and connection-drift checks
were retained. New fixtures cover supported local reads, measured/uninitialized/
malformed stats, backend-compatible Unicode IDs, exact intent and sequence
bodies, no generation-time approval/execution, single-use/expiry/input drift,
missing/foreign/changed/duplicate proposals, collision appearing during fresh
preflight, pending preview/readback, malformed success/ID/preview, missing
pending rows, uncertain POST/no retry, connection drift, and expiry/input drift
while awaiting fresh inventory.

An initial provisional harness run exposed reuse of one model across independent
unknown-outcome cases: its intended uncertain-ID fence correctly refused the
second case. Those independent fixtures now use separate disposable models;
the explicit same-model refusal regression remains. No production assertion
was weakened to release uncertain requests.

An additional actual-source integration probe used the registered router, real
ToolGenesisEngine, controlled no-network LLM and disposable SQLite, without
FastAPI lifespan or the runtime dispatcher. Approval/promotion/execution methods
were replaced with failing sentinels. The generated function raises if run.
The probe created two drafts, checked actual pending/inventory/statistics and
400/800 previews, loaded the disposable database, and checked empty-intent,
foreign-sequence and unavailable-service responses. From `ASOS/feral-core`:

```sh
../.venv/bin/python /private/tmp/feral-forge-route-probe.py > /private/tmp/feral-forge-route-contract-20261002.log 2>&1
```

Exit **0**; output `FORGE_ROUTE_CONTRACT_PASS`. The probe used no real provider,
account, registry, generated-code execution or personal storage. The existing
Pydantic field-shadow warning appeared; it did not prevent the check.
Targeted `git diff --check` exited 0. Full linked/typecheck and actual packaged
UI acceptance are parent-coordinated after the entire Swift source freezes.

## Remaining contract gaps

Fresh GET preflight is not atomic race protection. Another caller can create,
approve or replace a deterministic record between GET and POST. Neither route
accepts an expected revision, compare/create condition or durable operation ID.
The backend needs that contract before concurrent replacement can be supported
or a never-replace guarantee can be made. Known replacements are currently
disabled; preview-limited pending reads cannot substitute for complete code
review or an atomic server fence.

The uncertain-target refusal is native model memory, not a durable backend
operation receipt. Restarting the UI cannot reconstruct an in-flight generation
whose record has not yet appeared. A full recovery contract needs a durable
generation request identity/status and nonreplay semantics on the existing
server. This slice does not add another authorization or persistence store.

Pending preview comparison covers only the exposed 400 characters. Code beyond
that preview and concurrent changes remain unverified. Existing registration
reviews already disclose the truncated-code boundary. AST checks are not a
sandbox guarantee, and model calls may use the user's configured remote provider.

The native fixtures use URLProtocol, not real GUI clicks or model inference.
The actual-source probe verifies local route/engine/storage contracts with a
controlled LLM; it is not a real provider or full runtime acceptance result.
No full application build, signing, packaging or installation was performed.

## Frozen identities

| Source | SHA-256 |
| --- | --- |
| `desktop-native/NativeCapabilitiesFeature.swift` | `4f00455f137de4f719fb7f948efd8b13f7977a2c53374d323a399d7a1184d9a2` |
| `desktop-native/tests/NativeCapabilitiesFeatureTests.swift` | `12e304f3cceae6fcd49dc3499d731a20cf64da01f4c027aca42f6ed8802f2540` |
| Shared `desktop-native/NativeRichText.swift` during final direct compile | `aeaa41faa3eefe5fcc6577f65034f883dc2d4d6c7cb2e7498d935eb15c04f8c8` |
