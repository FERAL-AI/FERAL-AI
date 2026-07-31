"""Turn a stale provider catalog into a red build.

Why this test exists
--------------------
``.github/workflows/provider-research.yml`` ran green every single day
from 2026-04-26 to 2026-07-30 while accomplishing nothing. The workflow
passed eight ``PROVIDER_RESEARCH_*_KEY`` secrets that had never been
created; ``scripts/research_providers.py`` treated a missing key as
"nothing to do" and returned ``None`` per provider; ``changes`` came
back empty; the script printed "no provider model lists changed" and
exited 0; and ``peter-evans/create-pull-request`` correctly opened no PR
because nothing had changed. Every layer behaved "correctly" and the
net effect was three months of invisible drift.

That is expensive, not cosmetic. ``cost/pricing.py`` reads ALL pricing
from ``model_catalog.json`` and is documented as the single source of
truth, so every cost calculation and every budget cap in the system was
running on April rates while frontier models shipped and prices moved.

``--require-keys`` (which CI now passes) closes the hole from the
workflow side. This test closes it from the repository side: even if
the workflow is disabled, deleted, or silently stops being scheduled,
a catalog that has not been refreshed in ~two weeks fails the build.
A green CI can no longer coexist with a stale catalog.

If this test fails
------------------
Do not bump ``last_fetched`` by hand. Run the refresher:

    python scripts/research_providers.py --require-keys

and commit the diff it produces. If it fails on missing keys, that is
the actual bug — the repository secrets do not exist.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from providers.catalog_data import catalog_path

# Two weeks. The refresher runs daily, so this tolerates ~13 consecutive
# failed or skipped runs before going red — long enough to absorb a
# holiday weekend plus a provider outage, short enough that a quarter of
# silent drift is impossible.
MAX_AGE_DAYS = 14


def _load_catalog() -> dict:
    path = catalog_path()
    assert path.is_file(), f"bundled model catalog missing at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_catalog_declares_last_fetched() -> None:
    """``last_fetched`` must exist and be a parseable ISO-8601 instant."""
    catalog = _load_catalog()
    raw = catalog.get("last_fetched")
    assert raw, (
        "model_catalog.json has no `last_fetched` timestamp. The refresh "
        "pipeline sets it on every run; without it staleness is "
        "unmeasurable and this guard is blind."
    )
    parsed = dt.datetime.fromisoformat(str(raw))
    assert parsed.tzinfo is not None, (
        f"`last_fetched` ({raw!r}) is timezone-naive. "
        "scripts/research_providers.py writes a UTC-aware timestamp; a "
        "naive value means it was hand-edited."
    )


def test_catalog_is_not_stale() -> None:
    """Fail when the catalog has not been refreshed in ~two weeks."""
    catalog = _load_catalog()
    fetched = dt.datetime.fromisoformat(str(catalog["last_fetched"]))
    age = dt.datetime.now(dt.timezone.utc) - fetched
    assert age <= dt.timedelta(days=MAX_AGE_DAYS), (
        f"providers/model_catalog.json was last refreshed {age.days} days "
        f"ago ({catalog['last_fetched']}), over the {MAX_AGE_DAYS}-day "
        "limit.\n\n"
        "cost/pricing.py reads ALL pricing from this file, so a stale "
        "catalog means every cost calculation and budget cap in the "
        "system is running on stale rates.\n\n"
        "Fix: run `python scripts/research_providers.py --require-keys` "
        "and commit the result. Do NOT hand-edit `last_fetched` — that "
        "silences the guard without refreshing anything. If the script "
        "fails on missing API keys, the PROVIDER_RESEARCH_* repository "
        "secrets do not exist; that is the bug to fix."
    )


def test_catalog_is_not_dated_in_the_future() -> None:
    """A future timestamp would silence the staleness check indefinitely."""
    catalog = _load_catalog()
    fetched = dt.datetime.fromisoformat(str(catalog["last_fetched"]))
    skew = fetched - dt.datetime.now(dt.timezone.utc)
    assert skew <= dt.timedelta(days=1), (
        f"`last_fetched` is {skew.days} days in the future "
        f"({catalog['last_fetched']}). That disables the staleness guard. "
        "Re-run the refresher instead of setting the timestamp forward."
    )


@pytest.mark.parametrize(
    "provider_id",
    ["openai", "anthropic", "gemini", "deepseek", "moonshot", "openrouter"],
)
def test_core_providers_carry_pricing(provider_id: str) -> None:
    """The providers we actually bill against must have priced models.

    ``openrouter`` is in this list because it spent its whole life with
    355 models and zero prices — every OpenRouter turn silently fell
    through to ``cost.pricing._FALLBACK_PER_1K`` and cost tracking was
    blind for that provider.
    """
    catalog = _load_catalog()
    entry = catalog["providers"][provider_id]
    priced = [k for k in (entry.get("pricing") or {}) if not k.startswith("_")]
    assert priced, (
        f"provider {provider_id!r} carries {len(entry.get('models') or [])} "
        "models and ZERO priced entries, so every call against it prices at "
        "the generic fallback rate. Populate `pricing` (the refresher does "
        "this automatically for openrouter)."
    )
