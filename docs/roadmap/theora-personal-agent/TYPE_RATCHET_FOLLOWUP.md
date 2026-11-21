# Ubuntu type ratchet follow-up

Read-only audit, October 2, 2026. No code or baseline was changed in this pass.
Source: **`3ecb95bbea0f3bea9b67b331341928e673a6acbe`**.
[CI run 37030468541, mypy job 110915704575](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37030468541/job/110915704575)
completed with failure. The parent workflow was still running when inspected;
the completed job log was retrieved directly through the GitHub job-logs API.

The actual log reports **851 errors in 241 files, 1,240 source files checked**
against the committed **812-error baseline**. The previous source's measured
859 errors fell by eight. No diagnostics remain in the extracted log for
`security/vault_initialization_api.py`, `security/vault_coordinator.py` or
`security/agent_bootstrap_continuation.py`. This confirms TYPE-01A on Ubuntu;
it does not mean the complete ratchet passed.

The generic Python SDK HTTP contract step also completed successfully in
job `110915777924`; its broader brain test step was still running. The green
TypeScript SDK job is for `feral-nodes/ts-node-sdk`, not the generic
`sdk/node` client being audited separately.

## Per-file count changes against the committed baseline

Counts were recomputed from actual `file.py:line: error:` diagnostic lines,
not estimated by subtracting the prior slice from the baseline. A count rise
alone does not establish that every diagnostic in that file was introduced
by the latest commit; compare the diagnostic/source contracts before editing.

| File | Baseline | Actual | Change |
|---|---:|---:|---:|
| `agents/multi_agent.py` | 1 | 4 | +3 |
| `agents/orchestrator.py` | 29 | 33 | +4 |
| `agents/proactive_engine.py` | 2 | 3 | +1 |
| `agents/ui_handlers.py` | 1 | 6 | +5 |
| `api/routes/channels.py` | 2 | 4 | +2 |
| `api/routes/mcp.py` | 0 | 2 | +2 |
| `api/routes/security_and_hardware.py` | 3 | 0 | -3 |
| `api/server.py` | 19 | 17 | -2 |
| `api/state.py` | 1 | 2 | +1 |
| `bridges/acp.py` | 2 | 4 | +2 |
| `config/loader.py` | 2 | 3 | +1 |
| `hardware/mesh.py` | 2 | 5 | +3 |
| `memory/attachment_context.py` | 0 | 1 | +1 |
| `memory/embeddings.py` | 0 | 3 | +3 |
| `observability/log_redaction.py` | 0 | 1 | +1 |
| `security/vault.py` | 1 | 2 | +1 |
| `skills/impl/places.py` | 0 | 1 | +1 |
| `tests/test_failover_endpoint_routing.py` | 0 | 10 | +10 |
| `tests/test_hr_pipeline_demo_fixes.py` | 0 | 1 | +1 |
| `tests/test_manifest_trigger_conditions.py` | 0 | 1 | +1 |
| `tests/test_proactive_freshness_gate.py` | 0 | 1 | +1 |
| `tests/test_the_agent_can_write_memory.py` | 0 | 1 | +1 |
| `tests/test_voice_realtime_headers.py` | 6 | 5 | -1 |

These changed-file deltas sum to **+39**, matching 851 versus 812.
Unchanged file counts are omitted from the table, not from the total.

## Proposed next bounded slices

No edits are authorized by this evidence document itself. Reserve exclusive
files and verify the actual contracts before implementing.

- `observability/log_redaction.py:175`, one `call-overload`: mixed
  `dict[str, object]` expanded into `logging.basicConfig`. Branch on optional
  `force` and pass typed explicit arguments; retain omitted-force behavior.
- `memory/attachment_context.py:85`, one `dict-item`: a list of inferred
  string-only dictionaries receives the omission count as an integer. Annotate
  the actual heterogeneous block contract without changing integrity checks,
  read authorization, size limits or serialized content.
- `memory/embeddings.py:1511`, three `arg-type` errors: expanding
  `dict[str, bool]` into `TextEmbedding` makes the checker treat arbitrary
  constructor keywords as booleans. Explicitly pass `local_files_only=True`
  only in the cache-only branch and omit it otherwise. Validate against the
  installed FastEmbed signature; do not download or initialize model weights.
- Alternative larger test-only slice: `tests/test_failover_endpoint_routing.py`
  lines 524 through 541 has ten diagnostics. Its fixture passes `None` for
  required collaborators, then reassigns an inferred None-only LLM field and
  methods, and accesses an AsyncMock recorder through the production Callable
  annotation. Use precise fixture collaborators, scoped monkeypatching and a
  retained recorder variable. Preserve error-frame/no-assistant-row assertions.

Remaining diagnostics require source attribution, rather than increasing the
baseline or using broad `Any`, casts or ignores. Parent integration owns the
selection, final commit and the next full-source Ubuntu measurement.
