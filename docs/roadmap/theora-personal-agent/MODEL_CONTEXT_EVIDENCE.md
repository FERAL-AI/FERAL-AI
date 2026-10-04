# MODEL01B: continued local conversations

October 2, 2026. Implemented against committed base
`4e19f07f8603196fd73ecdafd0e127dbad865e69`. Parent integration, publication and
subsequent packaged acceptance are separate from these source tests.

## Confirmed failure

Immutable native 9.27 exposed a healthy Ollama alias with checked context 16,384
tokens, but an ordinary subsequent request exceeded the independent 32,000-byte
input guard. Native evidence reported system 18,545, history 1,888, schemas
11,692 and total 32,146 UTF-8 bytes. Its saved UI transcript does not contain
the complete provider tool-call/result wire history.

Source investigation and disposable executable probes confirmed:

- `ContextManager.compact` returns histories of 15 or fewer rows without sizing.
  Its character/history-share planning budget excludes system and tool schemas.
- The local complete-input guard raised plain `ValueError`, outside typed
  context refusal handling. Actual stream orchestration delegated to nonstream,
  then direct execution incorrectly said no model was connected.
- Actual `LLMProvider` plus mock HTTP transport reproduced a configured cloud
  fallback receiving that byte-refused request. Typed Ollama context overflow
  already prevented such failover; the byte failure had not reused that contract.

No personal profile, real model inference, account or external action was used
in these source probes. Native 9.27's actual inference evidence remains in
[its acceptance report](NATIVE_9_27_ACCEPTANCE.md).

## Implemented behavior

The four local HTTP paths (direct chat, stream, routed primary and temporary
fallback client) now reuse one request preparation method. It keeps the existing
32,000-byte serialized `messages` plus `tools` ceiling and existing schema
count/byte ceilings. It does not enlarge local runtime allocation.

Request fitting uses an immutable wire view. It retains every exact system row,
the latest user and current tool rounds, and the nearest preceding completed
assistant/user turn. Only older whole turns can be omitted. A cut cannot orphan
an existing tool result from its announcing assistant call. Full saved history
and the orchestrator's working transcript remain unchanged.

If that protected view and selected schemas still do not fit, existing tool
retrieval can select fewer whole optional ranked definitions. Discovery tools,
forced tools, explicitly named tools and definitions used by the current tool
round remain mandatory, with full parameter schemas. The existing discovery
notice reports partial selection and points to the installed registry; missing
wire definitions do not revoke or invent execution authority. Fresh requests
select again, without persisting a reduced catalogue.

Ollama still checks actual running allocation or declared model `num_ctx` through
its native metadata endpoints. Complete serialized input uses the existing
heuristic estimator, plus requested output and the shared 256-token template
reserve. Optional selection obeys both byte and checked token budgets. An
allocation change during reselection is checked again before inference.

Mandatory context/schema failures are typed local refusals. They emit the exact
context/error code in normal and stream orchestration, without direct execution,
cloud failover or recording a false assistant response. Ordinary unrelated
provider failures retain their existing error paths.

## What the measured regression establishes

`calibrated_native_request` recreates the measured aggregate sizes using synthetic
prose, production row conventions and tool-call/result pairs. It is explicitly
**not** a capture of the original native provider request, and does not claim
identical per-row metadata or tokenization.

| Check | Fixture measurement |
|---|---:|
| Original aggregate input | 32,146 bytes |
| Protected view with all 12 selected schemas | 32,034 bytes |
| Budgeted view with 11 whole schemas | 29,886 bytes |
| Input estimate plus 256 output and 256 template reserve | 10,970 tokens |
| Checked mock Ollama allocation | 16,384 tokens |

Removing the old arithmetic exchange alone is insufficient. Optional schema
selection makes the protected request fit, while preserving discovery and the
explicit write definition. No write executes in this regression. All four
actual provider transport paths pass this fixture, and a fresh short request
can select all 12 schemas again.

Other tests exercise Unicode byte pressure, paired tool history, capacity-only
pressure below the byte ceiling, mandatory policy/latest-input/schema refusal,
changed allocation, and no cloud fallback. Actual normal and streaming
orchestrators each run a 16-turn synthetic conversation through the real provider
adapter with controlled HTTP responses; every request fits and every user prompt
remains in the stored transcript. Real external effects are absent.

## Verification

From `feral-core`, using the pinned repository interpreter and disposable homes:

```sh
../.venv/bin/python -c 'import os,tempfile,pytest; h=tempfile.TemporaryDirectory(prefix="feral-model01b-final-"); os.environ["FERAL_HOME"]=h.name; os.environ["FERAL_DATA_HOME"]=h.name; c=pytest.main(["tests/test_local_request_context.py","tests/test_local_tool_budget.py","tests/test_ollama_context_contract.py","tests/test_multi_turn_amnesia.py","tests/test_memory_context_per_turn.py","tests/test_direct_execution_contract.py","tests/test_chat_turn_failure_wire.py","tests/test_chat_turn_abort.py","tests/test_pending_approval_response.py","tests/test_stream_nonstream_parity.py","tests/test_turn_attribution.py","tests/test_token_estimate_never_undercounts.py","-q","--no-cov","-p","no:randomly","--tb=short"]); h.cleanup(); raise SystemExit(c)'
```

Result: **236 passed, 1 skipped, 7 warnings in 8.72 seconds**. The existing
live-tiktoken cross-check skips because `tiktoken` is absent from the pinned
environment; the fixture corpus estimation tests run. Warnings are framework
deprecations, the existing Pydantic field warning and the suite's environment
restoration notice. Targeted Ruff and owned-path whitespace checks passed.

Additional provider/failover/router regressions: **94 passed, 7 warnings in
1.84 seconds**, with the same disposable-home runner:

```sh
../.venv/bin/python -c 'import os,tempfile,pytest; h=tempfile.TemporaryDirectory(prefix="feral-model01b-provider-regression-"); os.environ["FERAL_HOME"]=h.name; os.environ["FERAL_DATA_HOME"]=h.name; c=pytest.main(["tests/test_llm_provider.py","tests/test_llm_failover.py","tests/test_llm_failover_chain_validation.py","tests/test_llm_router_w2.py","-q","--no-cov","-p","no:randomly","--tb=short"]); h.cleanup(); raise SystemExit(c)'
```

```sh
../.venv/bin/python -m ruff check agents/local_tool_budget.py agents/context_manager.py agents/llm_provider.py agents/orchestrator.py tests/test_local_tool_budget.py tests/test_ollama_context_contract.py tests/test_local_request_context.py --select E,F,W --ignore E501,E402,F401,W291,W293
git diff --check -- agents/local_tool_budget.py agents/context_manager.py agents/llm_provider.py agents/orchestrator.py tests/test_local_tool_budget.py tests/test_ollama_context_contract.py
```

## Boundaries and remaining acceptance

- The token estimate is heuristic, not the selected model's tokenizer. LM Studio
  retains the byte contract; this card adds no false checked allocation claim
  for it. Ollama metadata uncertainty keeps its previously configured fallback
  behavior; a known byte/schema/capacity refusal never enables that fallback.
- A protected minimum can still be too large. FERAL then refuses clearly rather
  than dropping the current task, abbreviating required schemas or raising limits.
- This is request-view selection, not provider-backed summarization or a new
  history/memory stack. The generic planning window remains distinct from the
  authoritative complete local request preflight.
- These source tests do not establish repaired packaged native 9.27 behavior.
  A new frozen bundle must repeat continued conversations, approval denial and
  subsequent approval execution with an actual healthy local model.
- Chat completion remains processing status, not verification that a requested
  real-world action succeeded. This card changes no policy, approval or task
  outcome receipt authority.
