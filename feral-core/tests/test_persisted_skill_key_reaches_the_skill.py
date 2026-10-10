"""A key stored in the vault must reach the skill after a restart.

store_key writes to the encrypted vault (survives a restart) and to an
in-process cache. But the executor handed backing implementations the
bare cache, which is empty on a fresh process, so a persisted key never
reached the skill: GET /api/skills/keys reported has_key true while the
skill itself returned "not configured", and the operator had done
everything right.

Found live: the Maps key was stored through the key route, the brain was
restarted, and places__find_places still answered that it had no key.
"""
from __future__ import annotations

from skills.executor import SKILL_KEY_NAMESPACE, SkillExecutor


class _Vault:
    """Stands in for the encrypted vault across a restart."""

    def __init__(self, stored):
        self.stored = stored

    def get(self, namespace, key, requester=""):
        return self.stored.get((namespace, key))

    def retrieve(self, key, requester=""):
        return None


def _executor(*, cache=None, vault=None):
    ex = SkillExecutor.__new__(SkillExecutor)
    ex._vault = dict(cache or {})
    ex._blind_vault = vault
    return ex


def test_a_persisted_key_reaches_the_skill_with_an_empty_cache():
    ex = _executor(vault=_Vault({(SKILL_KEY_NAMESPACE, "places"): "maps-key"}))
    assert ex._vault_for("places")["places"] == "maps-key"


def test_the_process_cache_still_works_without_a_vault():
    ex = _executor(cache={"places": "cached-key"})
    assert ex._vault_for("places")["places"] == "cached-key"


def test_other_skills_keys_are_still_visible():
    ex = _executor(cache={"weather": "w"},
                   vault=_Vault({(SKILL_KEY_NAMESPACE, "places"): "maps-key"}))
    view = ex._vault_for("places")
    assert view["weather"] == "w" and view["places"] == "maps-key"


def test_a_skill_with_no_key_anywhere_gets_nothing():
    ex = _executor(vault=_Vault({}))
    assert "places" not in ex._vault_for("places")


def test_a_broken_vault_does_not_break_the_call():
    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("vault locked")

        def retrieve(self, *a, **k):
            raise RuntimeError("vault locked")

    ex = _executor(cache={"places": "cached"}, vault=_Boom())
    assert ex._vault_for("places")["places"] == "cached"
