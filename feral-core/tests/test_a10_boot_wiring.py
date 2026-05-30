"""Regression guard for BrainState orchestrator wiring order (-A10)."""

from __future__ import annotations

from pathlib import Path


def test_orchestrator_boot_block_has_no_preengine_wiring_calls() -> None:
    state_py = Path(__file__).resolve().parents[1] / "api" / "state.py"
    text = state_py.read_text(encoding="utf-8")

    marker = 'with boot_subsystem(self._boot_report, "Orchestrator", optional=False):'
    assert marker in text

    post_orchestrator = text.split(marker, 1)[1]
    tool_genesis_marker = 'with boot_subsystem(self._boot_report, "ToolGenesisEngine"):'
    assert tool_genesis_marker in post_orchestrator
    pre_tool_genesis = post_orchestrator.split(tool_genesis_marker, 1)[0]

    # -A10: the orchestrator boot block should not try to wire engines
    # before those engines are constructed.
    assert "set_tool_genesis(" not in pre_tool_genesis
    assert "set_mitosis_engine(" not in pre_tool_genesis

    # Keep wiring single-source: exactly one call each, in the engine
    # construction blocks later in the file.
    assert text.count("self.orchestrator.set_tool_genesis(") == 1
    assert text.count("self.orchestrator.set_mitosis_engine(") == 1


def test_boot_wires_starter_reminders_install_guard() -> None:
    state_py = Path(__file__).resolve().parents[1] / "api" / "state.py"
    text = state_py.read_text(encoding="utf-8")

    assert 'starter_dir = (' in text
    assert '"feral-reminders"' in text
    assert "if self.app_registry.get(\"feral-reminders\") is None:" in text
    assert "self.app_registry.install_from_dir(" in text
