# TYPE-01B: precise core annotations and keyword arguments

October 2, 2026. Implemented on source `739a20c0b56d2a2aa9956cde395a79b6dd5e0935`
plus this card's working-tree changes. These changes address five diagnostics
reported by Ubuntu CI in three files; a new full Ubuntu measurement is required
before claiming a reduction in the repository total.

- `observability/log_redaction.py`: pass typed logging keywords explicitly,
  preserving omission of `force` when it is unspecified.
- `memory/attachment_context.py`: annotate bounded attachment metadata with
  `str | int`, including the integer omitted-byte count. Authorization, content
  verification and truncation rules are unchanged.
- `memory/embeddings.py`: pass the cache-only constructor flag explicitly in its
  existing environment-controlled branch. The other branch still omits it.
  No model download or embedding weights were exercised.

No `Any`, cast, ignore or baseline adjustment was introduced.

Run from `feral-core` with the pinned interpreter:

```sh
FERAL_HOME=/private/tmp/feral-coding-integration-20261002/home FERAL_DATA_HOME=/private/tmp/feral-coding-integration-20261002/data ../.venv/bin/python -m pytest tests/test_log_redaction.py tests/test_attachment_model_context.py tests/test_embedding_model_cache_policy.py -q --no-cov -p no:randomly
../.venv/bin/python -m mypy --follow-imports=silent --cache-dir /private/tmp/feral-type-core-20261002 observability/log_redaction.py memory/attachment_context.py memory/embeddings.py
../.venv/bin/python -m ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 observability/log_redaction.py memory/attachment_context.py memory/embeddings.py
```

Results: **37 passed, six warnings in 1.72 seconds**; focused mypy reports no
issues in three files, with notes that untyped function bodies are not checked;
Ruff passes. Existing tests exercise redaction, attachment authorization/budgets
and cache-only embedding construction. They do not establish real embedding
quality or whole-repository type safety.

Latest prior full Ubuntu result at source739a is **852 errors in 241 files,
1,245 checked**, versus the unchanged 812-error baseline. The non-blocking job
remains failed. [Follow-up attribution](TYPE_RATCHET_FOLLOWUP.md) retains earlier
measurements; those are separate source identities.
