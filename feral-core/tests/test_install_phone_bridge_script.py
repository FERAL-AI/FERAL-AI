"""Smoke tests for ``scripts/install-phone-bridge.sh``.

The end-to-end installer test (does the LaunchAgent / systemd unit
actually register a daemon?) requires Lane 07's ``feral bridge
install`` wrapper to finalise — coordinate via WORK_LOG before
adding the live test. For now we pin the script's static structure:

- Calls the canonical ``feral_node_sdk run`` subcommand (NOT the
  bare module, which never had a long-running command).
- Honors the four documented flags ``--token``, ``--brain-url``,
  ``--node-id``, ``--prefix``.
- Writes the per-OS supervisor unit (LaunchAgent on Darwin, systemd
  --user on Linux).

The python-node-sdk ``run`` subcommand's argparse contract is pinned
separately in test_install_phone_bridge_cli_run_subcommand below so
the script and SDK stay in lockstep.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "install-phone-bridge.sh"

# Add the python-node-sdk src dir to the import path so the
# argparse-contract tests can import ``feral_node_sdk.cli`` without
# requiring the SDK to be pip-installed. Mirrors the bootstrap
# convention used by ``feral-nodes/python-node-sdk/tests/conftest.py``.
_NODE_SDK_SRC = REPO_ROOT / "feral-nodes" / "python-node-sdk" / "src"
if _NODE_SDK_SRC.is_dir():
    sys.path.insert(0, str(_NODE_SDK_SRC))


def _script_text() -> str:
    assert SCRIPT_PATH.is_file(), f"installer script missing: {SCRIPT_PATH}"
    return SCRIPT_PATH.read_text(encoding="utf-8")


def test_install_script_exists_and_is_bash():
    text = _script_text()
    assert text.startswith("#!/usr/bin/env bash"), "expected bash shebang"


def test_install_script_invokes_feral_node_sdk_run():
    """Lane 11 fix — script used to call the bare module
    (``python -m feral_node_sdk.cli``) which had no long-running
    entrypoint. The fix is to call the new ``run`` subcommand."""
    text = _script_text()
    assert re.search(
        r"python\s+-m\s+feral_node_sdk\s+run\b", text
    ), "install script must call `python -m feral_node_sdk run`"


@pytest.mark.parametrize("flag", ["--node-id", "--brain-url", "--token", "--prefix"])
def test_install_script_documents_all_flags(flag: str):
    text = _script_text()
    assert flag in text, f"installer flag {flag} missing from script"


def test_install_script_writes_per_os_supervisor_unit():
    """Darwin path writes a LaunchAgent plist; Linux path writes a
    systemd --user unit. Both must be present so the script is
    portable across the operator's machines."""
    text = _script_text()
    assert "Library/LaunchAgents" in text
    assert "systemd/user" in text


def test_python_node_sdk_run_subcommand_accepts_install_script_flags():
    """The python-node-sdk argparse contract must accept the flags the
    install script supplies. A drift here is the bug Lane 11 closes."""
    from feral_node_sdk.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "run",
        "--node-id", "bridge-test",
        "--brain-url", "ws://localhost:9090/v1/node",
        "--token", "tok-test",
    ])
    assert args.cmd == "run"
    assert args.node_id == "bridge-test"
    assert args.brain_url == "ws://localhost:9090/v1/node"
    assert args.token == "tok-test"
    assert args.node_type == "bridge"


def test_python_node_sdk_run_subcommand_rejects_missing_token():
    """argparse must require ``--token`` so the script never silently
    invokes the daemon with an empty bearer."""
    from feral_node_sdk.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "run",
            "--node-id", "bridge-test",
            "--brain-url", "ws://localhost:9090/v1/node",
        ])
