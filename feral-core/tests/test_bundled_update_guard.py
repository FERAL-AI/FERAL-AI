"""Actual loaded update-command source under isolated runtime layouts; no pip."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import types

import pytest

from cli import update_command


SOURCE = Path(update_command.__file__).read_bytes()


def load_at(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(SOURCE)
    spec = importlib.util.spec_from_file_location("isolated_update_command", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.__file__).read_bytes() == SOURCE
    return module


def runtime(monkeypatch, root: Path, *, executable: Path | None = None, base: Path | None = None):
    root.mkdir(parents=True, exist_ok=True)
    exe = executable or root / "bin/python3.11"
    exe.parent.mkdir(parents=True, exist_ok=True)
    if not exe.exists():
        exe.write_text("synthetic interpreter identity; never executed")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "prefix", str(root))
    monkeypatch.setattr(sys, "base_prefix", str(base or root))
    return exe


def forbid_effects(monkeypatch, module):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("Bundled update reached metadata/network/service/install/restart work")

    for name in ("install_kind", "_availability", "running_brain_interpreter", "_report_staleness"):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    monkeypatch.setitem(sys.modules, "cli.main", types.SimpleNamespace(cmd_restart=forbidden))
    return calls, forbidden


@pytest.mark.parametrize("layout", ["mac-native", "mac-tauri", "tauri-staged", "linux-current", "linux-historical"])
@pytest.mark.parametrize("wheel", [False, True])
@pytest.mark.parametrize("check_only", [False, True])
def test_actual_bundle_loaded_command_refuses_before_all_effects(tmp_path, monkeypatch, capsys, layout, wheel, check_only):
    parent = tmp_path.resolve()
    resources = {
        "mac-native": parent / "FERAL Native.app/Contents/Resources",
        "mac-tauri": parent / "FERAL.app/Contents/Resources",
        "tauri-staged": parent / "repo/desktop/src-tauri/resources",
        "linux-current": parent / "FERAL.AppDir/usr/lib/FERAL",
        "linux-historical": parent / "FERAL.AppDir/usr/lib/feral-desktop",
    }[layout]
    python = resources / "python"
    runtime(monkeypatch, python)
    location = (python / "lib/python3.11/site-packages" if wheel else resources / "feral-core")
    module = load_at(location / "cli/update_command.py")
    calls, forbidden = forbid_effects(monkeypatch, module)
    assert module.bundled_runtime()["kind"] == "bundled"
    assert module.cmd_update(check_only=check_only, runner=forbidden) == 1
    assert not calls
    output = capsys.readouterr().out
    assert "whole app" in output and "No network check" in output
    if check_only:
        assert "no package-index comparison" in output


@pytest.mark.parametrize("defect", ["foreign-prefix", "foreign-base", "foreign-executable", "foreign-module", "missing-python", "redirected-python"])
def test_identified_bundle_ambiguity_is_not_permission_to_upgrade(tmp_path, monkeypatch, defect):
    root = tmp_path.resolve()
    resource = root / "FERAL.app/Contents/Resources"
    python = resource / "python"
    runtime(monkeypatch, python)
    module_path = resource / "feral-core/cli/update_command.py"
    if defect == "foreign-module":
        module_path = root / "outside/cli/update_command.py"
    module = load_at(module_path)
    outside = root / "outside-python"
    outside.mkdir(exist_ok=True)
    if defect == "foreign-prefix":
        monkeypatch.setattr(sys, "prefix", str(outside))
    elif defect == "foreign-base":
        monkeypatch.setattr(sys, "base_prefix", str(outside))
    elif defect == "foreign-executable":
        exe = outside / "python3"
        exe.write_text("not executed")
        monkeypatch.setattr(sys, "executable", str(exe))
    elif defect == "missing-python":
        monkeypatch.setattr(sys, "prefix", str(resource / "missing"))
        monkeypatch.setattr(sys, "executable", str(resource / "missing/bin/python3"))
    elif defect == "redirected-python":
        # Keep source lexically bundled, but redirect its interpreter out of
        # the actual resource tree. No fixture binary is executed or deleted.
        redirected = resource / "redirected/python"
        redirected.parent.mkdir()
        redirected.symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(sys, "prefix", str(redirected))
    calls, forbidden = forbid_effects(monkeypatch, module)
    assert module.bundled_runtime()["kind"] == "ambiguous"
    assert module.cmd_update(runner=forbidden) == 1
    assert not calls


@pytest.mark.parametrize("layout", ["wheel", "venv", "local-wheel", "app-substring", "resources-name", "arbitrary-usr-lib", "external-venv-bundled-base"])
def test_ordinary_installs_keep_actual_update_dispatch(tmp_path, monkeypatch, layout):
    root = tmp_path.resolve()
    prefix = {
        "wheel": root / "opt/python",
        "venv": root / "project/.venv",
        "local-wheel": root / "local/env",
        "app-substring": root / "FERAL.app-backups/python",
        "resources-name": root / "Resources/python",
        "arbitrary-usr-lib": root / "usr/lib/unrelated/python",
        "external-venv-bundled-base": root / "project/.venv",
    }[layout]
    runtime(monkeypatch, prefix)
    if layout == "external-venv-bundled-base":
        bundled_base = root / "FERAL.app/Contents/Resources/python"
        bundled_base.mkdir(parents=True)
        monkeypatch.setattr(sys, "base_prefix", str(bundled_base))
    module = load_at(prefix / "lib/python3.11/site-packages/cli/update_command.py")
    assert module.bundled_runtime() is None
    kinds = []
    monkeypatch.setattr(module, "install_kind", lambda: kinds.append(1) or {"editable": False, "version": "1.0", "location": str(prefix)})
    monkeypatch.setattr(module, "_availability", lambda version: {"status": "update-available", "latest_version": "2.0", "update_available": True})
    monkeypatch.setattr(module, "running_brain_interpreter", lambda: {"status": "none"})
    calls = []

    def runner(command):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    assert module.cmd_update(runner=runner) == 0
    assert calls == [[sys.executable, "-m", "pip", "install", "--upgrade", "feral-ai"]]
    assert len(kinds) == 2


def test_ordinary_editable_checkout_keeps_refusal_without_network(tmp_path, monkeypatch, capsys):
    root = tmp_path.resolve()
    runtime(monkeypatch, root / ".venv")
    module = load_at(root / "feral-core/cli/update_command.py")
    monkeypatch.setattr(module, "install_kind", lambda: {"editable": True, "version": "1.0"})
    monkeypatch.setattr(module, "_availability", lambda _: pytest.fail("Editable path contacted package index"))
    assert module.cmd_update(runner=lambda _: pytest.fail("Editable path ran pip")) == 1
    assert "git pull" in capsys.readouterr().out


def test_bundle_refuses_even_when_distribution_reports_updateable_wheel(tmp_path, monkeypatch):
    resource = tmp_path.resolve() / "FERAL.app/Contents/Resources"
    python = resource / "python"
    runtime(monkeypatch, python)
    module = load_at(resource / "feral-core/cli/update_command.py")
    effects = []
    monkeypatch.setattr(module, "install_kind", lambda: effects.append("metadata") or {"editable": False, "version": "1.0"})
    monkeypatch.setattr(module, "_availability", lambda version: effects.append("index") or {"status": "update-available", "latest_version": "2.0", "update_available": True})
    monkeypatch.setattr(module, "running_brain_interpreter", lambda: effects.append("service") or {"status": "none"})

    def runner(command):
        effects.append(command)
        return subprocess.CompletedProcess(command, 0)

    assert module.cmd_update(runner=runner) == 1
    assert effects == []


def test_bundled_relative_interpreter_symlink_is_coherent(tmp_path, monkeypatch):
    resource = tmp_path.resolve() / "FERAL.app/Contents/Resources"
    python = resource / "python"
    exe = runtime(monkeypatch, python)
    alias = exe.parent / "python3"
    alias.symlink_to(exe.name)
    monkeypatch.setattr(sys, "executable", str(alias))
    module = load_at(resource / "feral-core/cli/update_command.py")
    assert module.bundled_runtime()["kind"] == "bundled"
    calls, forbidden = forbid_effects(monkeypatch, module)
    assert module.cmd_update(runner=forbidden) == 1 and calls == []


def test_external_venv_can_use_symlinked_bundled_base_without_upgrading_bundle(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    base = root / "FERAL.app/Contents/Resources/python"
    base_exe = runtime(monkeypatch, base)
    venv = root / "ordinary-venv"
    (venv / "bin").mkdir(parents=True)
    alias = venv / "bin/python3"
    alias.symlink_to(base_exe)
    monkeypatch.setattr(sys, "executable", str(alias))
    monkeypatch.setattr(sys, "prefix", str(venv))
    module = load_at(venv / "lib/python3.11/site-packages/cli/update_command.py")
    assert module.bundled_runtime() is None


def test_home_environment_does_not_convert_an_ordinary_install_into_bundle(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    runtime(monkeypatch, root / "ordinary-venv")
    module = load_at(root / "ordinary-venv/lib/python3.11/site-packages/cli/update_command.py")
    monkeypatch.setenv("FERAL_HOME", str(root / "FERAL.app/Contents/Resources"))
    assert module.bundled_runtime() is None
