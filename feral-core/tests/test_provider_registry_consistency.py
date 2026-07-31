"""Lock FERAL's three provider registries to each other.

The three registries
--------------------
1. ``agents/llm_provider.py::SUPPORTED_RUNTIME_PROVIDERS`` — the ids the
   brain can actually dispatch a chat turn against (derived from
   ``_PROVIDER_REGISTRY`` plus the local/pseudo runtimes).
2. ``providers/catalog.py::BUILT_IN_DESCRIPTORS`` — what the setup
   wizard and the v2 Settings picker render.
3. ``providers/model_catalog.json`` — the bundled model + pricing data.

Nothing structural keeps them in sync, and by 2026-07-30 all three
failure modes were live simultaneously:

* ``kimi`` and ``qwen`` had a runtime binding but NO descriptor, so a
  user could not select them in the setup wizard at all — the brain
  could dispatch to a provider the UI refused to offer.
* ``xai`` had catalog data but NO runtime binding, so the picker
  advertised Grok models that would fail at request time.
* The base URLs for ``kimi`` drifted between the registry tuple
  (``api.moonshot.cn``) and reality (``api.moonshot.ai``).

Each of those is invisible in review — you have to notice an absence in
a file you are not editing. This test makes each one a named failure.

Deliberate exceptions are enumerated below rather than tolerated by a
loose assertion, so adding one is a reviewable act.
"""

from __future__ import annotations

import pytest

from agents.llm_provider import (
    _CATALOG_PROVIDER_MAP,
    _PROVIDER_REGISTRY,
    SUPPORTED_RUNTIME_PROVIDERS,
)
from providers.catalog import BUILT_IN_DESCRIPTORS
from providers.catalog_data import provider_ids

# ── Allowlists ───────────────────────────────────────────────────────

#: Runtime ids that are not HTTP providers at all and therefore have no
#: descriptor and no catalog data: on-device inference and the
#: local+cloud splitter.
PSEUDO_RUNTIMES = frozenset({"local", "hybrid"})

#: Descriptors the catalog advertises but the runtime cannot dispatch
#: to. These reach chat through the catalog adapter / route_call path
#: rather than the httpx OpenAI-compat path in ``llm_provider``, which
#: is why they are absent from ``SUPPORTED_RUNTIME_PROVIDERS``. See the
#: bedrock descriptor's own notes field.
CATALOG_ONLY_DESCRIPTORS = frozenset({"bedrock", "together", "fireworks"})

#: Providers whose runtime base URL intentionally differs from the
#: descriptor's. ``gemini`` is the only one: the httpx runtime path
#: speaks Google's OpenAI-compatibility shim
#: (``/v1beta/openai``) while ``GeminiProvider`` and the picker use the
#: native ``/v1beta`` surface. Two different APIs on one host, both
#: correct for their caller — not drift.
BASE_URL_DIVERGENCE_ALLOWED = frozenset({"gemini"})

#: Catalog id ↔ runtime id, for the one provider whose names differ.
#: The catalog says ``moonshot``; the runtime historically says
#: ``kimi``. Mirrors ``_CATALOG_PROVIDER_MAP``.
RUNTIME_TO_CATALOG = dict(_CATALOG_PROVIDER_MAP)


def _descriptor_ids() -> set[str]:
    return {d.provider_id for d in BUILT_IN_DESCRIPTORS}


def _runtime_ids() -> set[str]:
    return set(SUPPORTED_RUNTIME_PROVIDERS) - PSEUDO_RUNTIMES


def _runtime_as_catalog(pid: str) -> str:
    return RUNTIME_TO_CATALOG.get(pid, pid)


# ── The three pairwise consistency checks ────────────────────────────


def test_every_runtime_provider_has_a_descriptor() -> None:
    """A dispatchable provider the UI cannot offer is unreachable.

    This is the ``kimi`` / ``qwen`` bug: both were in
    ``_PROVIDER_REGISTRY`` for their entire lifetime with no descriptor,
    so ``ProviderCatalog`` never advertised them and neither the setup
    wizard nor the v2 picker could select them.
    """
    descriptors = _descriptor_ids()
    missing = sorted(
        pid for pid in _runtime_ids() if _runtime_as_catalog(pid) not in descriptors
    )
    assert not missing, (
        f"runtime providers with no ProviderDescriptor: {missing}. "
        "The brain can dispatch to them but the setup wizard and v2 "
        "Settings picker cannot offer them, so a user can never select "
        "them. Add a descriptor to providers/catalog.py::"
        "BUILT_IN_DESCRIPTORS (and an entry to _CATALOG_PROVIDER_MAP if "
        "the catalog spells the id differently)."
    )


def test_every_descriptor_has_a_runtime_binding_or_is_allowlisted() -> None:
    """A descriptor with no runtime binding advertises a dead end.

    This is the ``xai`` bug: catalog data and (later) a descriptor
    existed, but ``_PROVIDER_REGISTRY`` had no entry, so selecting it
    produced a provider the brain could not dial.
    """
    runtime = {_runtime_as_catalog(pid) for pid in _runtime_ids()}
    orphaned = sorted(_descriptor_ids() - runtime - CATALOG_ONLY_DESCRIPTORS)
    assert not orphaned, (
        f"descriptors with no runtime binding: {orphaned}. The picker "
        "will offer these but the brain cannot dispatch to them. Either "
        "add them to agents/llm_provider.py::_PROVIDER_REGISTRY (cheap "
        "when the provider is OpenAI-compatible) or add them to "
        "CATALOG_ONLY_DESCRIPTORS in this test with a reason."
    )


def test_every_descriptor_has_catalog_data() -> None:
    """Descriptors without catalog data have no models and no pricing."""
    missing = sorted(_descriptor_ids() - provider_ids())
    assert not missing, (
        f"descriptors with no entry in model_catalog.json: {missing}. "
        "Without one, list_models() falls back to an empty list and every "
        "call prices at cost.pricing._FALLBACK_PER_1K. Add a provider "
        "entry (an endpoint-less stub with a note is fine for "
        "account-scoped or local-only providers)."
    )


def test_every_catalog_provider_has_a_descriptor() -> None:
    """Catalog data for a provider nothing can render is dead weight."""
    orphaned = sorted(provider_ids() - _descriptor_ids())
    assert not orphaned, (
        f"model_catalog.json carries data for providers with no "
        f"descriptor: {orphaned}. Nothing renders or dispatches them. "
        "Either add a descriptor or drop the catalog entry."
    )


# ── Shape checks on the registry entries themselves ──────────────────


def test_registry_and_descriptor_base_urls_agree() -> None:
    """The two base URLs for one provider must not drift apart.

    ``kimi`` shipped with ``api.moonshot.cn`` in ``_PROVIDER_REGISTRY``
    while the correct API host is ``api.moonshot.ai`` — the sort of
    divergence that only shows up as a runtime 404.
    """
    by_id = {d.provider_id: d for d in BUILT_IN_DESCRIPTORS}
    mismatches: list[str] = []
    for pid, (base_url, _env) in _PROVIDER_REGISTRY.items():
        if pid in BASE_URL_DIVERGENCE_ALLOWED:
            continue
        desc = by_id.get(_runtime_as_catalog(pid))
        if desc is None or not desc.default_base_url:
            continue
        if desc.default_base_url.rstrip("/") != base_url.rstrip("/"):
            mismatches.append(
                f"{pid}: registry={base_url!r} vs "
                f"descriptor={desc.default_base_url!r}"
            )
    assert not mismatches, (
        "base URL drift between agents.llm_provider._PROVIDER_REGISTRY and "
        "providers.catalog.BUILT_IN_DESCRIPTORS:\n  - "
        + "\n  - ".join(mismatches)
    )


@pytest.mark.parametrize("descriptor", BUILT_IN_DESCRIPTORS, ids=lambda d: d.provider_id)
def test_cloud_descriptors_do_not_hardcode_a_default_model(descriptor) -> None:
    """Roadmap §3.5 P0: no hardcoded model literals that go stale.

    ``default_model`` must stay empty so ``default_model_for`` resolves
    it from the live/bundled catalog. A literal here is exactly the
    class of bug that shipped ``gpt-4o-mini`` to production long after
    it stopped being a sensible default.
    """
    assert descriptor.default_model == "", (
        f"{descriptor.provider_id} descriptor hardcodes "
        f"default_model={descriptor.default_model!r}. Leave it empty; the "
        "catalog resolves the default lazily through "
        "ProviderCatalog.default_model_for()."
    )


@pytest.mark.parametrize("descriptor", BUILT_IN_DESCRIPTORS, ids=lambda d: d.provider_id)
def test_key_requiring_descriptors_name_an_env_var(descriptor) -> None:
    """A provider that needs a key must say which env var holds it."""
    if not descriptor.requires_api_key:
        return
    assert descriptor.credential_env_var, (
        f"{descriptor.provider_id} requires an API key but names no "
        "credential_env_var, so the wizard cannot tell the operator what "
        "to set and _is_configured() can never return True."
    )
