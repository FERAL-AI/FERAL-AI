"""``config.loader.load_settings`` must not rebuild the world per call.

The audited install logged 295 macOS Keychain unlocks in a single day,
roughly one every 16 seconds, with bursts of five inside 30 ms at boot.
Every one of them came from ``load_settings()``: it constructed a fresh
``ConfigLoader().discover()``, which re-read three settings files AND
opened BlindVault through ``keyring`` on a worker thread, logging three
INFO lines each time. The callers are per-turn and per-tick
(``memory/store.py``, ``agents/orchestrator.py``,
``agents/iteration_budget.py``, ``perception/context_attach.py``,
``voice/router.py``, ``cost/budget.py``), so the cost scaled with use.

Two properties are asserted here:

  * the settings-only path never opens the vault, and
  * an unchanged install builds one loader, while a changed file, a
    changed env override, or an explicit invalidation builds another.
"""

from __future__ import annotations

import json
import os

import pytest

import config.loader as loader_mod
from config.loader import ConfigLoader, clear_settings_cache, load_settings

pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated FERAL_HOME with a settings.json in it."""
    feral = tmp_path / "feral-home"
    feral.mkdir()
    (feral / "settings.json").write_text(json.dumps({"llm": {"model": "first"}}))
    monkeypatch.setenv("FERAL_HOME", str(feral))
    for key in list(os.environ):
        if key.startswith("FERAL_") and key != "FERAL_HOME":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    yield feral
    clear_settings_cache()


@pytest.fixture
def count_loaders(monkeypatch):
    """Count ``ConfigLoader`` constructions and record discover() kwargs."""
    calls = {"built": 0, "load_credentials": []}
    real_init = ConfigLoader.__init__
    real_discover = ConfigLoader.discover

    def counting_init(self, *a, **kw):
        calls["built"] += 1
        return real_init(self, *a, **kw)

    def recording_discover(self, load_credentials: bool = True):
        calls["load_credentials"].append(load_credentials)
        return real_discover(self, load_credentials=load_credentials)

    monkeypatch.setattr(ConfigLoader, "__init__", counting_init)
    monkeypatch.setattr(ConfigLoader, "discover", recording_discover)
    return calls


class TestNoVaultOnTheSettingsPath:
    def test_load_settings_never_opens_the_vault(self, home, monkeypatch):
        opened = []

        def boom(self):
            opened.append(True)

        monkeypatch.setattr(ConfigLoader, "_load_credentials", boom)
        settings = load_settings()
        assert settings["llm"]["model"] == "first"
        assert opened == [], "load_settings unlocked the keychain"

    def test_discover_still_loads_credentials_by_default(self, home, monkeypatch):
        opened = []
        monkeypatch.setattr(
            ConfigLoader, "_load_credentials", lambda self: opened.append(True)
        )
        ConfigLoader(project_dir=str(home.parent)).discover()
        assert opened == [True]


class TestCaching:
    def test_second_call_with_unchanged_files_builds_one_loader(
        self, home, count_loaders,
    ):
        first = load_settings()
        second = load_settings()
        assert count_loaders["built"] == 1
        assert first == second
        assert count_loaders["load_credentials"] == [False]

    def test_changed_mtime_invalidates(self, home, count_loaders):
        assert load_settings()["llm"]["model"] == "first"
        path = home / "settings.json"
        stat = path.stat()
        path.write_text(json.dumps({"llm": {"model": "second"}}))
        # Force a distinct mtime even on a filesystem that would otherwise
        # reuse the timestamp inside one test.
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        assert load_settings()["llm"]["model"] == "second"
        assert count_loaders["built"] == 2

    def test_changed_env_override_invalidates(self, home, count_loaders, monkeypatch):
        assert load_settings()["llm"]["model"] == "first"
        monkeypatch.setenv("FERAL_LLM_MODEL", "from-env")
        assert load_settings()["llm"]["model"] == "from-env"
        assert count_loaders["built"] == 2

    def test_explicit_clear_invalidates(self, home, count_loaders):
        load_settings()
        clear_settings_cache()
        load_settings()
        assert count_loaders["built"] == 2

    def test_caller_mutation_cannot_poison_the_cache(self, home):
        first = load_settings()
        first["llm"]["model"] = "clobbered"
        assert load_settings()["llm"]["model"] == "first"

    def test_saving_settings_clears_the_cache(self, home, count_loaders):
        assert load_settings()["llm"]["model"] == "first"
        writer = ConfigLoader(project_dir=str(home.parent))
        writer.user_home = home
        writer.save_user_settings({"llm": {"model": "written"}})
        assert loader_mod._settings_cache is None
        assert load_settings()["llm"]["model"] == "written"
