"""`FERAL_KEY_<SKILL>` must work whatever case the operator uses.

`_get_key` resolves the in-process cache with an exact-case dict
lookup, and every skill id in the tree is lower-case (all 41 manifests
checked). `load_vault_from_env` stored the id exactly as it appeared
after the prefix, so:

    FERAL_KEY_WEB_SEARCH=...   ->  vault["WEB_SEARCH"]   never read
    FERAL_KEY_web_search=...   ->  vault["web_search"]   read

Environment variables are conventionally upper-case, and shells,
systemd units and CI runners routinely upper-case them, so the operator
set the key, the skill reported it had none, and nothing anywhere said
why.

`config/loader.py` has always lower-cased this same pattern, so the two
halves of the system disagreed about what one variable meant.
"""

from __future__ import annotations

import glob
import json

import pytest

from skills.executor import SkillExecutor


def _executor() -> SkillExecutor:
    ex = SkillExecutor.__new__(SkillExecutor)
    ex._vault = {}
    ex._blind_vault = None
    return ex


@pytest.mark.parametrize(
    "env_name",
    ["FERAL_KEY_WEB_SEARCH", "FERAL_KEY_web_search", "FERAL_KEY_Web_Search"],
)
def test_any_case_reaches_the_skill(monkeypatch, env_name):
    monkeypatch.setenv(env_name, "secret-value")
    ex = _executor()
    ex.load_vault_from_env()
    assert ex._get_key("web_search") == "secret-value", (
        f"{env_name} did not reach skill 'web_search'; vault holds {sorted(ex._vault)}"
    )


def test_the_stored_id_matches_the_skill_id(monkeypatch):
    monkeypatch.setenv("FERAL_KEY_WEB_SEARCH", "x")
    ex = _executor()
    ex.load_vault_from_env()
    assert sorted(ex._vault) == ["web_search"]


def test_set_key_and_get_key_agree_on_case(monkeypatch):
    ex = _executor()
    ex.set_key("Web_Search", "from-set-key")
    assert ex._get_key("web_search") == "from-set-key"
    assert ex._get_key("WEB_SEARCH") == "from-set-key"


def test_a_bare_prefix_is_not_a_skill(monkeypatch):
    monkeypatch.setenv("FERAL_KEY_", "orphan")
    ex = _executor()
    ex.load_vault_from_env()
    assert "" not in ex._vault


def test_unrelated_env_vars_are_left_alone(monkeypatch):
    monkeypatch.setenv("FERALKEY_web_search", "no")
    monkeypatch.setenv("OPENAI_API_KEY", "no")
    ex = _executor()
    ex.load_vault_from_env()
    assert ex._get_key("web_search") is None


def test_every_shipped_skill_id_is_lowercase():
    """The premise of the normalisation, asserted rather than assumed.

    If a skill ever ships an id with a capital in it, lower-casing here
    would silently stop resolving its key, so this fails first and
    names it.
    """
    offenders = []
    for path in glob.glob("skills/manifests/*.json"):
        try:
            with open(path) as fh:
                sid = json.load(fh).get("skill_id", "")
        except Exception:
            continue
        if sid and sid != sid.lower():
            offenders.append(sid)
    assert not offenders, (
        f"skill ids with uppercase characters: {offenders}. "
        "load_vault_from_env lower-cases, so these would stop resolving."
    )


def test_the_loader_and_the_executor_agree(monkeypatch):
    """Two parsers, one variable. They must mean the same thing."""
    monkeypatch.setenv("FERAL_KEY_WEB_SEARCH", "shared")
    ex = _executor()
    ex.load_vault_from_env()

    import config.loader as cl
    src = open(cl.__file__).read()
    assert 'key[10:].lower()' in src, (
        "config/loader.py no longer lower-cases FERAL_KEY_*; the executor "
        "still does, so the two would disagree again"
    )
    assert list(ex._vault) == ["web_search"]
