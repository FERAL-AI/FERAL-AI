"""Lock ``_PROVIDER_ENV_KEYS`` (security layer) to ``_PROVIDER_REGISTRY``
(runtime LLM layer).

Why this test exists
--------------------

``security/vault_keys.py`` maintains its own per-provider env var table
(``_PROVIDER_ENV_KEYS``) instead of importing from
``agents/llm_provider.py``. That split is deliberate — the security
layer never imports the LLM runtime so it cannot accidentally form a
cycle, see the docstring at ``vault_keys._PROVIDER_ENV_KEYS``. The cost
of the split is that when a new runtime provider lands in
``_PROVIDER_REGISTRY`` (e.g. a future "xai" / "perplexity" / etc.) the
env-key map drifts silently and ``get_active_key(new_id)`` falls
through to ``""`` even when ``XAI_API_KEY`` is set in the operator's
shell.

This regression test enforces that:

1. every provider id in ``_PROVIDER_REGISTRY`` that has a non-empty env
   var name has a matching entry in ``_PROVIDER_ENV_KEYS``;
2. every entry in ``_PROVIDER_ENV_KEYS`` has a matching provider id in
   ``_PROVIDER_REGISTRY``;
3. the env var name is identical between the two tables.

The follow-up risk this guards against is documented in
``AUDIT-r14/round3/findings/lane4-vault-keys-hot-path.md`` (the
v2026.5.42 follow-up note next to the cross-cut #1 cutover).
"""
from __future__ import annotations

import pytest

from agents.llm_provider import _PROVIDER_REGISTRY
from security.vault_keys import _PROVIDER_ENV_KEYS


def _registry_env_vars() -> dict[str, str]:
    """Project ``_PROVIDER_REGISTRY`` onto ``{provider_id: env_var}``.

    Providers with an empty env-var slot (``lmstudio`` ships no
    credential — it talks to localhost) are skipped: the security
    layer has nothing to fall back to for them, so excluding such
    providers from ``_PROVIDER_ENV_KEYS`` is the right design and
    must not be flagged as drift.
    """
    return {pid: env for pid, (_base, env) in _PROVIDER_REGISTRY.items() if env}


def test_every_registry_provider_has_env_key_mapping() -> None:
    registry = _registry_env_vars()
    missing = sorted(set(registry) - set(_PROVIDER_ENV_KEYS))
    assert not missing, (
        f"providers present in agents.llm_provider._PROVIDER_REGISTRY but "
        f"missing from security.vault_keys._PROVIDER_ENV_KEYS: {missing}. "
        "Add them to _PROVIDER_ENV_KEYS so get_active_key() can fall back "
        "to the canonical env var when the labeled-vault lookup misses."
    )


def test_every_env_key_entry_is_a_real_runtime_provider() -> None:
    registry = _registry_env_vars()
    stale = sorted(set(_PROVIDER_ENV_KEYS) - set(registry))
    assert not stale, (
        f"security.vault_keys._PROVIDER_ENV_KEYS references providers "
        f"that are not in agents.llm_provider._PROVIDER_REGISTRY: {stale}. "
        "Either restore the provider to _PROVIDER_REGISTRY or drop the "
        "stale env-key entry — otherwise the security layer leaks an "
        "API surface for a runtime the brain cannot actually dispatch."
    )


def test_env_var_names_match_between_security_and_runtime() -> None:
    registry = _registry_env_vars()
    mismatches: list[str] = []
    for pid, env_var in registry.items():
        if pid not in _PROVIDER_ENV_KEYS:
            continue  # covered by the missing test above
        if _PROVIDER_ENV_KEYS[pid] != env_var:
            mismatches.append(
                f"{pid}: registry={env_var!r} vs vault_keys={_PROVIDER_ENV_KEYS[pid]!r}"
            )
    assert not mismatches, (
        "env var name drift between agents.llm_provider._PROVIDER_REGISTRY "
        "and security.vault_keys._PROVIDER_ENV_KEYS:\n  - "
        + "\n  - ".join(mismatches)
        + "\nKeep the two tables byte-identical so the LLM hot path and "
        "the keychain hydration path read the same env var."
    )


@pytest.mark.parametrize("env_var", list(_PROVIDER_ENV_KEYS.values()))
def test_env_var_names_look_canonical(env_var: str) -> None:
    """Cheap sanity gate: every env var name should be SCREAMING_SNAKE
    ending in ``_API_KEY`` so operator-facing docs / wizards / CLI
    surfaces can format them uniformly."""
    assert env_var.isupper(), f"{env_var!r} is not uppercase"
    assert env_var.endswith("_API_KEY"), (
        f"{env_var!r} does not end with _API_KEY; the security/CLI "
        "layers assume the canonical suffix for human-facing prompts."
    )
