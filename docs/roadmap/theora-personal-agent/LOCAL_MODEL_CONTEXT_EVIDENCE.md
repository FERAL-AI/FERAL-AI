# Local model context capacity contract

MODEL-01A, October 2, 2026. This worker slice changes only
`feral-core/agents/llm_provider.py`, `agents/context_manager.py`,
`providers/ollama_provider.py`, the new `tests/test_ollama_context_contract.py`,
and the Ollama metadata fixture in `tests/test_local_tool_budget.py`.
Parent integration owns the commit, rebuilt app and actual native acceptance.
The comparison source is `6b368ccf79c8b581bf2f705ed23e97095899f49d`.

## Problem and observed evidence

The parent supplied actual native acceptance evidence from immutable build
9.26: the installed `qwen3:4b` runtime logged a 6,891-token prompt truncated to
2,050 tokens at `n_ctx=4096`. Its 1,024-token output allowance was consumed by
reasoning without final content, leaving the native chat thinking/retrying.
The parent's separate disposable app with an explicitly configured 16k
`qwen2.5:3b` alias visibly answered `42`. Those are parent-observed runs, not
new inference performed by this worker.

Source inspection found the router uses Ollama's OpenAI-compatible
`/v1/chat/completions` transport. Its generic planning declaration previously
fell back to 128k even when the actual allocation was 4k. The existing local
request byte guard limits serialization size; it does not prove token fit.

This worker actually read the installed Ollama CLI version: **0.35.0**. The
default daemon was unreachable. A read-only query to the isolated acceptance
daemon at port 11436 returned **`{"models":[]}`**. No model inference, download,
warm-up, daemon configuration or user profile write was performed here.

## Primary documentation and installed-version source

- Ollama's OpenAI endpoint exposes no context-allocation field. Its documented
  way to change context is an explicit model configuration. The existing
  OpenAI transport is therefore preserved, without adding unsupported
  `options.num_ctx` to its request body.
  [OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility),
  [v0.35.0 OpenAI request translation](https://raw.githubusercontent.com/ollama/ollama/v0.35.0/openai/openai.go).
- Running-model metadata reports `context_length`; model details separately
  report parameter text and trained architecture metadata. A trained ceiling
  is not evidence of an allocated inference window.
  [Running models](https://docs.ollama.com/api/ps),
  [Model details](https://docs.ollama.com/api-reference/show-model-details).
- Default allocation depends on hardware and increasing context consumes more
  memory. FERAL does not adopt the documentation's larger agent context
  recommendation automatically. Runtime options begin with defaults and model
  configuration before request options are applied.
  [Context length](https://docs.ollama.com/context-length),
  [v0.35.0 server option resolution](https://raw.githubusercontent.com/ollama/ollama/v0.35.0/server/routes.go).
- The native Ollama API accepts generation options and distinguishes thinking
  from final message content. This card preserves the native API and bounds
  its output with `num_predict`; handling thinking-only completion is separate.
  [Native chat API](https://docs.ollama.com/api/chat).

The links establish API behavior from documentation/source. They are not a
claim that every host, model or configured proxy was tested against that API.

## Implemented contract

1. Every Ollama inference path first reads the selected endpoint's `/api/ps`,
   with a five-second metadata timeout. A selected-model allocation is preferred
   over any model parameter. An unrelated loaded model cannot certify capacity.
2. Without a usable matching allocation, `/api/show` must expose an explicit,
   bounded positive `num_ctx` parameter. Trained `model_info` context metadata
   is deliberately ignored. Missing or malformed evidence refuses inference.
3. An explicit `FERAL_CONTEXT_WINDOW_TOKENS` must be an integer from 1 through
   1,048,576. It may lower the budget, but cannot certify capacity above the
   observed allocation/model parameter. No settings are rewritten.
4. After existing retrieval and byte validation, a heuristic estimate covers
   the full serialized messages and tool schemas, requested output allowance,
   and a 256-token template reserve. An estimate above the checked window
   refuses inference without truncating the request.
5. Ordinary chat, streaming chat, the routed primary, temporary Ollama fallback
   clients and the separate native adapter all enforce the contract. The
   native adapter now explicitly defaults to `num_predict=1024` so an omitted
   output argument cannot invalidate its reserved-output check.
6. Router planning uses only one ephemeral endpoint/model-bound observation
   for up to 30 seconds. A changed endpoint/model or stale observation cannot
   inherit another model's capacity. Actual inference always rechecks metadata.
   With no observation, 4,096 is a conservative planning fallback, not a claim
   that this allocation was verified. Explicit declarations remain subject to
   inference preflight.

HTTP metadata uses the existing selected client's endpoint and headers,
preserving a proxy prefix such as `/proxy/v1` to `/proxy/api/ps`. Logs report
aggregate estimates/capacity only, without prompt content, tokens, server
bodies or credential-bearing URLs.

## Error and configured-failover behavior

| Error code | Meaning | Result |
|---|---|---|
| `local_context_configuration` | Invalid declared context or output allowance | Refuse with an actionable configuration error |
| `local_context_unverified` | Metadata unavailable, malformed, or no selected capacity evidence | Refuse local inference; an already configured failover chain may proceed |
| `local_context_mismatch` | FERAL declares more than the selected runtime exposes | Refuse without a retry or failover |
| `local_context_overflow` | Estimated complete request plus output exceeds the checked budget | Refuse without truncation, retry or failover |

Chat returns an error object with `error`, `error_code` and empty `choices`;
streaming yields an explicit error event. The native provider raises the typed
`OllamaContextRefusal`. These are failures, not fabricated task success.

The existing router policy already stops failover on proven context overflow.
This card preserves that policy and preserves operator-configured candidates
when capacity is merely unknown. It never introduces a provider, changes the
selected model, sends a rejected local inference again, downloads weights or
allocates a larger window. A local-only configuration stops on refusal.

## Verification actually performed

From `feral-core`, with the existing repository environment and disposable homes:

```sh
FERAL_HOME=/private/tmp/feral-context-contract-home FERAL_DATA_HOME=/private/tmp/feral-context-contract-data ../.venv/bin/python -m pytest tests/test_ollama_context_contract.py tests/test_local_tool_budget.py tests/test_llm_provider.py tests/test_llm_local_timeouts.py tests/test_providers.py tests/test_llm_failover.py tests/test_llm_base_url_coherence.py tests/test_switch_provider_base_url.py tests/test_silent_degradation_is_loud.py -q --no-cov -p no:randomly
```

Result: **202 passed, 7 warnings in 5.75 seconds**. New tests inspect actual
outgoing HTTPX request paths, hosts and bodies with `MockTransport`, rather than
mocking the verification helper. They exercise OpenAI/native transport parity,
loaded and cold capacity, reallocated windows, schemas/output accounting,
model/endpoint identity, malformed responses/timeouts, error redaction and
configured failover. No model tokens were generated by these tests. After the
final wording-only error clarification, the dedicated new suite was rerun:
**30 passed, 6 warnings in 0.95 seconds**.

The existing switch-provider fixtures attempted calls to `api.openai.com`,
`api.anthropic.com` and `custom-gateway.example.com`; the repository network
guard blocked and reported them. This is not evidence of real provider access.
Warnings also include existing model/lifespan deprecations and restored
test-process environment changes.

Ruff passed all five owned Python files with the repository's
`E,F,W` selection and `E501,E402,F401,W291,W293` exclusions. The owned-file
diff whitespace check passed.

The existing CI Architecture boundaries script also passed locally across
**32 skill implementations**. Its rule checks skill imports, not provider
imports. Dependency inspection found `agents/__init__.py` empty and the shared
context helper limited to stdlib, HTTPX and the lightweight token estimator;
it does not import the LLM runtime, API or memory subsystem. No boundary
suppression or new import-rule exemption was added.

Focused mypy with `--follow-imports=silent` reports no issues in
`agents/context_manager.py` and `providers/ollama_provider.py`. Including
`agents/llm_provider.py` reports **23 existing errors in that file**. A read-only
copy of the exact comparison source, checked in the same macOS environment,
reports the same 23 diagnostic messages/codes at its earlier line positions.
No type baseline, ignore or blanket cast was added. This local comparison is
not the complete Ubuntu CI ratchet; the parent must record that separately.

## Remaining acceptance gates

- Cold models without explicit allocation evidence now stop clearly. The next
  native activation/preparation card must make selecting/preparing a suitable
  model understandable. This correctness fix alone does not certify the app
  as ready for ordinary users.
- The estimate is not the model's exact tokenizer. Template reserve is a
  heuristic, and server allocation can change between metadata and inference.
  Real hardware/model acceptance must check for server truncation and final
  content; this preflight is not a mathematical proof of token fit.
- Thinking-only output, final-content absence, and appropriate completion
  status remain a separate card. This change does not label reasoning as a
  successful final answer.
- Rebuild the app and repeat real cold/warm, small-window, configured-context,
  cancellation and final-answer acceptance. Record the new immutable source,
  app identity, selected runtime allocation and actual result.
- Publish only after parent integration checks. This worker did not modify
  orchestrator/native code, user profiles, global configuration, real accounts
  or previously frozen SDK/security files.
