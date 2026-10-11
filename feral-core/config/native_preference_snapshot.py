"""Validate an explicit native preference artifact; never access OS defaults."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import unicodedata
from pathlib import PurePosixPath
from uuid import UUID

MAX_SNAPSHOT_BYTES = 1_048_576
MAX_AVATAR_BYTES = 8_388_608
RESERVED_ENTRY = "config/.feral-native-preferences.v1.json"
_CONTEXT_KEYS = {"native.savedContextModes.v1", "native.pendingSavedContextCreation.v1",
                 "native.pendingContextRecovery.v1"}
_BOOL_KEYS = {"onboarded", "desktop.keepRunningWhenClosed", "desktop.menuBarEnabled",
              "desktop.restoreLastDestination"}
_FIXED_KEYS = _CONTEXT_KEYS | _BOOL_KEYS | {"displayName", "avatarChoice", "desktop.lastDestination"}
_DESTINATIONS = {"Home", "Chat", "Conversations", "Voice", "Coding", "Health", "Memory", "Knowledge",
                 "Memory Context", "Identity", "Specialists", "Workflows", "Automation", "Apps",
                 "Skills & Store", "Oversight", "Activity", "Devices", "AI Providers", "Security & Cost", "Settings"}


class NativePreferenceSnapshotError(ValueError):
    """Static, redacted snapshot refusal."""


@dataclass(frozen=True)
class NativeAvatarReference:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class NativePreferenceSnapshot:
    payload: bytes
    primary_session_id: str
    sha256: str
    avatar: NativeAvatarReference | None


def valid_id(value: object) -> bool:
    if not isinstance(value, str) or not value or value.strip() != value:
        return False
    try:
        return len(value.encode("utf-8")) <= 256 and not any(
            unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)
    except UnicodeError:
        return False


def valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise NativePreferenceSnapshotError("Duplicate native preference snapshot field")
        result[key] = value
    return result


def _uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _context(key: str, value: object, primary: str) -> bool:
    if not isinstance(value, dict) or set(value) != {primary}:
        return False
    own = value[primary]
    if key == "native.savedContextModes.v1":
        return (isinstance(own, list) and len(own) <= 1000 and all(valid_id(item) for item in own)
                and len(set(own)) == len(own))
    if key == "native.pendingSavedContextCreation.v1":
        return valid_id(own)
    if not isinstance(own, dict) or len(own) > 1000:
        return False
    expected = {"contract_version", "session_id", "generation", "revision", "attempt_id", "acknowledge_unknown_effects"}
    for session, row in own.items():
        if (not valid_id(session) or not isinstance(row, dict) or set(row) != expected
                or type(row["contract_version"]) is not int or row["contract_version"] != 1
                or row["session_id"] != session or not _uuid(row["generation"])
                or not _uuid(row["attempt_id"]) or type(row["revision"]) is not int
                or not 1 <= row["revision"] <= 2**63 - 3
                or row["acknowledge_unknown_effects"] is not True):
            return False
    return True


def validate_snapshot(payload: bytes, expected_primary: str) -> NativePreferenceSnapshot:
    """Require Swift v1's typed, compact sorted JSON and exact primary binding."""
    try:
        if not isinstance(payload, bytes) or len(payload) > MAX_SNAPSHOT_BYTES or not valid_id(expected_primary):
            raise NativePreferenceSnapshotError("Unsupported native preference snapshot")
        body = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(body, dict) or set(body) not in (
                {"format_version", "primary_session_id", "preferences"},
                {"format_version", "primary_session_id", "preferences", "avatar"})
                or type(body["format_version"]) is not int or body["format_version"] != 1
                or not valid_id(body["primary_session_id"])):
            raise NativePreferenceSnapshotError("Unsupported native preference snapshot")
        if body["primary_session_id"] != expected_primary:
            raise NativePreferenceSnapshotError("Native preference primary installation does not match")
        if json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") != payload:
            raise NativePreferenceSnapshotError("Native preference snapshot is not canonical")
        preferences = body["preferences"]
        selection = "feral.native.selectedConversation." + expected_primary.encode("utf-8").hex()
        if not isinstance(preferences, dict) or not set(preferences).issubset(_FIXED_KEYS | {selection}):
            raise NativePreferenceSnapshotError("Native preference snapshot contains unsupported fields")
        for key, value in preferences.items():
            valid = False
            if key in _BOOL_KEYS:
                valid = type(value) is bool
            elif key == "displayName":
                valid = isinstance(value, str) and len(value.encode("utf-8")) <= 1024
            elif key == "avatarChoice":
                valid = value in {"photo", "orb", "imported"} if isinstance(value, str) else False
            elif key == "desktop.lastDestination":
                valid = value in _DESTINATIONS if isinstance(value, str) else False
            elif key in _CONTEXT_KEYS:
                valid = _context(key, value, expected_primary)
            elif key == selection:
                valid = valid_id(value)
            if not valid:
                raise NativePreferenceSnapshotError("Native preference snapshot contains malformed values")
        avatar = None
        if preferences.get("avatarChoice") == "imported":
            value = body.get("avatar")
            if (not isinstance(value, dict) or set(value) != {"relative_path", "bytes", "sha256"}
                    or not isinstance(value["relative_path"], str) or type(value["bytes"]) is not int
                    or not 1 <= value["bytes"] <= MAX_AVATAR_BYTES or not valid_digest(value["sha256"])):
                raise NativePreferenceSnapshotError("Invalid portable avatar reference")
            name = value["relative_path"]
            path = PurePosixPath(name)
            if (str(path) != name or len(path.parts) != 2 or path.parts[0] != "avatars"
                    or len(name.encode("utf-8")) > 512 or any(part in {".", "..", ""} for part in path.parts)
                    or "\\" in name or ":" in name or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in name)):
                raise NativePreferenceSnapshotError("Invalid portable avatar reference")
            avatar = NativeAvatarReference(name, value["bytes"], value["sha256"])
        elif "avatar" in body:
            raise NativePreferenceSnapshotError("Unexpected portable avatar reference")
        return NativePreferenceSnapshot(payload, expected_primary, hashlib.sha256(payload).hexdigest(), avatar)
    except NativePreferenceSnapshotError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as error:
        raise NativePreferenceSnapshotError("Unsupported native preference snapshot") from error
