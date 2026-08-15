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

``cost/pricing.py`` reads ALL pricing from ``model_catalog.json`` and is
documented as the single source of truth, so a stale catalog means the
system is quoting old rates while frontier models ship and prices move.

How much that costs, precisely
------------------------------
This docstring used to say "every cost calculation and every budget cap
in the system was running on April rates", and the failure message said
the same. That overstates it, and the overstatement is why this test read
as an emergency for something that is usually cosmetic.

``cost/budget.py:44`` ships the budget UNLIMITED by default, deliberately,
since v2026.5.47. Until an operator types a number into Settings, or sets
``cost.global_per_hour_usd``, ``_cap_for`` returns ``None`` for every
call site and window and ``BudgetLoopGuard.allow()`` always passes. And
the cost-ordered provider routing in ``agents/llm_provider.py`` only
reorders candidates when budget headroom is tight, which cannot happen
without a cap.

So on a default install, prices drive one thing: the "you used N tokens,
that cost about $X" figure. Stale rates make that figure slightly wrong.
Nothing is gated, throttled or refused.

The moment an operator sets a cap, the same numbers start deciding what
runs, and being wrong in the cheap direction means sailing past a limit
they asked for. That is the real risk, and it is real, but it belongs to
installs that opted in rather than to every build.

Hence two thresholds instead of one. Past ``WARN_AFTER_DAYS`` the catalog
is stale and this file says so loudly. Past ``MAX_AGE_DAYS`` it is stale
enough that the displayed figure is likely wrong for models that did not
exist when it was written, and the build fails. A permanently red build
that everyone learns to ignore protects nothing.

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
import warnings

import pytest

from providers.catalog_data import catalog_path

# Two weeks. The refresher runs daily, so this tolerates ~13 consecutive
# failed or skipped runs before it is worth saying something: long enough
# to absorb a holiday weekend plus a provider outage.
WARN_AFTER_DAYS = 14

# Six weeks. Past this the catalog predates whole model generations and
# the cost figure shown to the operator is wrong for anything launched
# since, so the build fails.
#
# Not 14, because at 14 days the only consequence on a default install is
# a slightly wrong number on a usage screen (see the module docstring),
# and a build that is red for six weeks over a display value is a build
# nobody reads. The guard the repo actually needed was against a quarter
# of silent drift, which this still catches with five weeks to spare.
MAX_AGE_DAYS = 42


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


def _catalog_age() -> dt.timedelta:
    catalog = _load_catalog()
    fetched = dt.datetime.fromisoformat(str(catalog["last_fetched"]))
    return dt.datetime.now(dt.timezone.utc) - fetched


_REFRESH_INSTRUCTIONS = (
    "Fix: run `python scripts/research_providers.py --require-keys` and "
    "commit the result. Do NOT hand-edit `last_fetched`, that silences the "
    "guard without refreshing anything. If the script fails on missing API "
    "keys, the PROVIDER_RESEARCH_* repository secrets do not exist; that is "
    "the bug to fix."
)


def test_catalog_staleness_is_reported() -> None:
    # No `recwarn` fixture here on purpose. Requesting it captures warnings
    # raised in the test instead of letting them propagate, so the warning
    # would never reach the run summary and this guard would report
    # staleness to nobody. That is the exact failure it exists to prevent.
    """Warn, without failing, once the catalog passes WARN_AFTER_DAYS.

    Visible on every run rather than only when someone opens CI, and it
    does not turn the build red for a figure that is display-only until an
    operator sets a spending cap.
    """
    age = _catalog_age()
    if age <= dt.timedelta(days=WARN_AFTER_DAYS):
        return

    warnings.warn(
        f"providers/model_catalog.json is {age.days} days old, past the "
        f"{WARN_AFTER_DAYS}-day refresh window. On a default install this "
        "only skews the reported token cost, since cost/budget.py ships "
        "unlimited and nothing is gated until an operator sets a cap. If a "
        f"cap IS set, stale rates can let spend past it. Build fails at "
        f"{MAX_AGE_DAYS} days.\n\n" + _REFRESH_INSTRUCTIONS,
        stacklevel=1,
    )


def test_catalog_is_not_hopelessly_stale() -> None:
    """Fail once the catalog predates whole model generations."""
    age = _catalog_age()
    assert age <= dt.timedelta(days=MAX_AGE_DAYS), (
        f"providers/model_catalog.json was last refreshed {age.days} days "
        f"ago, over the {MAX_AGE_DAYS}-day hard limit.\n\n"
        "cost/pricing.py reads ALL pricing from this file. At this age it "
        "predates model generations, so the cost reported for anything "
        "launched since is wrong, and any spending cap an operator has set "
        "is being enforced against rates that no longer exist.\n\n"
        + _REFRESH_INSTRUCTIONS
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
