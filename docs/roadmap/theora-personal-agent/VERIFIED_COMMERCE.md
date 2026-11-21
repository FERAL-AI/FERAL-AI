# Direct commerce verification — 2026-09-30

Publication note: checkout paths are portable placeholders. Set `EVIDENCE_ROOT` to a new disposable directory outside personal/app data before running the commands below, for example `export EVIDENCE_ROOT="$(mktemp -d)"`. Evidence filenames identify historical local outputs, not shipped archives or fresh reruns. `<theora-ios-checkout>` denotes the separate Theora iOS repository.

Executed real FERAL Python functions with a synthetic browser/merchant and throwaway SQLite ledgers. No real merchant/provider, browser, network connection, credentials, or purchase was used. This initial verification changed no project source. Storage was isolated by `FERAL_HOME`, `FERAL_DATA_HOME`, hash embeddings and TemporaryDirectory. The executable probe remains at `${EVIDENCE_ROOT:?}/theora-commerce-probe.py`.

## Commands

Working directory: `<checkout>/ASOS/feral-core`.

```sh
FERAL_HOME=${EVIDENCE_ROOT:?}/theora-commerce-probe-home FERAL_DATA_HOME=${EVIDENCE_ROOT:?}/theora-commerce-probe-home/data FERAL_EMBED_PROVIDER=hash PYTHONPATH=. ../.venv/bin/python ${EVIDENCE_ROOT:?}/theora-commerce-probe.py
FERAL_HOME=${EVIDENCE_ROOT:?}/theora-commerce-probe-home FERAL_DATA_HOME=${EVIDENCE_ROOT:?}/theora-commerce-probe-home/data FERAL_EMBED_PROVIDER=hash ../.venv/bin/python -m pytest tests/test_spend_caps.py -q --no-cov -p no:randomly --tb=short
```

## Probe result: exit 0; all four assertions reproduced

```text
purchase audit read failed: no such table: purchases
purchase audit write failed: no such table: purchases
PREVIEW {"purchased": false, "awaiting_confirmation": true, "amount": "24.00", "detected_prices": ["$24.00", "$900.00"], "calls": ["navigate", "wait", "get_page_info", "evaluate_price_candidates", "screenshot"], "audit": [["offered", "24.00"]]}
CARD_DISPATCH {"action_ids": ["confirm_3e7dcd45_yes", "confirm_3e7dcd45_no"], "executions": [], "commands": [], "text_responses": [], "pending": {}}
LEDGER_READ_FAILURE {"before": {"allowed": false, "code": "over_per_day", "spent_today": "90"}, "after": {"allowed": true, "code": "within_limits", "spent_today": "0"}, "fault": "renamed purchases table in throwaway database"}
LEDGER_WRITE_FAILURE {"record_raised": false, "persisted_original_rows": [["completed", "90"]]}
RESULT: all 4 synthetic probes reproduced; no external operations
```

Card IDs are random per execution. The shown IDs came from the recorded run.

1. **Verified preview behavior / architecture gap:** actual `WebActionsSkill.make_purchase` chooses first `$24` string when returned candidates also contain `$900`; it only navigates, waits, reads page info, evaluates prices and screenshots. It writes an `offered` record and returns `purchased:false`. This is not a checkout/order/payment implementation. The synthetic test proves first-candidate selection, not that any live merchant charged an incorrect price. No cart, tax or shipping total verification was performed.
2. **Verified nonfunctional generated card:** dispatching both generated Confirm and Cancel IDs through actual `agents.ui_handlers.handle_ui_event` with no pending confirmation results in no execution, no model command and no user response. The `confirm_` branch returns even without a matching pending item. This corrects earlier research wording claiming unmatched card IDs could reach the model fallback: they are silently ignored in this branch.
3. **Verified fail-open cap defect:** healthy ledger has a synthetic completed `$90`, so proposed `$20` under a `$100` daily cap is refused. Renaming the purchases table in the isolated ledger induces an actual SQLite read failure; `spent_today` returns zero and `evaluate` allows the same proposal. No payment was attempted.
4. **Verified silent audit-write defect:** `PurchaseAudit.record` against the unavailable table logs a failure and returns without raising. Original retained ledger still contains only the `$90` row; proposed additional record is absent. This can mislead callers about persistence but does not prove loss of any live purchase record.

Source: `feral-core/security/commerce.py` (`PurchaseAudit.record`, `spent_today`, `evaluate`), `feral-core/skills/impl/web_actions.py` (`make_purchase`, `_build_confirmation_card`), `feral-core/agents/ui_handlers.py` (`handle_ui_event`).

## Existing regression baseline

`tests/test_spend_caps.py`: **24 passed, 5 warnings in 4.39 seconds**, exit 0. Warnings concern Pydantic schema naming and FastAPI deprecated startup/shutdown handlers. The current suite tests healthy cap decisions and preview refusal/offer behavior; it did not catch the induced ledger failure or card no-op.

No release, transaction engine, live merchant, medical, relay or iOS correctness claim follows from these probes. Repository status showed only the pre-existing untracked roadmap directory; no source was changed during initial verification.

## Bounded correction after reproduction

The parent agent subsequently authorized fixing the two verified ledger defects only. Changed `feral-core/security/commerce.py` to introduce `PurchaseAuditUnavailable` for failed writes/reads; healthy method return values stay unchanged. `evaluate` catches the unavailable-ledger signal and returns a refusal with code `audit_unavailable`, unknown spending (`None`), and a clear reason. Failed `record` raises instead of reporting implicit success; the existing skill execution wrapper reports failure and cannot return a confirmation card.

Added three meaningful regression tests in `tests/test_spend_caps.py`:

- Healthy `$450 AED` ledger denies `$100` against `$500` cap; actual table-unavailable read remains denied and direct read raises, instead of becoming zero.
- Actual table-unavailable write raises and preserves original ledger contents; healthy write still returns None.
- Real SQLite trigger rejects only `offered` insertion while reads work. Actual `WebActionsSkill.execute` reports failure, returns no data/card, and does not screenshot or persist an offer.

Re-ran the same pytest command: **27 passed, 5 warnings in 0.75 seconds**, exit 0. `git diff --check` passed. `.venv/bin/python -m ruff check feral-core/security/commerce.py feral-core/tests/test_spend_caps.py` returned **All checks passed!**. The initial reproducer intentionally expects the original faulty behavior, so it is a retained pre-fix artifact, not a post-fix pass test.

Diff at verification: two tracked files, 82 insertions / 5 deletions. No transaction engine, UI/card fix, cloud integration, or purchase completion was introduced. The card no-op and non-authoritative quote selection remain verified unresolved gaps.

## Follow-up: truthful purchase preview

The parent subsequently authorized removing nonfunctional purchase confirmation controls without adding checkout. Updated `skills/impl/web_actions.py`, `skills/manifests/web_actions.json`, `tests/test_web_actions.py`, and the existing spend regression expectation. Generated purchase SDUI now has **no Button nodes or action IDs**, states preview-only/no purchase made/checkout unavailable, and labels the observed price as unverified with taxes/shipping/items/variants unverified. Return state is `awaiting_confirmation:false`, `preview_only:true`, `checkout_available:false`, `price_verified:false`. Renamed misleading `total_display` to `observed_price_display`; repository source search found no consuming runtime client for that field. Parsed amount remains an observed price for screening, not payment authority. Existing over-cap/no-cap/unreadable refusal behavior remains.

Regression uses two returned price candidates and verifies no orphan controls, no pending-approval claim, honest price label and no form fill. Existing booking/reservation helper was not changed by this bounded purchase correction.

```sh
FERAL_HOME=${EVIDENCE_ROOT:?}/theora-commerce-probe-home FERAL_DATA_HOME=${EVIDENCE_ROOT:?}/theora-commerce-probe-home/data FERAL_EMBED_PROVIDER=hash ../.venv/bin/python -m pytest tests/test_web_actions.py tests/test_spend_caps.py tests/test_app_action_dispatch.py tests/test_approval_reaches_the_wearer.py -q --no-cov -p no:randomly --tb=short
```

Result: **61 passed, 5 existing warnings in 1.22 seconds**, exit 0. Ruff on the four changed Python files passed; diff whitespace check passed. No payment completion was introduced. Remaining architecture gap: observed price extraction does not establish an actual checkout total, and the tool now says so explicitly.
