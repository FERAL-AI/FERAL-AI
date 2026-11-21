# Receipt type follow-up

October 2, 2026. Parent repair of four attributed diagnostics from the completed
Ubuntu mypy job110942696634 at source4e19f07f. That full run failed with850 errors
against baseline812. These changes do not raise or regenerate the baseline.

`MemoryStore.chat_turn_claim` rejects a missing count row inside its transaction
rather than indexing None. Interrupted-receipt recovery materializes fetched
rows before counting. Gateway `chat.abort` validates both identifiers with the
same canonical UUID validator before calling the typed manager. Missing or
invalid identifiers retain the existing `chat_turn_invalid_request` refusal;
the manager still checks exact session/socket ownership. No cast or ignore hides
an invalid input.

Parent checks:51 memory/receipt tests passed; after the gateway validation
change,21 abort/gateway tests passed with six existing dependency warnings.
Focused mypy has no diagnostics at these four repaired locations. It still
fails with existing store/workspace diagnostics and eight gateway diagnostics
(seven optional defaults plus missing YAML stubs). Whole-file and full Ubuntu
type gates are not claimed clean. The next commit needs its own remote count.

All runtime tests use disposable home/data and controlled execution. No real
account or external action was exercised.
