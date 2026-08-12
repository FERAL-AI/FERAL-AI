"""A skill that fails to load, or that nothing can reach, must say so.

Two defects, one shape.

1. ``skills/impl/__init__.py`` auto-loaded 26 implementations, each in
   its own ``try: import ... except ImportError: pass``. A missing
   optional dependency removed the skill from the process with no log
   line and no record. The operator could not tell a skill was gone; the
   model was simply never offered the tool.

2. ``skills/impl/image_gen.py`` registers ``image_gen`` on import and is
   a complete DALL-E 3 implementation with provider failover, but no
   manifest names ``image_gen``. ``SkillRegistry.get_skill`` returns None
   for a skill_id it holds no manifest for, so the implementation is
   loaded code that nothing can dispatch. Verified against the shipped
   tree: 38 manifests, none with ``skill_id == "image_gen"``.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import skills.impl as impl  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")


# ---------------------------------------------------------------------------
# 1. A failed import must be recorded and logged
# ---------------------------------------------------------------------------

def test_the_loader_is_data_driven_not_26_silent_blocks():
    assert len(impl.AUTOLOAD_MODULES) >= 26
    assert "image_gen" in impl.AUTOLOAD_MODULES
    assert "agentic_computer_use" in impl.AUTOLOAD_MODULES


def test_a_missing_dependency_is_recorded_and_logged(monkeypatch, caplog):
    import importlib

    real_import = importlib.import_module

    def _fake(name, *a, **kw):
        if name == "skills.impl.agentic_computer_use":
            raise ImportError("No module named 'some_optional_vlm_dep'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", _fake)
    monkeypatch.setattr(impl, "FAILED_IMPLEMENTATIONS", {})

    with caplog.at_level(logging.WARNING, logger="feral.skills.impl"):
        impl._autoload()

    assert "agentic_computer_use" in impl.FAILED_IMPLEMENTATIONS
    assert "some_optional_vlm_dep" in impl.FAILED_IMPLEMENTATIONS["agentic_computer_use"]
    assert "is NOT loaded" in caplog.text
    assert "agentic_computer_use" in caplog.text


def test_a_module_that_raises_at_import_is_recorded_too(monkeypatch, caplog):
    import importlib

    real_import = importlib.import_module

    def _fake(name, *a, **kw):
        if name == "skills.impl.plan":
            raise RuntimeError("bad module-level code")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", _fake)
    monkeypatch.setattr(impl, "FAILED_IMPLEMENTATIONS", {})

    with caplog.at_level(logging.ERROR, logger="feral.skills.impl"):
        impl._autoload()

    assert "plan" in impl.FAILED_IMPLEMENTATIONS
    assert "RuntimeError" in impl.FAILED_IMPLEMENTATIONS["plan"]


def test_this_install_loaded_everything():
    """Nothing is silently missing in the tree under test."""
    assert impl.load_report()["failed"] == {}


# ---------------------------------------------------------------------------
# 2. Registered but unreachable
# ---------------------------------------------------------------------------

def _shipped_manifest_skill_ids() -> set[str]:
    ids = set()
    for path in glob.glob(os.path.join(REPO, "skills", "manifests", "*.json")):
        with open(path, encoding="utf-8") as fh:
            ids.add(json.load(fh).get("skill_id", os.path.basename(path)[:-5]))
    return ids


def test_image_gen_registers_an_implementation():
    assert impl.get_implementation("image_gen") is not None


def test_image_gen_has_no_manifest_so_it_cannot_be_dispatched():
    """The finding itself, asserted against the shipped tree."""
    assert "image_gen" not in _shipped_manifest_skill_ids()


def test_the_registry_cannot_return_image_gen():
    from skills.registry import SkillRegistry

    registry = SkillRegistry()
    registry.load_builtin_skills()
    assert registry.get_skill("image_gen") is None, (
        "if this starts passing, image_gen gained a manifest and this test "
        "should be inverted"
    )


def test_an_unreachable_implementation_is_reported(caplog):
    with caplog.at_level(logging.WARNING, logger="feral.skills.impl"):
        unreachable = impl.report_unreachable_implementations(
            {"web_search", "notes_memory"},
        )
    assert "image_gen" in unreachable
    assert "no manifest names it" in caplog.text


def test_a_complete_registry_reports_nothing_unreachable_that_is_reachable():
    """weather_current and browser are reachable, via a hardcoded
    constant and via api.state._register_browser_skill. Neither may be
    reported once the real registry ids are supplied."""
    known = set(impl.load_report()["loaded"])
    assert impl.load_report(known)["unreachable_no_manifest"] == []


@pytest.mark.parametrize("skill_id", ["weather_current", "browser"])
def test_runtime_registered_skills_are_not_false_positives(skill_id):
    from skills.registry import SkillRegistry

    registry = SkillRegistry()
    registry.load_builtin_skills()
    if skill_id not in registry.skills:
        pytest.skip(f"{skill_id} is not registered in this configuration")
    assert skill_id not in impl.report_unreachable_implementations(
        registry.skills.keys(),
    )
