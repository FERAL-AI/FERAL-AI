"""Audit-r9 regression test: web + phone share `state.primary_session_id`.

Operator report 2026-05-10:
> "the chat and memory should be the same for my phone chat and the
>  webui for feral brain on the local brain right?"

Yes. Until this fix, web `/v1/session` minted `uuid4()` per WebSocket
(`api/server.py:835`) and phone `chat_request` used `phone-{node_id}`
(`api/server.py:1486`). Result: `Orchestrator.conversation_history[
session_id]` and the working-memory deque were partitioned per
surface AND per browser tab.

Now both default to `state.primary_session_id` — a stable per-install
id minted on first boot and persisted at
`<feral_data_home>/primary_session_id`. Explicit client-supplied
`session_id` still wins.

These tests pin:
1. Persistence — the id survives a `BrainState` rebuild.
2. Env override — `FERAL_PRIMARY_SESSION_ID` short-circuits the file.
3. Filesystem-failure fallback — never blocks boot.
4. The phone `chat_request` resolver picks primary over the legacy
   `phone-{node_id}` fallback.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


pytestmark = pytest.mark.no_auto_feral_home


def _make_state_class():
    """Import `BrainState` lazily so the autouse fixtures can isolate
    the data home BEFORE the class loads its module-level imports."""
    from api.state import BrainState
    return BrainState


def test_primary_session_id_minted_and_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """First boot mints + persists; second boot reads back the same id."""
    # `feral_data_home()` now honors `FERAL_HOME` first, exactly as
    # `feral_home()` does. Before that, relocating an install split the
    # brain (settings under the new root, databases still under
    # `~/.feral`) and setting `FERAL_HOME` alone in a test left data
    # writes pointing at the developer's real home directory.
    # `XDG_DATA_HOME` is still set here so the assertion below stays
    # unambiguous about which of the two won.
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_PRIMARY_SESSION_ID", raising=False)

    BrainState = _make_state_class()
    s1 = BrainState()
    sid_1 = s1.primary_session_id
    assert sid_1, "primary_session_id must be minted on first boot"
    assert sid_1.startswith("primary-"), f"unexpected format: {sid_1}"

    # `FERAL_HOME` wins, so the file lands directly under it rather
    # than under the `XDG_DATA_HOME / "feral"` layout.
    persisted = tmp_path / "primary_session_id"
    assert not (tmp_path / "feral" / "primary_session_id").exists(), (
        "FERAL_HOME must take precedence over XDG_DATA_HOME"
    )
    assert persisted.is_file(), (
        f"primary_session_id must be persisted on disk; expected at {persisted}"
    )
    assert persisted.read_text().strip() == sid_1

    # Second boot reads the same id.
    s2 = BrainState()
    assert s2.primary_session_id == sid_1


def test_env_override_short_circuits_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`FERAL_PRIMARY_SESSION_ID` lets tests + integration runs pin a
    deterministic id without touching the filesystem."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("FERAL_PRIMARY_SESSION_ID", "my-test-primary-id")

    BrainState = _make_state_class()
    s = BrainState()
    assert s.primary_session_id == "my-test-primary-id"


def test_filesystem_failure_falls_back_to_ephemeral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """If the data home is unwritable, boot must succeed with an
    ephemeral id rather than crash. Operator's brain CANNOT fail to
    boot just because a config file can't be written."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_PRIMARY_SESSION_ID", raising=False)

    BrainState = _make_state_class()
    with patch("api.state.feral_data_home") as mocked:
        mocked.side_effect = PermissionError("read-only fs")
        s = BrainState()
        assert s.primary_session_id.startswith("primary-ephemeral-")


def test_chat_request_resolves_to_primary_when_payload_omits_session_id():
    """The phone `chat_request` handler in `api/server.py:1486` MUST
    resolve `target_sid` to `state.primary_session_id` when the
    payload doesn't include an explicit `session_id`. Pinned as a
    static check on the resolver expression so a future refactor that
    drops the `getattr(state, "primary_session_id", "")` chain
    silently regresses to per-phone partitioning.
    """
    import inspect
    from api import server as server_mod

    # Find the chat_request handler block in the daemon WebSocket
    # endpoint. The exact line number drifts; locate by searching the
    # source for the resolver expression.
    src = inspect.getsource(server_mod)
    assert "primary_session_id" in src, (
        "api/server.py must reference state.primary_session_id in the "
        "chat_request resolver — without it, phone chat partitions "
        "from web chat by default. See audit-r9."
    )
    # Confirm the resolver picks payload first, then primary, then
    # phone-{node_id} fallback (the order the comment promises).
    assert (
        'payload_dict.get("session_id"' in src
        and "primary_session_id" in src
        and "phone-" in src
    ), "chat_request resolver order regressed; see api/server.py:1486"


def test_web_session_resolves_to_primary_when_query_omits_session_id():
    """The web `/v1/session` handler in `api/server.py:835` MUST
    resolve to `state.primary_session_id` when the query string
    doesn't include `session_id=`. Same static-check guard as the
    phone resolver above.
    """
    import inspect
    from api import server as server_mod

    src = inspect.getsource(server_mod)
    # The web mint site is in `dashboard_websocket` (or similar). The
    # contract: `requested_sid or primary_session_id or uuid4()`.
    assert "requested_sid" in src and "primary_session_id" in src, (
        "api/server.py /v1/session must default session_id to "
        "state.primary_session_id — without it, web tabs partition "
        "from each other AND from iOS chat."
    )
