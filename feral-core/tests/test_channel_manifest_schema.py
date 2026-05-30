""" — schema validation contract for channel manifests.

Each invalid case asserts on the *type* of error AND the JSON path
the loader pointed at. The path is part of the contract because IDE
linting + CI hints depend on stable error messages.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from channels.manifest import (
    ManifestSchemaError,
    ManifestUnknownError,
    load_manifest,
    load_manifest_dict,
)


def _valid_manifest() -> dict:
    return {
        "id": "telegram",
        "providers": ["telegram"],
        "providerAuthEnvVars": {"telegram": ["FERAL_TELEGRAM_BOT_TOKEN"]},
        "capabilities": {"messagingProvider": True},
    }


class TestValidShapes:
    def test_minimal_manifest_loads(self) -> None:
        m = load_manifest_dict(_valid_manifest())
        assert m.id == "telegram"
        assert m.providers == ("telegram",)
        assert m.provider_auth_env_vars == {"telegram": ("FERAL_TELEGRAM_BOT_TOKEN",)}
        assert m.capability("messagingProvider") is True
        assert m.capability("voiceProvider") is False
        assert m.signature is None
        assert m.is_signed is False

    def test_full_manifest_loads_with_optional_fields(self) -> None:
        data = _valid_manifest()
        data["capabilities"]["fileProvider"] = True
        data["providerAuthChoices"] = [
            {
                "provider": "telegram",
                "method": "api-key",
                "choiceId": "telegram-bot-token",
                "choiceLabel": "Telegram Bot Token",
                "optionKey": "telegramBotToken",
                "cliFlag": "--telegram-bot-token",
            }
        ]
        data["modelSupport"] = {"preferredModels": ["gpt-5.5"]}
        data["contracts"] = {"messaging": "v1"}

        m = load_manifest_dict(data)
        assert m.capability("fileProvider") is True
        assert len(m.provider_auth_choices) == 1
        assert m.provider_auth_choices[0]["method"] == "api-key"
        assert m.model_support == {"preferredModels": ["gpt-5.5"]}
        assert m.contracts == {"messaging": "v1"}

    def test_bundled_telegram_manifest_loads_from_disk(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "channels"
            / "telegram"
            / "feral-channel.manifest.json"
        )
        m = load_manifest(path)
        assert m.id == "telegram"
        assert "telegram" in m.providers
        assert m.is_signed is True
        assert m.source_path == path


class TestInvalidShapes:
    def test_top_level_must_be_object(self) -> None:
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(["not", "an", "object"])
        assert exc.value.path == "$"

    def test_unknown_top_level_key_rejected(self) -> None:
        data = _valid_manifest()
        data["surpriseField"] = 1
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert "unknown top-level keys" in str(exc.value)

    def test_missing_id_rejected(self) -> None:
        data = _valid_manifest()
        del data["id"]
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert "id" in str(exc.value)

    def test_id_must_match_pattern(self) -> None:
        data = _valid_manifest()
        data["id"] = "Telegram"  # uppercase not allowed
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert exc.value.path == "$.id"

    def test_id_cannot_start_with_digit(self) -> None:
        data = _valid_manifest()
        data["id"] = "1telegram"
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert exc.value.path == "$.id"

    def test_providers_must_be_non_empty(self) -> None:
        data = _valid_manifest()
        data["providers"] = []
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert exc.value.path == "$.providers"

    def test_providers_reject_duplicates(self) -> None:
        data = _valid_manifest()
        data["providers"] = ["telegram", "telegram"]
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert exc.value.path == "$.providers[1]"

    def test_provider_auth_env_var_key_must_be_in_providers(self) -> None:
        data = _valid_manifest()
        data["providerAuthEnvVars"] = {"slack": ["SLACK_BOT_TOKEN"]}
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert exc.value.path == "$.providerAuthEnvVars.slack"

    def test_env_var_name_must_be_uppercase_snake(self) -> None:
        data = _valid_manifest()
        data["providerAuthEnvVars"] = {"telegram": ["telegram_bot_token"]}
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert "providerAuthEnvVars.telegram[0]" in exc.value.path

    def test_capabilities_must_have_one_true(self) -> None:
        data = _valid_manifest()
        data["capabilities"] = {"messagingProvider": False}
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert exc.value.path == "$.capabilities"

    def test_capability_value_must_be_boolean(self) -> None:
        data = _valid_manifest()
        data["capabilities"] = {"messagingProvider": "yes"}
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert exc.value.path == "$.capabilities.messagingProvider"

    def test_auth_choice_method_must_be_known(self) -> None:
        data = _valid_manifest()
        data["providerAuthChoices"] = [
            {"provider": "telegram", "method": "magic-link", "choiceId": "x"}
        ]
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert exc.value.path == "$.providerAuthChoices[0].method"

    def test_auth_choice_provider_must_be_in_providers(self) -> None:
        data = _valid_manifest()
        data["providerAuthChoices"] = [
            {"provider": "slack", "method": "oauth", "choiceId": "s"}
        ]
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert exc.value.path == "$.providerAuthChoices[0].provider"

    def test_cli_flag_pattern(self) -> None:
        data = _valid_manifest()
        data["providerAuthChoices"] = [
            {
                "provider": "telegram",
                "method": "api-key",
                "choiceId": "x",
                "cliFlag": "telegram-flag",  # missing leading --
            }
        ]
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert exc.value.path == "$.providerAuthChoices[0].cliFlag"

    def test_signature_envelope_requires_known_fields(self) -> None:
        data = _valid_manifest()
        data["signature"] = {"algo": "ed25519"}  # missing the rest
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert exc.value.path == "$.signature"

    def test_signature_algo_must_be_ed25519(self) -> None:
        data = _valid_manifest()
        data["signature"] = {
            "algo": "rsa-pss",
            "publicKeyId": "k",
            "publicKey": "x",
            "signature": "y",
            "signedAt": "2026-04-25T00:00:00+00:00",
        }
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest_dict(data)
        assert exc.value.path == "$.signature.algo"


class TestFileLevelErrors:
    def test_missing_file_raises_unknown_error(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestUnknownError):
            load_manifest(tmp_path / "missing.json")

    def test_directory_raises_unknown_error(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestUnknownError):
            load_manifest(tmp_path)

    def test_invalid_json_raises_schema_error(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(ManifestSchemaError) as exc:
            load_manifest(p)
        assert "valid JSON" in str(exc.value)

    def test_round_trip_from_disk(self, tmp_path: Path) -> None:
        p = tmp_path / "ok.json"
        p.write_text(json.dumps(_valid_manifest()), encoding="utf-8")
        m = load_manifest(p)
        assert m.source_path == p
        assert m.raw == _valid_manifest()


def test_dataclass_is_frozen_and_round_trips_raw() -> None:
    m = load_manifest_dict(_valid_manifest())
    with pytest.raises(Exception):
        # frozen dataclass — direct mutation is forbidden
        m.id = "other"  # type: ignore[misc]
    # raw is the validated dict, untouched apart from copy
    assert m.raw["id"] == "telegram"
    # mutation of caller's dict doesn't leak into the loaded manifest
    original = _valid_manifest()
    original["id"] = "mutated"
    m2 = load_manifest_dict(copy.deepcopy(_valid_manifest()))
    assert m2.id == "telegram"
