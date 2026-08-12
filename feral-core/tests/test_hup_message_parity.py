"""v2026.5.28 — HUP wire-protocol parity between iOS and Android bridges.

Until v2026.5.28 the Android `TheoraBrainClient.kt` shipped a strict
subset of the iOS `FeralBrainClient.swift` HUP surface: four outbound
message types and three inbound branches were missing. That left
Android phones unable to send sensor batches / camera frames / skill
approvals / confirmation responses, and unable to react to the
corresponding inbound prompts.

This test parses both source files as text and asserts that the
supported HUP message-type sets match. It is intentionally
implementation-light (regex against the raw source) so the test
catches divergence regardless of which language refactors a method.
The brain is the single source of truth for HUP wire types: whenever
a new type lands, both client files must adopt it in the same release.

Tests:
* every iOS outbound type is implemented on Android (and vice versa)
* every iOS inbound case is handled on Android (and vice versa)
* the canonical class + filename are `FeralBrainClient`
  (matches v2026.5.28 rename from `TheoraBrainClient` on Android)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# Paths are resolved relative to ASOS/ — the brain test runner cwd is
# `feral-core/`, the bridges live alongside under `feral-nodes/`.
ASOS_ROOT = Path(__file__).resolve().parent.parent.parent
# F-15: this used to read feral-nodes/ios-bridge/FeralBrainClient.swift, an
# unbuilt duplicate that was 170 lines behind the copy Package.swift compiles.
# Checking parity against a file nothing builds is how the Android bridge came
# to be documented against a superseded pairing shape.
IOS_BRIDGE = (
    ASOS_ROOT
    / "feral-nodes"
    / "ios-app"
    / "Sources"
    / "FeralBridge"
    / "FeralBrainClient.swift"
)
ANDROID_BRIDGE = (
    ASOS_ROOT
    / "feral-nodes"
    / "android-bridge"
    / "bridge"
    / "src"
    / "main"
    / "java"
    / "ai"
    / "feral"
    / "bridge"
    / "FeralBrainClient.kt"
)


def _ios_outbound_types(source: str) -> set[str]:
    """Extract iOS outbound HUP types.

    iOS payloads use the shape ``"type": "<name>"`` inside the
    `[String: Any]` dict literal that is then handed to ``sendJSON``.
    """
    return set(re.findall(r'"type"\s*:\s*"([a-z_]+)"', source))


def _ios_inbound_types(source: str) -> set[str]:
    """Extract iOS inbound HUP types from `case "<name>":` switch arms."""
    return set(re.findall(r'case\s+"([a-z_]+)"\s*:', source))


def _android_outbound_types(source: str) -> set[str]:
    """Extract Android outbound HUP types.

    Android uses ``put("type", "<name>")`` inside ``buildJsonObject``.
    """
    return set(re.findall(r'put\(\s*"type"\s*,\s*"([a-z_]+)"\s*\)', source))


def _android_inbound_types(source: str) -> set[str]:
    """Extract Android inbound HUP types from the `when` arm strings."""
    body = source
    when_match = re.search(r'when\s*\(\s*type\s*\)\s*\{', body)
    if when_match:
        body = body[when_match.end():]
    return set(re.findall(r'"([a-z_]+)"\s*->', body))


def test_bridge_files_exist() -> None:
    assert IOS_BRIDGE.exists(), f"missing iOS bridge at {IOS_BRIDGE}"
    assert ANDROID_BRIDGE.exists(), (
        f"missing Android bridge at {ANDROID_BRIDGE} — was the class "
        "renamed from TheoraBrainClient.kt to FeralBrainClient.kt in "
        "v2026.5.28?"
    )


def test_android_class_renamed_to_FeralBrainClient() -> None:
    """v2026.5.28 renamed TheoraBrainClient -> FeralBrainClient."""
    text = ANDROID_BRIDGE.read_text()
    assert "class FeralBrainClient" in text, (
        "Android class name no longer matches iOS — restore the "
        "`class FeralBrainClient` declaration"
    )
    assert "class TheoraBrainClient" not in text, (
        "Android still ships the legacy `TheoraBrainClient` class "
        "alongside `FeralBrainClient`; remove the legacy name"
    )


def test_android_package_matches_directory() -> None:
    """v2026.5.28 moved io/feral/bridge/ → ai/feral/bridge/."""
    text = ANDROID_BRIDGE.read_text()
    assert "package ai.feral.bridge" in text
    assert "/ai/feral/bridge/" in str(ANDROID_BRIDGE)
    assert "/io/feral/bridge/" not in str(ANDROID_BRIDGE)


def test_outbound_message_types_match() -> None:
    """Every outbound HUP type sent by iOS must be sent by Android too."""
    ios_src = IOS_BRIDGE.read_text()
    android_src = ANDROID_BRIDGE.read_text()

    ios = _ios_outbound_types(ios_src)
    android = _android_outbound_types(android_src)

    # Ignore types only used in inbound-response constructors (none today).
    expected = {
        "register",
        "voice_config",
        "audio_chunk",
        "text_command",
        "sensor_telemetry",
        "sensor_batch",
        "frame",
        "glasses_status",
        "skill_approval",
        "confirmation_response",
    }
    missing_on_android = expected - android
    missing_on_ios = expected - ios
    assert not missing_on_android, (
        f"Android outbound HUP types missing vs spec: {sorted(missing_on_android)}"
    )
    assert not missing_on_ios, (
        f"iOS outbound HUP types missing vs spec: {sorted(missing_on_ios)}"
    )

    # No drift in either direction:
    drift_ios_to_android = ios - android
    drift_android_to_ios = android - ios
    assert not drift_ios_to_android, (
        f"iOS sends outbound types Android does not: {sorted(drift_ios_to_android)}"
    )
    assert not drift_android_to_ios, (
        f"Android sends outbound types iOS does not: {sorted(drift_android_to_ios)}"
    )


def test_inbound_message_types_match() -> None:
    """Every inbound HUP type handled by iOS must be handled by Android too."""
    ios_src = IOS_BRIDGE.read_text()
    android_src = ANDROID_BRIDGE.read_text()

    ios = _ios_inbound_types(ios_src)
    android = _android_inbound_types(android_src)

    expected = {
        "registered",
        "text_response",
        "skill_proposal",
        "confirmation_required",
        "audio_response",
        "speech_started",
        "transcript",
        "execute",
    }
    missing_on_android = expected - android
    missing_on_ios = expected - ios
    assert not missing_on_android, (
        f"Android inbound HUP types missing vs spec: {sorted(missing_on_android)}"
    )
    assert not missing_on_ios, (
        f"iOS inbound HUP types missing vs spec: {sorted(missing_on_ios)}"
    )


@pytest.mark.parametrize(
    "type_name",
    [
        # New v2026.5.28 outbound types — explicit guard so a future
        # refactor does not silently drop them again.
        "sensor_batch",
        "frame",
        "skill_approval",
        "confirmation_response",
    ],
)
def test_v2026_5_28_outbound_additions_present_on_android(type_name: str) -> None:
    android_src = ANDROID_BRIDGE.read_text()
    assert (
        f'put("type", "{type_name}")' in android_src
        or f"put(\"type\", \"{type_name}\")" in android_src
    ), f"Android lost the v2026.5.28 `{type_name}` outbound HUP path"


@pytest.mark.parametrize(
    "type_name",
    [
        # New v2026.5.28 inbound branches — explicit guard so a future
        # refactor does not silently drop them again.
        "registered",
        "skill_proposal",
        "confirmation_required",
    ],
)
def test_v2026_5_28_inbound_additions_present_on_android(type_name: str) -> None:
    android_src = ANDROID_BRIDGE.read_text()
    assert (
        f'"{type_name}" ->' in android_src
    ), f"Android lost the v2026.5.28 `{type_name}` inbound HUP branch"


def test_v2_tokens_kt_exists_in_bridge() -> None:
    """v2026.5.28 ported FeralV2Tokens.swift to FeralV2Tokens.kt."""
    tokens_kt = (
        ASOS_ROOT
        / "feral-nodes"
        / "android-bridge"
        / "bridge"
        / "src"
        / "main"
        / "java"
        / "ai"
        / "feral"
        / "bridge"
        / "FeralV2Tokens.kt"
    )
    tokens_swift = (
        ASOS_ROOT / "feral-nodes" / "ios-app" / "App" / "FeralV2Tokens.swift"
    )
    assert tokens_kt.exists(), (
        f"missing Android tokens file at {tokens_kt} — V2_MOBILE_PORTING.md "
        "§1 requires iOS + Android + web tokens to ship together"
    )
    assert tokens_swift.exists(), f"missing iOS tokens file at {tokens_swift}"
