"""B8 — re-running the setup wizard must not erase settings it never saw.

``POST /api/setup/complete`` calls ``ConfigLoader.save_user_settings``,
which did ``open(path, "w")`` + ``json.dump(settings)`` with no read and
no merge. The browser form's payload became the WHOLE of settings.json.

Every other writer in the codebase goes through ``update_settings``,
which re-reads the file and patches one key, so everything those writers
own is invisible to the wizard and was deleted by it:

  * ``meta.brain_id``                       (config/loader.py)
  * ``meta.relay_id``                       (security/brain_identity.py)
  * ``channels.*_allowed_senders`` / ``_allowed_chats``
                                            (api/state.py _persist_pairing)
  * ``access.tailscale``                    (api/routes/access.py)
  * ``llm.fallback_providers``              (api/routes/llm.py)
  * ``memory.backend``                      (api/routes/memory.py)

A paired Telegram sender is the sharpest one: the operator re-opens the
wizard to change a model, and every person allowed to message the brain
silently stops being allowed.

SEMANTICS UNDER TEST (RFC 7386 JSON Merge Patch):
  * a key ABSENT from the payload is left exactly as it was,
  * a key PRESENT with any non-null value replaces what was there
    (so ``""``, ``false``, ``0`` and ``[]`` all still clear a value),
  * a key present with ``null`` is DELETED,
  * objects merge recursively, arrays replace wholesale.

A blind merge would be wrong on its own: the wizard must still be able to
clear something. Under these semantics it clears by SAYING SO, and only
silence is treated as "leave alone".
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.loader import ConfigLoader  # noqa: E402


@pytest.fixture
def loader(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    cfg = ConfigLoader()
    cfg.user_home.mkdir(parents=True, exist_ok=True)
    return cfg


def _read(loader) -> dict:
    return json.loads((loader.user_home / "settings.json").read_text())


def _existing_state(loader) -> None:
    """Everything the wizard's payload does not mention, as written by the
    six other writers."""
    (loader.user_home / "settings.json").write_text(json.dumps({
        "meta": {"brain_id": "brain-abc", "relay_id": "relay-xyz",
                 "setup_complete": True},
        "channels": {
            "telegram_enabled": True,
            "telegram_allowed_senders": ["12345"],
            "telegram_allowed_chats": ["-100999"],
            "discord_allowed_senders": ["77"],
        },
        "access": {"tailscale": {"funnel": True,
                                 "tailnet_url": "https://brain.ts.net"}},
        "llm": {"provider": "ollama", "fallback_providers": ["groq", "openai"]},
        "memory": {"backend": "qdrant"},
    }, indent=2))


# ── the wizard payload, as the browser form builds it ────────────────

_WIZARD_PAYLOAD = {
    "llm": {"provider": "openai", "model": "gpt-4o"},
    "voice": {"enabled": True},
}


def test_paired_telegram_sender_survives_a_wizard_rerun(loader):
    """The headline symptom: everyone allowed to message the brain is
    silently un-allowed by re-running setup."""
    _existing_state(loader)
    loader.save_user_settings(dict(_WIZARD_PAYLOAD))
    after = _read(loader)
    assert after["channels"]["telegram_allowed_senders"] == ["12345"]
    assert after["channels"]["telegram_allowed_chats"] == ["-100999"]
    assert after["channels"]["discord_allowed_senders"] == ["77"]


def test_identity_and_remote_access_survive_a_wizard_rerun(loader):
    _existing_state(loader)
    loader.save_user_settings(dict(_WIZARD_PAYLOAD))
    after = _read(loader)
    assert after["meta"]["brain_id"] == "brain-abc"
    assert after["meta"]["relay_id"] == "relay-xyz"
    assert after["access"]["tailscale"]["tailnet_url"] == "https://brain.ts.net"
    assert after["memory"]["backend"] == "qdrant"


def test_sibling_keys_in_a_touched_section_survive(loader):
    """``llm`` IS in the payload, but only two of its keys are. The rest
    of the section must not go with it -- this is the difference between
    a deep merge and a top-level ``dict.update``."""
    _existing_state(loader)
    loader.save_user_settings(dict(_WIZARD_PAYLOAD))
    after = _read(loader)
    assert after["llm"]["provider"] == "openai", "the wizard's own value must win"
    assert after["llm"]["model"] == "gpt-4o"
    assert after["llm"]["fallback_providers"] == ["groq", "openai"]


def test_the_wizard_can_still_clear_a_value_it_names(loader):
    """A merge that cannot clear anything is its own bug. An explicitly
    sent empty/false value must land."""
    _existing_state(loader)
    loader.save_user_settings({
        "channels": {"telegram_enabled": False, "telegram_allowed_senders": []},
        "llm": {"provider": ""},
    })
    after = _read(loader)
    assert after["channels"]["telegram_enabled"] is False
    assert after["channels"]["telegram_allowed_senders"] == []
    assert after["llm"]["provider"] == ""
    # ...and the untouched neighbours still survive.
    assert after["channels"]["discord_allowed_senders"] == ["77"]
    assert after["meta"]["brain_id"] == "brain-abc"


def test_explicit_null_deletes_a_key(loader):
    """RFC 7386: null is the delete verb. Without one there is no way to
    remove a key at all once merging is on."""
    _existing_state(loader)
    loader.save_user_settings({"memory": {"backend": None}})
    after = _read(loader)
    assert "backend" not in after.get("memory", {})
    assert after["meta"]["brain_id"] == "brain-abc"


def test_arrays_replace_rather_than_accumulate(loader):
    """A list is one value. Merging element-wise would make an allowlist
    impossible to shrink and would duplicate on every write."""
    _existing_state(loader)
    loader.save_user_settings({"llm": {"fallback_providers": ["anthropic"]}})
    assert _read(loader)["llm"]["fallback_providers"] == ["anthropic"]


def test_first_write_with_no_existing_file(loader):
    """Fresh install: nothing to merge with, payload lands verbatim."""
    loader.save_user_settings({"llm": {"provider": "ollama"}})
    assert _read(loader) == {"llm": {"provider": "ollama"}}


def test_a_corrupt_settings_file_does_not_lose_the_new_write(loader):
    """If the existing file cannot be parsed there is nothing to preserve;
    the write must still succeed rather than raise into the route."""
    (loader.user_home / "settings.json").write_text("{ not json")
    loader.save_user_settings({"llm": {"provider": "ollama"}})
    assert _read(loader)["llm"]["provider"] == "ollama"


def test_update_settings_still_patches_exactly_one_key(loader):
    """``update_settings`` funnels through ``save_user_settings``; the new
    merge must not change its read-modify-write contract."""
    _existing_state(loader)
    loader.update_settings("llm", "provider", "anthropic")
    after = _read(loader)
    assert after["llm"]["provider"] == "anthropic"
    assert after["llm"]["fallback_providers"] == ["groq", "openai"]
    assert after["meta"]["brain_id"] == "brain-abc"


async def test_setup_complete_route_preserves_pairings(loader, monkeypatch):
    """End to end through the actual route, which is where the operator
    meets this bug."""
    from api.routes import config as config_route

    _existing_state(loader)
    monkeypatch.setattr(config_route.state, "config", loader, raising=False)

    result = await config_route.complete_setup({"settings": dict(_WIZARD_PAYLOAD)})
    assert result["ok"] is True

    after = _read(loader)
    assert after["channels"]["telegram_allowed_senders"] == ["12345"]
    assert after["meta"]["relay_id"] == "relay-xyz"
    assert after["llm"]["provider"] == "openai"
