"""The coding-harness knobs and the self-learning flag must survive a restart.

Both halves of the round trip are under test here, because only having
both is what preserves the existing contract:

* ``_apply_env_overrides`` pulls ``FERAL_*`` into the settings tree, so a
  shell export still wins over ``settings.json``;
* ``export_as_env`` pushes the tree back out, so a value that was only
  ever persisted still reaches subsystems that resolve their config from
  the environment and nowhere else.

Drop either half and the key becomes the defect this repo keeps hitting:
stored, displayed, and ignored.

``FERAL_HOME`` is redirected to a tmp dir by the autouse
``isolate_feral_home`` fixture in ``conftest.py``, so nothing here can
touch a real install.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.loader import DEFAULT_SETTINGS, ConfigLoader  # noqa: E402

# name in settings under ``coding`` -> (env var, shipped default as exported)
CODING_KNOBS = [
    ("read_before_edit", "FERAL_READ_BEFORE_EDIT", "warn"),
    ("tool_call_context", "FERAL_TOOL_CALL_CONTEXT", "on"),
    ("edit_max_content_lines", "FERAL_EDIT_MAX_CONTENT_LINES", "4000"),
    ("edit_max_needle_lines", "FERAL_EDIT_MAX_NEEDLE_LINES", "400"),
    ("checkpoint_retention_days", "FERAL_CHECKPOINT_RETENTION_DAYS", "14"),
    ("checkpoint_max_blob_bytes", "FERAL_CHECKPOINT_MAX_BLOB_BYTES", "8388608"),
    ("post_edit_diagnostics", "FERAL_POST_EDIT_DIAGNOSTICS", "on"),
    ("diagnostics_timeout", "FERAL_DIAGNOSTICS_TIMEOUT", "5"),
    ("turn_idle_seconds", "FERAL_TURN_IDLE_SECONDS", "180"),
]


def _loader(tmp_path, monkeypatch) -> ConfigLoader:
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    loader = ConfigLoader(project_dir=str(tmp_path / "project"))
    loader.user_home = tmp_path / "home"
    loader.discover()
    return loader


@pytest.mark.parametrize("key,env_name,expected", CODING_KNOBS)
def test_shipped_defaults_export_the_values_the_readers_already_use(
    key, env_name, expected, tmp_path, monkeypatch
):
    """An install with no ``coding`` block must behave exactly as before.

    Each expected value is the literal default in the subsystem that
    reads the variable, so this is the regression that catches a typo
    silently changing harness behaviour for every existing install.
    """
    monkeypatch.delenv(env_name, raising=False)
    env = _loader(tmp_path, monkeypatch).export_as_env()
    assert env[env_name] == expected


@pytest.mark.parametrize("key,env_name,_expected", CODING_KNOBS)
def test_persisted_setting_reaches_the_env(key, env_name, _expected, tmp_path, monkeypatch):
    """A value written to settings.json alone must reach the reader."""
    monkeypatch.delenv(env_name, raising=False)
    loader = _loader(tmp_path, monkeypatch)
    default = DEFAULT_SETTINGS["coding"][key]
    override = 999 if isinstance(default, int) else "enforce"
    loader.update_settings("coding", key, override)

    reloaded = ConfigLoader(project_dir=str(tmp_path / "project"))
    reloaded.user_home = tmp_path / "home"
    reloaded.discover()
    assert reloaded.export_as_env()[env_name] == str(override)


@pytest.mark.parametrize("key,env_name,_expected", CODING_KNOBS)
def test_env_var_still_overrides_the_persisted_setting(
    key, env_name, _expected, tmp_path, monkeypatch
):
    """Existing behaviour is unchanged: the shell wins over settings.json."""
    loader = _loader(tmp_path, monkeypatch)
    default = DEFAULT_SETTINGS["coding"][key]
    stored = 111 if isinstance(default, int) else "off"
    loader.update_settings("coding", key, stored)

    from_env = "222" if isinstance(default, int) else "enforce"
    monkeypatch.setenv(env_name, from_env)

    reloaded = ConfigLoader(project_dir=str(tmp_path / "project"))
    reloaded.user_home = tmp_path / "home"
    reloaded.discover()
    assert reloaded.settings["coding"][key] == (
        int(from_env) if isinstance(default, int) else from_env
    )
    assert reloaded.export_as_env()[env_name] == from_env


def test_empty_checkpoint_dir_is_not_exported(tmp_path, monkeypatch):
    """``checkpoint_root`` treats any truthy value as an outright override.

    Exporting ``FERAL_CHECKPOINT_DIR=""`` would be harmless today only
    because the reader happens to use a truthiness check. Not exporting
    an unset value keeps that from becoming load-bearing.
    """
    monkeypatch.delenv("FERAL_CHECKPOINT_DIR", raising=False)
    env = _loader(tmp_path, monkeypatch).export_as_env()
    assert "FERAL_CHECKPOINT_DIR" not in env


def test_configured_checkpoint_dir_is_exported(tmp_path, monkeypatch):
    monkeypatch.delenv("FERAL_CHECKPOINT_DIR", raising=False)
    loader = _loader(tmp_path, monkeypatch)
    loader.update_settings("coding", "checkpoint_dir", "/tmp/feral-ckpt")

    reloaded = ConfigLoader(project_dir=str(tmp_path / "project"))
    reloaded.user_home = tmp_path / "home"
    reloaded.discover()
    assert reloaded.export_as_env()["FERAL_CHECKPOINT_DIR"] == "/tmp/feral-ckpt"


# ---------------------------------------------------------------------
# features.self_learning
# ---------------------------------------------------------------------

def test_self_learning_off_survives_a_restart(tmp_path, monkeypatch):
    """The exact defect: turn it off, restart, it comes back on.

    ``agents/learner.py::_self_learning_enabled`` reads
    ``FERAL_SELF_LEARNING`` and defaults to true when unset, and only
    ``api/routes/config.py`` set that variable, at toggle time. With no
    export at boot the operator's "off" was lost on every restart and
    the extract + summarize LLM calls resumed.
    """
    monkeypatch.delenv("FERAL_SELF_LEARNING", raising=False)
    loader = _loader(tmp_path, monkeypatch)
    assert loader.export_as_env()["FERAL_SELF_LEARNING"] == "true"

    loader.update_settings("features", "self_learning", False)

    restarted = ConfigLoader(project_dir=str(tmp_path / "project"))
    restarted.user_home = tmp_path / "home"
    restarted.discover()
    assert restarted.export_as_env()["FERAL_SELF_LEARNING"] == "false"


def test_self_learning_env_var_still_wins(tmp_path, monkeypatch):
    loader = _loader(tmp_path, monkeypatch)
    loader.update_settings("features", "self_learning", True)
    monkeypatch.setenv("FERAL_SELF_LEARNING", "false")

    restarted = ConfigLoader(project_dir=str(tmp_path / "project"))
    restarted.user_home = tmp_path / "home"
    restarted.discover()
    assert restarted.export_as_env()["FERAL_SELF_LEARNING"] == "false"


def test_learner_honours_the_exported_value(tmp_path, monkeypatch):
    """End to end: settings -> export -> the function that gates the LLM calls."""
    import os

    from agents.learner import _self_learning_enabled

    monkeypatch.delenv("FERAL_SELF_LEARNING", raising=False)
    loader = _loader(tmp_path, monkeypatch)
    loader.update_settings("features", "self_learning", False)

    restarted = ConfigLoader(project_dir=str(tmp_path / "project"))
    restarted.user_home = tmp_path / "home"
    restarted.discover()
    for name, value in restarted.export_as_env().items():
        monkeypatch.setitem(os.environ, name, value)

    assert _self_learning_enabled() is False
