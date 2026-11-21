"""Real manifest budgets must retain usable AX references on realtime wires."""
import json
from pathlib import Path

import pytest

from skills.executor import SkillExecutor
from skills.registry import SkillRegistry
from skills.result_budget import budget_for_tool, reset_budget_cache
from voice.ax_snapshot_receipt import compact_ax_snapshot
from voice.tool_result_envelope import serialize_realtime_tool_result


def snapshot(count=40, *, offset=0, total=None):
    total = total if total is not None else offset + count
    nodes = [{"ref": f"ax{i}", "role": "AXButton", "label": "Activity " + str(i) + " x" * 55,
              "bounds": {"x": 20, "y": 40, "width": 100, "height": 20},
              "enabled": True, "actions": ["AXPress"], "secure": False}
             for i in range(offset, offset + count)]
    return {"success": True, "status_code": 200, "error": None,
            "data": {"app": "Fixture", "bundle_id": "test.fixture", "filter": "interactive",
                     "nodes": nodes, "tree": "\n".join(f'[{n["ref"]}] {n["role"]} {n["label"]}' for n in nodes),
                     "returned": count, "offset": offset, "total_matched": total,
                     "truncated": offset + count < total,
                     "next_offset": offset + count if offset + count < total else None,
                     "limits_hit": []}}


@pytest.fixture
def registry():
    reset_budget_cache()
    registry = SkillRegistry()
    registry.load_from_directory(Path(__file__).parents[1] / "skills/manifests")
    yield registry
    reset_budget_cache()


def test_actual_manifest_executor_wire_keeps_contiguous_actionable_refs(registry):
    budget = budget_for_tool("macos_ax__snapshot", registry)
    assert budget.max_list_len >= 200
    result = snapshot()
    executor = SkillExecutor.__new__(SkillExecutor)
    result["data"] = executor._sanitize_response(result["data"], budget=budget)
    wire = serialize_realtime_tool_result("macos_ax__snapshot", result, registry=registry)
    assert len(wire) <= 8192
    data = json.loads(wire)["data"]
    assert isinstance(data, dict), "Large snapshots must not discard all actionable refs"
    lines = data["tree"].splitlines()
    assert 0 < data["returned"] <= 40
    assert [line.split("]")[0] for line in lines] == [f"[ax{i}" for i in range(data["returned"])]
    assert data["next_offset"] == (data["returned"] if data["returned"] < 40 else None)


def test_compaction_redacts_secure_fields_and_retains_incomplete_walk():
    data = snapshot(offset=7, total=100)["data"]
    data["nodes"][0].update(role="AXSecureTextField", label="private-canary")
    data["limits_hit"] = ["timeout"]
    compact = compact_ax_snapshot(data, 1800, lambda text: text.replace("Activity", "Item"))
    assert compact and len(json.dumps(compact)) <= 1800
    assert "private-canary" not in compact["tree"]
    assert "secure value withheld" in compact["tree"]
    assert compact["limits_hit"] == ["timeout"]
    assert compact["next_offset"] == 7 + compact["returned"]


@pytest.mark.parametrize("field,value", [("returned", 2), ("offset", True),
                                        ("total_matched", 1), ("next_offset", 999),
                                        ("truncated", "yes"), ("limits_hit", ["invented"])])
def test_invalid_pagination_cannot_mint_a_valid_compact_page(field, value):
    data = snapshot()["data"]
    data[field] = value
    assert compact_ax_snapshot(data, 1800, lambda text: text) is None


def test_bad_or_duplicate_ref_stops_without_skipping_nodes():
    data = snapshot()["data"]
    data["nodes"][2]["ref"] = "ax0"
    compact = compact_ax_snapshot(data, 8000, lambda text: text)
    assert compact["returned"] == 2 and compact["next_offset"] == 2
    assert "ax3" not in compact["tree"]
