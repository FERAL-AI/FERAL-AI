from __future__ import annotations

import pytest

from skills.impl.feral_reminders import FeralRemindersSkill


@pytest.mark.asyncio
async def test_create_list_complete_delete_and_schedule(monkeypatch, tmp_path):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    skill = FeralRemindersSkill()

    created = await skill.execute(
        "create",
        {"values": {"title": "Buy milk", "due": "2026-05-02T09:00:00Z"}},
        {},
    )
    assert created["success"] is True
    reminder = created["data"]["reminder"]
    rid = reminder["id"]
    assert reminder["title"] == "Buy milk"

    listed = await skill.execute("list", {}, {})
    assert listed["success"] is True
    assert listed["data"]["count"] == 1
    assert listed["data"]["items"][0]["id"] == rid

    completed = await skill.execute("complete", {"id": rid}, {})
    assert completed["success"] is True
    assert completed["data"]["reminder"]["completed"] is True

    listed_open = await skill.execute("list", {}, {})
    assert listed_open["success"] is True
    assert listed_open["data"]["count"] == 0

    listed_all = await skill.execute("list", {"include_completed": True}, {})
    assert listed_all["success"] is True
    assert listed_all["data"]["count"] == 1

    scheduled = await skill.execute(
        "schedule_notification",
        {"id": rid, "when_iso": "2026-05-03T10:30:00Z"},
        {},
    )
    assert scheduled["success"] is True
    assert scheduled["data"]["scheduled"] is True

    deleted = await skill.execute("delete", {"id": rid}, {})
    assert deleted["success"] is True
    assert deleted["data"]["deleted_id"] == rid

    listed_final = await skill.execute("list", {"include_completed": True}, {})
    assert listed_final["success"] is True
    assert listed_final["data"]["count"] == 0


# ── Lane 05 (Wave 2): `due` is required at the dispatcher layer ────


@pytest.mark.asyncio
async def test_create_rejects_missing_due(monkeypatch, tmp_path):
    """Missing `due` returns a structured 400 with reason+field, not silent acceptance."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    skill = FeralRemindersSkill()

    result = await skill.execute("create", {"title": "Drink water"}, {})

    assert result["success"] is False
    assert result["status_code"] == 400
    assert result["reason"] == "missing_required_field"
    assert result["field"] == "due"
    assert "due" in result["error"].lower()


@pytest.mark.asyncio
async def test_create_rejects_empty_due(monkeypatch, tmp_path):
    """Empty / whitespace-only `due` is rejected — JSON-schema would accept ''."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    skill = FeralRemindersSkill()

    for empty in ("", "   ", "\n\t"):
        result = await skill.execute("create", {"title": "Drink water", "due": empty}, {})
        assert result["success"] is False, f"empty due {empty!r} should be rejected"
        assert result["status_code"] == 400
        assert result["reason"] == "missing_required_field"
        assert result["field"] == "due"


@pytest.mark.asyncio
async def test_create_accepts_natural_language_due(monkeypatch, tmp_path):
    """Natural-language strings are valid `due` values (orchestrator resolves them)."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    skill = FeralRemindersSkill()

    result = await skill.execute(
        "create",
        {"title": "Standup", "due": "tomorrow at 9am"},
        {},
    )
    assert result["success"] is True
    assert result["data"]["reminder"]["due"] == "tomorrow at 9am"


@pytest.mark.asyncio
async def test_create_accepts_when_iso_alias(monkeypatch, tmp_path):
    """Legacy `when_iso` alias still maps to `due` — backwards compat."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    skill = FeralRemindersSkill()

    result = await skill.execute(
        "create",
        {"title": "Demo", "when_iso": "2026-06-01T10:00:00Z"},
        {},
    )
    assert result["success"] is True
    assert result["data"]["reminder"]["due"] == "2026-06-01T10:00:00Z"


def test_manifest_marks_due_required():
    """The manifest schema must declare `due` required so the
    JSON-schema validator (Lane 02) rejects calls missing the key
    before this dispatcher ever runs."""
    import json
    from pathlib import Path

    manifest = json.loads(
        (
            Path(__file__).resolve().parent.parent
            / "skills" / "manifests" / "feral_reminders.json"
        ).read_text()
    )
    create_ep = next(ep for ep in manifest["endpoints"] if ep["id"] == "create")
    due_param = next(p for p in create_ep["params"] if p["name"] == "due")
    assert due_param["required"] is True


def test_remind_me_only_in_reminders_manifest():
    """Trigger-phrase collision fix: 'remind me' must live in
    feral_reminders only, not in notes_memory. Otherwise the
    orchestrator's keyword router can't decide between the two when
    the user says 'remind me about the meeting'."""
    import json
    from pathlib import Path

    manifests_dir = Path(__file__).resolve().parent.parent / "skills" / "manifests"
    notes = json.loads((manifests_dir / "notes.json").read_text())
    reminders = json.loads((manifests_dir / "feral_reminders.json").read_text())

    notes_lc = [p.lower() for p in notes["trigger_phrases"]]
    reminders_lc = [p.lower() for p in reminders["trigger_phrases"]]

    assert "remind me" not in notes_lc, (
        "'remind me' must be removed from notes_memory; it routes to feral_reminders"
    )
    assert "remind me" in reminders_lc, (
        "'remind me' must remain in feral_reminders triggers"
    )
