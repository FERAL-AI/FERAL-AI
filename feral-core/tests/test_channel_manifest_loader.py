""" — bundled-manifest discovery + capability registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from channels.loader import (
    BUNDLED_CHANNELS_DIR,
    CapabilityRegistry,
    discover_bundled,
)
from channels.manifest import ManifestSchemaError, load_manifest_dict


def _make_manifest(
    channel_id: str,
    *,
    capabilities: dict | None = None,
    providers: list | None = None,
    extra: dict | None = None,
) -> dict:
    prov = providers[0] if providers else channel_id
    base = {
        "id": channel_id,
        "providers": providers or [channel_id],
        "providerAuthEnvVars": {
            prov: [f"FERAL_{prov.upper().replace('-', '_')}_TOKEN"]
        },
        "capabilities": capabilities or {"messagingProvider": True},
    }
    if extra:
        base.update(extra)
    return base


class TestDiscoverBundled:
    def test_discovers_bundled_telegram_manifest(self) -> None:
        manifests = discover_bundled()
        ids = [m.id for m in manifests]
        assert "telegram" in ids, f"telegram missing from {ids}"

    def test_discover_returns_empty_when_root_has_no_manifests(self, tmp_path: Path) -> None:
        assert discover_bundled(root=tmp_path) == []

    def test_discover_skips_non_directories(self, tmp_path: Path) -> None:
        (tmp_path / "not-a-channel.txt").write_text("hi", encoding="utf-8")
        assert discover_bundled(root=tmp_path) == []

    def test_discover_skips_directory_without_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "empty-channel").mkdir()
        assert discover_bundled(root=tmp_path) == []

    def test_discover_loads_multiple_manifests_in_sorted_order(self, tmp_path: Path) -> None:
        for cid in ("zeta", "alpha", "mike"):
            d = tmp_path / cid
            d.mkdir()
            (d / "feral-channel.manifest.json").write_text(
                json.dumps(_make_manifest(cid)),
                encoding="utf-8",
            )
        manifests = discover_bundled(root=tmp_path)
        # Sorted by directory name → alpha, mike, zeta
        assert [m.id for m in manifests] == ["alpha", "mike", "zeta"]

    def test_discover_aborts_on_malformed_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / "feral-channel.manifest.json").write_text(
            "{not json", encoding="utf-8"
        )
        with pytest.raises(ManifestSchemaError):
            discover_bundled(root=tmp_path)


class TestCapabilityRegistry:
    def _build(self, *defs: dict) -> CapabilityRegistry:
        return CapabilityRegistry.build_from(load_manifest_dict(d) for d in defs)

    def test_registry_indexes_telegram_in_messaging_providers(self) -> None:
        registry = CapabilityRegistry.build_from(discover_bundled())
        assert "telegram" in registry.messaging_providers()
        assert registry.get("telegram") is not None
        assert "telegram" in registry

    def test_messaging_providers_filters_by_capability(self) -> None:
        registry = self._build(
            _make_manifest("telegram", capabilities={"messagingProvider": True}),
            _make_manifest("voiceonly", capabilities={"voiceProvider": True}),
        )
        assert registry.messaging_providers() == ("telegram",)
        assert registry.voice_providers() == ("voiceonly",)
        assert registry.file_providers() == ()
        assert registry.webhook_providers() == ()

    def test_registry_reports_capabilities_for_compound_channels(self) -> None:
        registry = self._build(
            _make_manifest(
                "feishu",
                providers=["feishu"],
                capabilities={
                    "messagingProvider": True,
                    "fileProvider": True,
                    "voiceProvider": False,
                },
            ),
        )
        assert "feishu" in registry.messaging_providers()
        assert "feishu" in registry.file_providers()
        assert "feishu" not in registry.voice_providers()  # explicitly false

    def test_registry_dedupes_providers_across_manifests(self) -> None:
        registry = self._build(
            _make_manifest(
                "telegram",
                providers=["telegram"],
                capabilities={"messagingProvider": True},
            ),
            _make_manifest(
                "telegram-voice",
                providers=["telegram"],
                capabilities={"messagingProvider": True, "voiceProvider": True},
            ),
        )
        # Same provider listed twice in messaging — registry collapses it
        assert registry.messaging_providers() == ("telegram",)
        assert registry.voice_providers() == ("telegram",)
        # But the channel ids stay distinct
        assert sorted(registry.channels_with_capability("messagingProvider")) == [
            "telegram",
            "telegram-voice",
        ]

    def test_registry_rejects_duplicate_channel_ids(self) -> None:
        with pytest.raises(ManifestSchemaError):
            self._build(
                _make_manifest("telegram"),
                _make_manifest("telegram"),
            )

    def test_registry_iteration_and_len(self) -> None:
        registry = self._build(_make_manifest("a"), _make_manifest("b"))
        assert len(registry) == 2
        assert {m.id for m in registry} == {"a", "b"}
        assert sorted(registry.ids()) == ["a", "b"]

    def test_capability_map_round_trip(self) -> None:
        registry = self._build(
            _make_manifest(
                "telegram",
                capabilities={"messagingProvider": True, "fileProvider": True},
            )
        )
        m = registry.as_capability_map()
        assert m["messaging"] == ["telegram"]
        assert m["file"] == ["telegram"]


def test_module_level_bundled_dir_is_correct() -> None:
    assert BUNDLED_CHANNELS_DIR.name == "channels"
    assert (BUNDLED_CHANNELS_DIR / "telegram" / "feral-channel.manifest.json").is_file()
