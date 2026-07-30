from __future__ import annotations

from agents.identity_loader import IdentityLoader


class _Frame:
    connected_nodes = []

    def to_system_context(self) -> str:
        return "No sensor data available."


def _loader() -> IdentityLoader:
    loader = IdentityLoader(memory=None, somatic_engine=None, calendar=None)
    return loader


async def test_system_prompt_uses_canonical_coding_tools_file_path() -> None:
    prompt = await _loader().build_system_prompt(
        _Frame(),
        [],
        session_id="sess-test",
        identity_text="",
        full_catalog=[],
    )

    assert "coding_tools__write_file" in prompt
    assert "coding_tools__bash" in prompt
    assert "permission_needed" in prompt
    assert "desktop_control__shell_command" not in prompt
    assert "python3 -c" not in prompt
