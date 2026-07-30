"""UI result previews are opt-in per endpoint, and clamped for outside skills.

The chat UI gained a shape-aware result renderer, but `ToolResultPayload`
carried no result, so every tool card rendered "Completed with no returned
output". This adds `result_preview`, gated by `emit_result_preview` in the
skill manifest.

Opt-IN rather than opt-out is deliberate: tool results routinely carry vault
reads, API responses holding tokens, file contents and mail bodies, and this
codebase has no redaction pass (no redact/redact_secrets helper exists in
security/ or agents/). An opt-out default would leak by omission the first
time anyone added a credential-touching endpoint.
"""

from __future__ import annotations

import pytest

from skills.registry import SkillRegistry
from skills.result_budget import (
    PREVIEW_MAX_CHARS,
    build_result_preview,
    preview_enabled_for,
    preview_enabled_for_tool,
)


@pytest.fixture(scope="module")
def registry():
    reg = SkillRegistry()
    reg.load_builtin_skills()
    return reg


class TestPreviewGating:
    def test_default_is_off(self):
        """No manifest, no preview."""
        assert preview_enabled_for("anything", "endpoint", None) is False

    def test_unknown_tool_name_is_off(self):
        assert preview_enabled_for_tool("not_a_tool_name", None) is False
        assert preview_enabled_for_tool("", None) is False

    @pytest.mark.parametrize("tool", [
        "coding_tools__read_file",
        "coding_tools__grep_search",
        "coding_tools__bash",
    ])
    def test_opted_in_endpoints_emit(self, registry, tool):
        assert preview_enabled_for_tool(tool, registry) is True

    @pytest.mark.parametrize("tool", [
        "email__search",          # mail bodies
        "notes_memory__search",   # personal memory
    ])
    def test_sensitive_endpoints_stay_off(self, registry, tool):
        assert preview_enabled_for_tool(tool, registry) is False

    def test_marketplace_skill_cannot_opt_itself_in(self):
        """A runtime-installed skill must not be able to exfiltrate results."""
        class FakeEndpoint:
            id = "steal"
            emit_result_preview = True

        class FakeManifest:
            endpoints = [FakeEndpoint()]
            emit_result_preview = True

        # skill_id is not among the manifests shipping in this repo.
        assert preview_enabled_for("evil_marketplace_skill", "steal", FakeManifest()) is False


class TestPreviewBuilding:
    def test_prefers_the_data_envelope(self):
        text, truncated = build_result_preview({"success": True, "data": "hello"})
        assert text == "hello"
        assert truncated is False

    def test_bounded_and_flagged_when_oversized(self):
        text, truncated = build_result_preview({"success": True, "data": "x" * (PREVIEW_MAX_CHARS * 3)})
        assert len(text) == PREVIEW_MAX_CHARS
        assert truncated is True

    def test_none_yields_empty_not_a_crash(self):
        assert build_result_preview(None) == ("", False)

    def test_structured_payloads_are_rendered(self):
        text, _ = build_result_preview({"success": True, "data": {"rows": [1, 2, 3]}})
        assert "rows" in text


class TestPayloadShape:
    def test_payload_defaults_to_no_preview(self):
        from models.protocol import ToolResultPayload

        p = ToolResultPayload(tool="x")
        assert p.result_preview == ""
        assert p.result_preview_truncated is False
