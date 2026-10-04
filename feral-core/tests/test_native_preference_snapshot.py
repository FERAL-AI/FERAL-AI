"""Native preference payload contract; synthetic artifacts, no OS defaults."""

import hashlib
import json

import pytest

from config.native_preference_snapshot import (
    NativePreferenceSnapshotError,
    validate_snapshot,
)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def body():
    primary = "fixture-primary"
    return {
        "format_version": 1,
        "primary_session_id": primary,
        "preferences": {
            "displayName": "Féral 👓",
            "avatarChoice": "imported",
            "onboarded": True,
            "feral.native.selectedConversation."
            + primary.encode().hex(): "fixture-chat",
            "native.savedContextModes.v1": {primary: ["fixture-chat"]},
        },
        "avatar": {
            "relative_path": "avatars/pet.png",
            "bytes": 4,
            "sha256": hashlib.sha256(b"fake").hexdigest(),
        },
    }


def test_valid_typed_snapshot_and_portable_avatar():
    payload = canonical(body())
    snapshot = validate_snapshot(payload, "fixture-primary")
    assert (
        snapshot.payload == payload
        and snapshot.avatar.relative_path == "avatars/pet.png"
    )
    assert snapshot.sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    "change",
    [
        "foreign",
        "future",
        "bool_version",
        "credential",
        "absolute_path",
        "bool_coercion",
        "foreign_selection",
        "foreign_context",
        "duplicate_sessions",
        "bad_session",
        "unknown_destination",
        "missing_avatar",
        "avatar_parent",
        "avatar_absolute",
        "avatar_extra",
        "avatar_bool_size",
        "avatar_hash",
        "unknown_root",
        "oversized_name",
        "nan",
        "duplicate_key",
        "pretty",
        "oversized",
        "pending_foreign",
        "recovery_bad_fence",
    ],
)
def test_malformed_snapshot_refused_without_exposing_values(change):
    value = body()
    preferences = value["preferences"]
    if change == "foreign":
        value["primary_session_id"] = "foreign-primary"
    elif change == "future":
        value["format_version"] = 2
    elif change == "bool_version":
        value["format_version"] = True
    elif change == "credential":
        preferences["api_key"] = "fixture-private-sentinel"
    elif change == "absolute_path":
        preferences["importedAvatarPath"] = "/fixture-private-sentinel"
    elif change == "bool_coercion":
        preferences["onboarded"] = 1
    elif change == "foreign_selection":
        preferences["feral.native.selectedConversation.foreign"] = "foreign-chat"
    elif change == "foreign_context":
        preferences["native.savedContextModes.v1"] = {
            "foreign-primary": ["fixture-chat"]
        }
    elif change == "duplicate_sessions":
        preferences["native.savedContextModes.v1"]["fixture-primary"] *= 2
    elif change == "bad_session":
        preferences["native.savedContextModes.v1"]["fixture-primary"] = ["\nprivate"]
    elif change == "unknown_destination":
        preferences["desktop.lastDestination"] = "unknown"
    elif change == "missing_avatar":
        value.pop("avatar")
    elif change == "avatar_parent":
        value["avatar"]["relative_path"] = "avatars/../private"
    elif change == "avatar_absolute":
        value["avatar"]["relative_path"] = "/avatars/pet.png"
    elif change == "avatar_extra":
        value["avatar"]["secret"] = "fixture-private-sentinel"
    elif change == "avatar_bool_size":
        value["avatar"]["bytes"] = True
    elif change == "avatar_hash":
        value["avatar"]["sha256"] = "BAD"
    elif change == "unknown_root":
        value["secret"] = "fixture-private-sentinel"
    elif change == "oversized_name":
        preferences["displayName"] = "x" * 1025
    elif change == "nan":
        preferences["onboarded"] = float("nan")
    elif change == "pending_foreign":
        preferences["native.pendingSavedContextCreation.v1"] = {
            "foreign-primary": "foreign-chat"
        }
    elif change == "recovery_bad_fence":
        preferences["native.pendingContextRecovery.v1"] = {
            "fixture-primary": {"fixture-chat": {"acknowledge_unknown_effects": True}}
        }
    payload = canonical(value)
    if change == "duplicate_key":
        payload = payload.replace(
            b'"format_version":1', b'"format_version":1,"format_version":1'
        )
    if change == "pretty":
        payload = json.dumps(value, indent=2).encode()
    if change == "oversized":
        payload = b" " * 1_048_577
    with pytest.raises(NativePreferenceSnapshotError) as error:
        validate_snapshot(payload, "fixture-primary")
    assert "fixture-private-sentinel" not in str(error.value)


def test_non_imported_avatar_has_no_reference():
    value = body()
    value["preferences"]["avatarChoice"] = "orb"
    with pytest.raises(NativePreferenceSnapshotError):
        validate_snapshot(canonical(value), "fixture-primary")
    value.pop("avatar")
    assert validate_snapshot(canonical(value), "fixture-primary").avatar is None
