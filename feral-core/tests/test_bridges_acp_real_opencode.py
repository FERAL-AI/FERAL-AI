"""Drive a REAL opencode binary over ACP. Opt-in, skipped by default.

Why this file exists
====================
The rest of the bridge suite talks to a fake agent that this repository
wrote, so it can only prove that the client matches this repository's
reading of the spec. Every interesting defect found while building the
bridge came from the real binary instead, and the sharpest one is pinned
in ``test_client_claims_fs_but_not_terminal``: opencode calls
``fs/write_text_file`` on the client to apply an approved edit whether or
not the client advertised the capability, so the original "advertise
nothing" design failed every granted edit with
``client did not advertise fs capability`` after the human had already
said yes. No mock would have said so.

Running it
==========
Set both variables and point them at a real install::

    FERAL_ACP_REAL_BINARY=/path/to/opencode \\
    FERAL_ACP_REAL_CONFIG=/path/to/opencode.json \\
    pytest tests/test_bridges_acp_real_opencode.py

``FERAL_ACP_REAL_CONFIG`` should select a model the machine can serve for
free (a local Ollama endpoint via ``provider.<name>.options.baseURL``) and
set ``"permission": "ask"`` so a permission request actually fires. The
run is slow: a small local model on modest hardware takes minutes for one
turn, hence ``FERAL_ACP_REAL_TIMEOUT`` defaulting to 25 minutes.

Nothing here touches the caller's opencode state: every XDG directory is
redirected into ``tmp_path``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridges.acp import AcpAgentProcess  # noqa: E402
from bridges.permissions import (  # noqa: E402
    PermissionBroker,
    PermissionDecision,
    reject,
)

BINARY = os.environ.get("FERAL_ACP_REAL_BINARY", "")
CONFIG = os.environ.get("FERAL_ACP_REAL_CONFIG", "")
TIMEOUT = float(os.environ.get("FERAL_ACP_REAL_TIMEOUT", "1500"))

pytestmark = pytest.mark.skipif(
    not (BINARY and os.path.isfile(BINARY) and CONFIG and os.path.isfile(CONFIG)),
    reason=(
        "set FERAL_ACP_REAL_BINARY and FERAL_ACP_REAL_CONFIG to drive a real "
        "opencode over ACP"
    ),
)


class AllowOnceThenDeny(PermissionBroker):
    """Stands in for a human who says yes exactly once. Never blanket-allows."""

    def __init__(self):
        self.seen: list = []

    async def decide(self, request) -> PermissionDecision:
        self.seen.append(request)
        if len(self.seen) > 1:
            return reject(request, "scripted human said no")
        option = request.first_of(("allow_once",))
        if option is None:
            return reject(request, "agent offered no allow_once option")
        return PermissionDecision(
            option_id=option.option_id, allowed=True, reason="scripted human said yes"
        )


def isolated_env(root: Path, workspace: Path) -> dict[str, str]:
    """opencode's whole state, redirected away from the caller's machine."""
    env = dict(os.environ)
    env.update(
        {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"),
            "OPENCODE_TEST_HOME": str(root),
            "OPENCODE_CONFIG": CONFIG,
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            # opencode resolves its project root from PWD, not from the
            # inherited cwd, so without this it adopts the caller's repo.
            "PWD": str(workspace),
        }
    )
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        env.pop(key, None)
    return env


async def test_a_real_opencode_session_end_to_end(tmp_path):
    """initialize, session/new, prompt, stream, tool call, permission, write."""
    root = tmp_path / "ocroot"
    workspace = tmp_path / "workspace"
    root.mkdir()
    workspace.mkdir()

    broker = AllowOnceThenDeny()
    proc = await AcpAgentProcess.spawn(
        [BINARY, "acp"],
        cwd=str(workspace),
        env=isolated_env(root, workspace),
        broker=broker,
    )
    try:
        info = await proc.initialize(client_name="feral", client_version="test")
        assert info["protocolVersion"] == 1
        assert proc.negotiated_version == 1
        assert proc.agent_info.get("name")

        session = await proc.new_session(str(workspace))
        assert session.session_id

        result = await session.prompt(
            "Create a file hello.txt containing the word ferrous. /no_think",
            timeout=TIMEOUT,
        )

        assert broker.seen, (
            "the agent never asked permission; check that the config sets "
            '"permission": "ask"'
        )
        request = broker.seen[0]
        assert {o.kind for o in request.options} >= {"allow_once", "reject_once"}

        assert result.events, "no session/update notifications arrived"
        assert any(e.is_tool_call for e in result.events), (
            f"no tool call in {[e.kind for e in result.events]}"
        )

        written = workspace / "hello.txt"
        assert written.exists(), (
            "the granted edit never reached fs/write_text_file; agent stderr:\n"
            + proc.stderr_tail[-2000:]
        )
        assert "ferrous" in written.read_text()
    finally:
        await proc.close()
