"""FERAL reminders companion skill for the starter GenUI app."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

from config.loader import feral_home
from skills.base import BaseSkill
from skills.impl import register_skill


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _store_path() -> Path:
    return feral_home() / "data" / "reminders.json"


def _load_items(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            items.append(dict(entry))
    return items


def _save_items(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=True, indent=2), encoding="utf-8")


def _arg_value(args: Dict[str, Any], key: str) -> Any:
    if key in args:
        return args.get(key)
    values = args.get("values")
    if isinstance(values, dict):
        return values.get(key)
    return None


def _lookup_reminder_id(args: Dict[str, Any]) -> str:
    for key in ("id", "reminder_id", "target_id"):
        value = _arg_value(args, key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


@register_skill
class FeralRemindersSkill(BaseSkill):
    name = "FERAL Reminders"
    description = "Companion reminder operations for the GenUI starter app."
    safety_level = "SAFE"

    def __init__(self) -> None:
        super().__init__(skill_id="feral_reminders")

    async def execute(self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]) -> Dict[str, Any]:
        del vault  # no credentials required
        path = _store_path()
        items = _load_items(path)

        if endpoint_id == "create":
            title_raw = _arg_value(args, "title")
            title = str(title_raw or "").strip()
            if not title:
                return {
                    "success": False,
                    "status_code": 400,
                    "data": None,
                    "error": "Missing reminder title",
                    "reason": "missing_required_field",
                    "field": "title",
                }
            # `due` is required at the dispatcher layer in addition to the
            # JSON-schema gate in tool_dispatch_validator. The schema gate
            # catches calls that arrive missing the key entirely; this
            # second check rejects ``due=""`` / ``due=None`` / ``due="   "``
            # which JSON-schema would accept as a string. Without this, an
            # LLM that obeyed the tool signature literally ("due is a
            # string, sure, here's an empty one") could create reminders
            # with no scheduled time — silent data loss.
            due_value = _arg_value(args, "due")
            if due_value is None:
                due_value = _arg_value(args, "when_iso")
            due = str(due_value).strip() if due_value is not None else ""
            if not due:
                return {
                    "success": False,
                    "status_code": 400,
                    "data": None,
                    "error": (
                        "Reminder 'due' is required. Pass an ISO-8601 timestamp "
                        "(e.g. '2026-05-22T14:00:00-07:00') or a natural-language "
                        "phrase the orchestrator can resolve."
                    ),
                    "reason": "missing_required_field",
                    "field": "due",
                }
            reminder = {
                "id": f"rem_{uuid4().hex[:10]}",
                "title": title,
                "due": due,
                "completed": False,
                "created_at": _now_iso(),
                "completed_at": None,
            }
            items.append(reminder)
            _save_items(path, items)
            # Honesty principle: re-read the store and confirm the reminder
            # actually persisted before reporting success. Never claim a
            # reminder was created when it is not in the list.
            persisted = _load_items(path)
            if not any(str(it.get("id")) == reminder["id"] for it in persisted):
                return {
                    "success": False,
                    "status_code": 500,
                    "data": None,
                    "error": (
                        "Reminder could not be confirmed — it is not in the list "
                        "after write. Nothing was saved; please retry."
                    ),
                    "reason": "verify_after_write_failed",
                }
            return {
                "success": True,
                "status_code": 200,
                "data": {"reminder": reminder, "count": len(persisted), "verified": True},
                "error": None,
            }

        if endpoint_id == "list":
            include_completed = _to_bool(_arg_value(args, "include_completed"))
            listed = [item for item in items if include_completed or not _to_bool(item.get("completed"))]
            listed.sort(key=lambda item: item.get("created_at", ""), reverse=True)
            return {"success": True, "status_code": 200, "data": {"items": listed, "count": len(listed)}, "error": None}

        if endpoint_id == "complete":
            reminder_id = _lookup_reminder_id(args)
            if not reminder_id:
                return {"success": False, "status_code": 400, "data": None, "error": "Missing reminder id"}
            for item in items:
                if str(item.get("id")) != reminder_id:
                    continue
                item["completed"] = True
                item["completed_at"] = _now_iso()
                _save_items(path, items)
                return {"success": True, "status_code": 200, "data": {"reminder": item}, "error": None}
            return {"success": False, "status_code": 404, "data": None, "error": f"Reminder {reminder_id} not found"}

        if endpoint_id == "delete":
            reminder_id = _lookup_reminder_id(args)
            if not reminder_id:
                return {"success": False, "status_code": 400, "data": None, "error": "Missing reminder id"}
            remaining = [item for item in items if str(item.get("id")) != reminder_id]
            if len(remaining) == len(items):
                return {"success": False, "status_code": 404, "data": None, "error": f"Reminder {reminder_id} not found"}
            _save_items(path, remaining)
            return {
                "success": True,
                "status_code": 200,
                "data": {"deleted_id": reminder_id, "count": len(remaining)},
                "error": None,
            }

        if endpoint_id == "schedule_notification":
            reminder_id = _lookup_reminder_id(args)
            if not reminder_id:
                return {"success": False, "status_code": 400, "data": None, "error": "Missing reminder id"}
            when_iso_raw = _arg_value(args, "when_iso")
            when_iso = str(when_iso_raw).strip() if when_iso_raw else ""
            if not when_iso:
                when_iso = _now_iso()
            return {
                "success": True,
                "status_code": 200,
                "data": {
                    "scheduled": True,
                    "id": reminder_id,
                    "when_iso": when_iso,
                },
                "error": None,
            }

        return {"success": False, "status_code": 404, "data": None, "error": f"Unknown endpoint: {endpoint_id}"}
