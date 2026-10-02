"""Internal model-context codec, never a transcript import or permission source.

Historical tool messages are inert data. Images are deliberately unavailable
after restoration. The lifecycle owner must rebuild policy and current prompts.
"""
from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

FORMAT_VERSION = 1
IMAGE_OMITTED = "[Image unavailable in restored context]"


class CheckpointValidationError(ValueError):
    """Redacted invalid context/identity; messages never include private input."""


class CheckpointFutureFormat(CheckpointValidationError):
    pass


@dataclass(frozen=True)
class CheckpointLimits:
    history_rows: int = 200
    working_rows: int = 50
    record_bytes: int = 512 * 1024
    total_bytes: int = 64 * 1024 * 1024
    sessions: int = 1000

    def __post_init__(self) -> None:
        maxima = (200, 50, 512 * 1024, 64 * 1024 * 1024, 1000)
        for value, maximum in zip((self.history_rows, self.working_rows,
                                   self.record_bytes, self.total_bytes, self.sessions), maxima):
            if type(value) is not int or not 1 <= value <= maximum:
                raise CheckpointValidationError("Invalid checkpoint limits")


def validate_session_id(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 1024 or value.strip() != value or any(
        unicodedata.category(char) == "Cc" for char in value
    ):
        raise CheckpointValidationError("Invalid checkpoint session identity")


def validate_uuid(value: str) -> None:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise CheckpointValidationError("Invalid checkpoint generation or attempt") from None


@dataclass(frozen=True)
class CheckpointFence:
    session_id: str
    generation: str
    revision: int
    attempt_id: str

    def __post_init__(self) -> None:
        validate_session_id(self.session_id)
        validate_uuid(self.generation)
        validate_uuid(self.attempt_id)
        if type(self.revision) is not int or not 1 <= self.revision < 2**63 - 1:
            raise CheckpointValidationError("Invalid checkpoint revision")


class CheckpointStatus(str, Enum):
    ABSENT = "absent"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    DELETED = "deleted"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    APPLIED = "applied"
    CONFLICT = "conflict"
    QUOTA = "quota"
    INVALID = "invalid"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CheckpointValidationError("Invalid checkpoint object")
    return dict(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointValidationError("Duplicate checkpoint field")
        result[key] = value
    return result


def _load(encoded: str) -> dict[str, object]:
    try:
        value: object = json.loads(encoded, object_pairs_hook=_unique_object)
        return _object(value)
    except (ValueError, TypeError, RecursionError):
        raise CheckpointValidationError("Invalid checkpoint encoding") from None


@dataclass(frozen=True)
class CheckpointContext:
    """Immutable encoded context. Commit revalidates even manually constructed values."""
    encoded: str

    @property
    def byte_count(self) -> int:
        try:
            return len(self.encoded.encode("utf-8"))
        except UnicodeError:
            raise CheckpointValidationError("Invalid checkpoint text encoding") from None

    def history(self) -> list[dict[str, object]]:
        return _rows(_load(self.encoded)["history"])

    def working(self) -> list[dict[str, object]]:
        return _rows(_load(self.encoded)["working"])

    def omissions(self) -> dict[str, object]:
        return _object(_load(self.encoded)["omissions"])


@dataclass(frozen=True)
class CheckpointRecord:
    fence: CheckpointFence
    state: CheckpointStatus
    updated_at: float
    context: CheckpointContext | None = None


@dataclass(frozen=True)
class CheckpointResult:
    status: CheckpointStatus
    record: CheckpointRecord | None = None


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise CheckpointValidationError("Invalid checkpoint rows")
    return [_object(row) for row in value]


def _content(value: object) -> tuple[str, int]:
    if isinstance(value, str):
        return value, 0
    if not isinstance(value, list):
        raise CheckpointValidationError("Invalid checkpoint message content")
    texts: list[str] = []
    images = 0
    for raw in value:
        block = _object(raw)
        kind = block.get("type")
        if kind == "text" and set(block) == {"type", "text"} and isinstance(block["text"], str):
            texts.append(block["text"])
        elif isinstance(kind, str) and kind in {"image_url", "input_image", "image"}:
            # Image fields can contain credentials or base64; never copy them.
            images += 1
            texts.append(IMAGE_OMITTED)
        else:
            raise CheckpointValidationError("Unsupported checkpoint content block")
    return "\n".join(texts), images


def _history_row(raw: Mapping[str, object]) -> tuple[dict[str, object] | None, int]:
    allowed = {"role", "content", "tool_calls", "tool_call_id", "name", "ts", "source", "feral_consolidation"}
    if set(raw) - allowed:
        raise CheckpointValidationError("Unsupported checkpoint history fields")
    role = raw.get("role")
    if not isinstance(role, str) or role not in {"user", "assistant", "tool", "system"}:
        raise CheckpointValidationError("Invalid checkpoint message role")
    text, images = _content(raw.get("content", ""))
    if role == "system":
        # Only a server-produced compaction summary survives, demoted to data.
        # Its provenance is not restored as authority or a consolidation marker.
        watermark = raw.get("feral_consolidation")
        if isinstance(watermark, dict) and watermark.get("derived_from") == "raw_turns" and text.startswith("[Session Summary]\n"):
            return {"role": "assistant", "content": "[Restored summary data]\n" + text}, images
        return None, images
    row: dict[str, object] = {"role": role, "content": text}
    calls = raw.get("tool_calls")
    if calls is not None:
        if role != "assistant" or not isinstance(calls, list) or not calls:
            raise CheckpointValidationError("Invalid checkpoint tool calls")
        normalized: list[dict[str, object]] = []
        identifiers: set[str] = set()
        for item in calls:
            call = _object(item)
            function = _object(call.get("function"))
            identifier = call.get("id")
            name, arguments = function.get("name"), function.get("arguments")
            if set(call) != {"id", "type", "function"} or call["type"] != "function" or set(function) != {"name", "arguments"}:
                raise CheckpointValidationError("Invalid checkpoint tool call fields")
            if not isinstance(identifier, str) or not identifier or identifier in identifiers or not isinstance(name, str) or not name or not isinstance(arguments, str):
                raise CheckpointValidationError("Invalid checkpoint tool call identity")
            identifiers.add(identifier)
            normalized.append({"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}})
        row["tool_calls"] = normalized
    if role == "tool":
        identifier, name = raw.get("tool_call_id"), raw.get("name")
        if not isinstance(identifier, str) or not identifier or (name is not None and not isinstance(name, str)):
            raise CheckpointValidationError("Invalid checkpoint tool result")
        row["tool_call_id"] = identifier
        if name is not None:
            row["name"] = name
    elif "tool_call_id" in raw or "name" in raw:
        raise CheckpointValidationError("Misplaced checkpoint tool fields")
    return row, images


def _groups(rows: list[dict[str, object]]) -> tuple[list[list[dict[str, object]]], int]:
    """Tool rounds remain indivisible; incomplete and orphan results are omitted."""
    groups: list[list[dict[str, object]]] = []
    omitted = 0
    index = 0
    while index < len(rows):
        row = rows[index]
        calls = row.get("tool_calls")
        if isinstance(calls, list):
            expected = {str(_object(call)["id"]) for call in calls}
            group = [row]
            seen: set[str] = set()
            index += 1
            while index < len(rows) and rows[index]["role"] == "tool":
                result = rows[index]
                identifier = str(result["tool_call_id"])
                if identifier not in expected or identifier in seen:
                    break
                seen.add(identifier)
                group.append(result)
                index += 1
            if expected == seen:
                groups.append(group)
            else:
                omitted += len(group)
        elif row["role"] == "tool":
            omitted += 1
            index += 1
        else:
            groups.append([row])
            index += 1
    return groups, omitted


def encode_context(history: Sequence[Mapping[str, object]], working: Sequence[Mapping[str, object]],
                   *, limits: CheckpointLimits = CheckpointLimits(),
                   tool_image_call_ids: frozenset[str] = frozenset()) -> CheckpointContext:
    """Called only by trusted runtime writers; never bind this to an import API."""
    if not isinstance(tool_image_call_ids, frozenset) or any(not isinstance(item, str) or not item for item in tool_image_call_ids):
        raise CheckpointValidationError("Invalid checkpoint image references")
    normalized: list[dict[str, object]] = []
    omitted_system = images = 0
    for raw in history:
        row, count = _history_row(raw)
        images += count
        if row is None:
            omitted_system += 1
        else:
            if row.get("tool_call_id") in tool_image_call_ids:
                row["content"] = str(row["content"]) + "\n" + IMAGE_OMITTED
                images += 1
            normalized.append(row)
    groups, omitted_history = _groups(normalized)
    kept: list[list[dict[str, object]]] = []
    count = 0
    for group in reversed(groups):
        if count + len(group) > limits.history_rows:
            # Preserve a contiguous suffix, never a hole or partial tool group.
            break
        kept.insert(0, group)
        count += len(group)
    omitted_history += len(normalized) - omitted_history - count
    working_rows: list[dict[str, object]] = []
    for raw in working:
        role = raw.get("role")
        if set(raw) - {"role", "text", "summary", "content", "source", "ts"} or not isinstance(role, str) or role not in {"user", "assistant"}:
            raise CheckpointValidationError("Unsupported checkpoint working fields")
        text, count = _content(raw.get("text", raw.get("summary", raw.get("content", ""))))
        images += count
        row = {"role": raw["role"], "text": text}
        ts = raw.get("ts")
        if ts is not None:
            if type(ts) not in {int, float} or not isinstance(ts, (int, float)) or not math.isfinite(ts):
                raise CheckpointValidationError("Invalid checkpoint working timestamp")
            row["ts"] = ts
        working_rows.append(row)
    encoded = _json({"format_version": FORMAT_VERSION, "history": [row for group in kept for row in group],
                     "working": working_rows[-limits.working_rows:], "omissions": {
                         "history_rows": omitted_history, "system_rows": omitted_system,
                         "images": images, "working_rows": max(0, len(working_rows) - limits.working_rows)}})
    context = CheckpointContext(encoded)
    if context.byte_count > limits.record_bytes:
        raise CheckpointValidationError("Checkpoint context exceeds byte quota")
    return context


def decode_context(encoded: str, *, limits: CheckpointLimits = CheckpointLimits()) -> CheckpointContext:
    if not isinstance(encoded, str) or CheckpointContext(encoded).byte_count > limits.record_bytes:
        raise CheckpointValidationError("Checkpoint context exceeds byte quota")
    root = _load(encoded)
    version = root.get("format_version")
    if type(version) is int and version > FORMAT_VERSION:
        raise CheckpointFutureFormat("Unsupported checkpoint format")
    if version != FORMAT_VERSION or type(version) is not int or set(root) != {"format_version", "history", "working", "omissions"}:
        raise CheckpointValidationError("Invalid checkpoint format")
    history, working = _rows(root["history"]), _rows(root["working"])
    omissions = _object(root["omissions"])
    if set(omissions) != {"history_rows", "system_rows", "images", "working_rows"} or any(type(value) is not int or not isinstance(value, int) or value < 0 for value in omissions.values()):
        raise CheckpointValidationError("Invalid checkpoint omissions")
    rebuilt = encode_context(history, working, limits=limits)
    sanitized = _load(rebuilt.encoded)
    # Persisted context must already be normalized and complete, not silently
    # repaired. Image/system omissions describe the prior server snapshot.
    if sanitized["history"] != history or sanitized["working"] != working:
        raise CheckpointValidationError("Invalid checkpoint message groups")
    return CheckpointContext(_json(root))
