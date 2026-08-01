"""The ACP client, driven against a real subprocess that speaks ACP.

These are not mocks. ``FAKE_AGENT`` is written to a temp file and spawned
as a genuine child process; every assertion below travels over a real
pipe as newline-delimited JSON. That matters because the two defects this
client is most likely to have are both invisible to a mock:

* a read loop that awaits its own handler deadlocks the moment an agent
  asks permission mid-turn, because the ``session/prompt`` response can
  never arrive while the handler is blocked;
* a stderr pipe that is never drained wedges the child once the OS
  buffer fills, which looks like an agent that went quiet for no reason.

The fake agent reproduces both shapes: it asks permission in the middle
of a prompt turn and it writes a large amount to stderr.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridges.acp import (  # noqa: E402
    PROTOCOL_VERSION,
    AcpAgentProcess,
    AcpProtocolError,
    parse_session_update,
)
from bridges.jsonrpc import JsonRpcError  # noqa: E402
from bridges.permissions import (  # noqa: E402
    DenyAllBroker,
    PermissionBroker,
    PermissionDecision,
    reject,
)

# ``asyncio_mode = "auto"`` in pyproject.toml already collects the async
# tests below; a module-level asyncio mark would only warn on the sync
# parsing tests that share this file.

# A minimal but honest ACP agent. Method names come from
# schema/v1/meta.json in the agent-client-protocol repository.
FAKE_AGENT = r'''
import json, sys, threading

OUT = sys.stdout
LOCK = threading.Lock()

def send(obj):
    with LOCK:
        OUT.write(json.dumps(obj) + "\n")
        OUT.flush()

def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})

def result(rid, payload):
    send({"jsonrpc": "2.0", "id": rid, "result": payload})

# Enough stderr to overflow a 64 KiB pipe buffer if nobody drains it.
sys.stderr.write("boot\n" * 20000)
sys.stderr.flush()

PENDING = {}
NEXT_ID = [1000]

def handle(msg):
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if "result" in msg or "error" in msg:
        fut = PENDING.pop(msg.get("id"), None)
        if fut is not None:
            fut(msg)
        return

    if method == "initialize":
        result(rid, {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True},
            "agentInfo": {"name": "fake", "version": "0.1"},
        })
    elif method == "session/new":
        result(rid, {"sessionId": "sess-1"})
    elif method == "session/prompt":
        threading.Thread(target=run_turn, args=(rid, params), daemon=True).start()
    elif method == "session/cancel":
        pass
    elif method == "session/close":
        result(rid, {})
    elif method is not None and rid is not None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601, "message": "no such method"}})

def run_turn(rid, params):
    sid = params.get("sessionId")
    notify("session/update", {"sessionId": sid, "update": {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "thinking about it. "},
    }})
    notify("session/update", {"sessionId": sid, "update": {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-1",
        "title": "Write hello.txt",
        "kind": "edit",
        "status": "pending",
    }})

    # Ask permission and BLOCK until answered. A client whose read loop
    # awaits its handlers can never get here without deadlocking.
    done = threading.Event()
    box = {}
    perm_id = NEXT_ID[0]; NEXT_ID[0] += 1
    PENDING[perm_id] = lambda m: (box.update(m), done.set())
    send({"jsonrpc": "2.0", "id": perm_id, "method": "session/request_permission",
          "params": {
              "sessionId": sid,
              "toolCall": {"toolCallId": "tc-1", "title": "Write hello.txt",
                           "kind": "edit", "toolName": "write"},
              "options": [
                  {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
                  {"optionId": "always", "name": "Always allow", "kind": "allow_always"},
                  {"optionId": "no", "name": "Reject", "kind": "reject_once"},
              ],
          }})
    done.wait(30)
    outcome = ((box.get("result") or {}).get("outcome") or {})
    picked = outcome.get("optionId") if outcome.get("outcome") == "selected" else None
    allowed = picked in ("once", "always")

    notify("session/update", {"sessionId": sid, "update": {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc-1",
        "status": "completed" if allowed else "failed",
        "toolName": "write",
    }})
    notify("session/update", {"sessionId": sid, "update": {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text",
                    "text": "wrote it." if allowed else "refused."},
    }})
    result(rid, {"stopReason": "end_turn"})

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    handle(json.loads(line))
'''


class RecordingBroker(PermissionBroker):
    """Stands in for the human. Answers with the option kind it was told to."""

    def __init__(self, kind: str = "allow_once"):
        self.kind = kind
        self.seen: list = []

    async def decide(self, request) -> PermissionDecision:
        self.seen.append(request)
        option = request.first_of((self.kind,))
        if option is None:
            return reject(request, "no such option")
        return PermissionDecision(
            option_id=option.option_id,
            allowed=option.allows,
            reason="test",
        )


@pytest.fixture
def agent_script(tmp_path) -> str:
    path = tmp_path / "fake_acp_agent.py"
    path.write_text(FAKE_AGENT)
    return str(path)


async def _spawn(agent_script, workspace, broker):
    return await AcpAgentProcess.spawn(
        [sys.executable, "-u", agent_script], cwd=str(workspace), broker=broker
    )


class TestHandshake:
    async def test_initialize_negotiates_and_records_capabilities(
        self, agent_script, tmp_path
    ):
        proc = await _spawn(agent_script, tmp_path, DenyAllBroker())
        try:
            result = await proc.initialize()
            assert result["protocolVersion"] == PROTOCOL_VERSION
            assert proc.negotiated_version == PROTOCOL_VERSION
            assert proc.agent_capabilities == {"loadSession": True}
            assert proc.agent_info["name"] == "fake"
        finally:
            await proc.close()

    async def test_client_claims_fs_but_not_terminal(
        self, agent_script, tmp_path, monkeypatch
    ):
        """Pinned against a regression found by driving a real opencode.

        opencode calls ``fs/write_text_file`` to apply an approved edit
        whether or not the client advertised the capability, so declaring
        it false made every granted edit fail with
        ``client did not advertise fs capability``.
        """
        proc = await _spawn(agent_script, tmp_path, DenyAllBroker())
        sent = {}
        original = proc.peer.request

        async def spy(method, params=None, **kwargs):
            sent[method] = params
            return await original(method, params, **kwargs)

        monkeypatch.setattr(proc.peer, "request", spy)
        try:
            await proc.initialize()
            caps = sent["initialize"]["clientCapabilities"]
            assert caps["fs"] == {"readTextFile": True, "writeTextFile": True}
            assert caps["terminal"] is False
        finally:
            await proc.close()

    async def test_missing_binary_is_a_clear_error(self, tmp_path):
        with pytest.raises(AcpProtocolError, match="not found"):
            await AcpAgentProcess.spawn(
                ["/nonexistent/definitely-not-here"], cwd=str(tmp_path)
            )

    async def test_session_new_rejects_a_missing_cwd(self, agent_script, tmp_path):
        proc = await _spawn(agent_script, tmp_path, DenyAllBroker())
        try:
            await proc.initialize()
            with pytest.raises(AcpProtocolError, match="does not exist"):
                await proc.new_session(str(tmp_path / "nope"))
        finally:
            await proc.close()


class TestPromptTurn:
    async def test_streams_events_and_answers_permission_without_deadlock(
        self, agent_script, tmp_path
    ):
        broker = RecordingBroker("allow_once")
        proc = await _spawn(agent_script, tmp_path, broker)
        try:
            await proc.initialize()
            session = await proc.new_session(str(tmp_path))
            result = await session.prompt("write hello.txt", timeout=45)

            assert result.stop_reason == "end_turn"
            assert "thinking about it." in result.text
            assert "wrote it." in result.text

            # The permission actually reached the broker, with the tool
            # name and all three options intact.
            assert len(broker.seen) == 1
            request = broker.seen[0]
            assert request.tool_name == "write"
            assert {o.kind for o in request.options} == {
                "allow_once", "allow_always", "reject_once"
            }

            calls = result.tool_calls
            assert len(calls) == 1
            assert calls[0].tool_call_id == "tc-1"
            assert calls[0].status == "completed"
        finally:
            await proc.close()

    async def test_rejecting_is_visible_to_the_agent(self, agent_script, tmp_path):
        broker = RecordingBroker("reject_once")
        proc = await _spawn(agent_script, tmp_path, broker)
        try:
            await proc.initialize()
            session = await proc.new_session(str(tmp_path))
            result = await session.prompt("write hello.txt", timeout=45)
            assert "refused." in result.text
            assert result.tool_calls[0].status == "failed"
        finally:
            await proc.close()

    async def test_deny_all_broker_is_the_default(self, agent_script, tmp_path):
        """A bridge with nobody wired in must do nothing, not everything."""
        proc = await _spawn(agent_script, tmp_path, DenyAllBroker())
        try:
            await proc.initialize()
            session = await proc.new_session(str(tmp_path))
            result = await session.prompt("write hello.txt", timeout=45)
            assert "refused." in result.text
        finally:
            await proc.close()

    async def test_broker_that_raises_denies_rather_than_allows(
        self, agent_script, tmp_path
    ):
        class Exploding(PermissionBroker):
            async def decide(self, request):
                raise RuntimeError("approval surface is on fire")

        proc = await _spawn(agent_script, tmp_path, Exploding())
        try:
            await proc.initialize()
            session = await proc.new_session(str(tmp_path))
            result = await session.prompt("write hello.txt", timeout=45)
            assert "refused." in result.text
        finally:
            await proc.close()

    async def test_stderr_is_drained_so_the_child_never_wedges(
        self, agent_script, tmp_path
    ):
        """The fake agent writes ~100 KB to stderr before answering."""
        proc = await _spawn(agent_script, tmp_path, RecordingBroker())
        try:
            await proc.initialize()
            session = await proc.new_session(str(tmp_path))
            result = await session.prompt("go", timeout=45)
            assert result.stop_reason == "end_turn"
            assert "boot" in proc.stderr_tail
        finally:
            await proc.close()


class TestFilesystemOnBehalfOfTheAgent:
    """Every agent-driven read/write must stay inside the workspace."""

    @pytest.fixture
    async def session(self, agent_script, tmp_path):
        workspace = tmp_path / "repo"
        workspace.mkdir()
        proc = await _spawn(agent_script, workspace, DenyAllBroker())
        await proc.initialize()
        acp_session = await proc.new_session(str(workspace))
        yield proc, acp_session, workspace
        await proc.close()

    async def test_write_then_read_round_trips(self, session):
        proc, acp_session, workspace = session
        await proc._on_request(
            "fs/write_text_file",
            {"sessionId": acp_session.session_id,
             "path": str(workspace / "a" / "b.txt"),
             "content": "ferrous\n"},
        )
        assert (workspace / "a" / "b.txt").read_text() == "ferrous\n"

        result = await proc._on_request(
            "fs/read_text_file",
            {"sessionId": acp_session.session_id,
             "path": str(workspace / "a" / "b.txt")},
        )
        assert result == {"content": "ferrous\n"}

    async def test_read_honours_line_and_limit(self, session):
        proc, acp_session, workspace = session
        (workspace / "many.txt").write_text("1\n2\n3\n4\n5\n")
        result = await proc._on_request(
            "fs/read_text_file",
            {"sessionId": acp_session.session_id,
             "path": str(workspace / "many.txt"), "line": 2, "limit": 2},
        )
        assert result == {"content": "2\n3\n"}

    async def test_a_path_outside_the_workspace_is_refused(self, session):
        proc, acp_session, _workspace = session
        with pytest.raises(JsonRpcError, match="escapes the session workspace"):
            await proc._on_request(
                "fs/read_text_file",
                {"sessionId": acp_session.session_id, "path": "/etc/passwd"},
            )

    async def test_dot_dot_traversal_is_refused(self, session):
        proc, acp_session, workspace = session
        with pytest.raises(JsonRpcError, match="escapes the session workspace"):
            await proc._on_request(
                "fs/write_text_file",
                {"sessionId": acp_session.session_id,
                 "path": str(workspace / ".." / "escaped.txt"),
                 "content": "nope"},
            )
        assert not (workspace.parent / "escaped.txt").exists()

    async def test_a_symlink_out_of_the_workspace_is_refused(self, session, tmp_path):
        proc, acp_session, workspace = session
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        (workspace / "link.txt").symlink_to(outside)
        with pytest.raises(JsonRpcError, match="escapes the session workspace"):
            await proc._on_request(
                "fs/read_text_file",
                {"sessionId": acp_session.session_id,
                 "path": str(workspace / "link.txt")},
            )

    async def test_an_unknown_session_id_is_refused(self, session):
        proc, _acp_session, workspace = session
        with pytest.raises(JsonRpcError, match="unknown sessionId"):
            await proc._on_request(
                "fs/read_text_file",
                {"sessionId": "not-a-session", "path": str(workspace / "x")},
            )

    async def test_a_relative_path_resolves_against_the_workspace(self, session):
        proc, acp_session, workspace = session
        await proc._on_request(
            "fs/write_text_file",
            {"sessionId": acp_session.session_id,
             "path": "rel.txt", "content": "ok"},
        )
        assert (workspace / "rel.txt").read_text() == "ok"

    async def test_non_string_content_is_refused(self, session):
        proc, acp_session, workspace = session
        with pytest.raises(JsonRpcError, match="content must be a string"):
            await proc._on_request(
                "fs/write_text_file",
                {"sessionId": acp_session.session_id,
                 "path": str(workspace / "x.txt"), "content": {"not": "a string"}},
            )


class TestClientMethodsWeDoNotImplement:
    async def test_unknown_client_method_is_refused(self, agent_script, tmp_path):
        proc = await _spawn(agent_script, tmp_path, DenyAllBroker())
        try:
            with pytest.raises(JsonRpcError, match="unsupported"):
                await proc._on_request("terminal/create", {})
        finally:
            await proc.close()

    async def test_permission_request_with_no_options_is_invalid(
        self, agent_script, tmp_path
    ):
        proc = await _spawn(agent_script, tmp_path, DenyAllBroker())
        try:
            with pytest.raises(JsonRpcError, match="no options"):
                await proc._on_request(
                    "session/request_permission",
                    {"sessionId": "s", "toolCall": {}, "options": []},
                )
        finally:
            await proc.close()


class TestTeardown:
    async def test_close_kills_the_child(self, agent_script, tmp_path):
        proc = await _spawn(agent_script, tmp_path, DenyAllBroker())
        await proc.initialize()
        assert proc.alive
        await proc.close()
        assert not proc.alive
        assert proc.proc.returncode is not None

    async def test_pending_requests_fail_when_the_peer_closes(
        self, agent_script, tmp_path
    ):
        proc = await _spawn(agent_script, tmp_path, DenyAllBroker())
        await proc.initialize()
        await proc.peer.close("test teardown")
        with pytest.raises(JsonRpcError, match="test teardown"):
            await proc.peer.request("initialize", {})
        await proc.close()


class TestUpdateParsing:
    """Flattening must never lose the raw payload."""

    def test_unknown_update_kind_survives(self):
        event = parse_session_update(
            "s", {"sessionUpdate": "some_future_variant", "widget": 3}
        )
        assert event.kind == "some_future_variant"
        assert event.raw["widget"] == 3

    def test_tool_call_prefers_tool_name_then_kind_then_title(self):
        by_name = parse_session_update(
            "s", {"sessionUpdate": "tool_call", "toolName": "bash", "kind": "execute"}
        )
        assert by_name.tool_name == "bash"
        by_kind = parse_session_update(
            "s", {"sessionUpdate": "tool_call", "kind": "execute"}
        )
        assert by_kind.tool_name == "execute"
        by_title = parse_session_update(
            "s", {"sessionUpdate": "tool_call", "title": "Run tests"}
        )
        assert by_title.tool_name == "Run tests"

    def test_plan_entries_are_flattened(self):
        event = parse_session_update(
            "s",
            {
                "sessionUpdate": "plan",
                "entries": [{"content": "step one"}, {"content": "step two"}],
            },
        )
        assert event.text == "step one\nstep two"


class TestFraming:
    """JSON-RPC framing details that only bite in production."""

    async def test_a_non_json_line_does_not_kill_the_session(self, tmp_path):
        script = tmp_path / "chatty.py"
        script.write_text(
            "import json, sys\n"
            "sys.stdout.write('opencode v1 starting up\\n'); sys.stdout.flush()\n"
            "for line in sys.stdin:\n"
            "    msg = json.loads(line)\n"
            "    sys.stdout.write(json.dumps("
            "{'jsonrpc':'2.0','id':msg['id'],"
            "'result':{'protocolVersion':1}}) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )
        proc = await AcpAgentProcess.spawn(
            [sys.executable, "-u", str(script)], cwd=str(tmp_path)
        )
        try:
            result = await proc.initialize(timeout=20)
            assert result["protocolVersion"] == 1
        finally:
            await proc.close()

    async def test_a_line_far_larger_than_the_default_limit_survives(self, tmp_path):
        """asyncio's 64 KiB default would raise LimitOverrunError here."""
        script = tmp_path / "big.py"
        script.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    msg = json.loads(line)\n"
            "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':msg['id'],"
            "'result':{'protocolVersion':1,'blob':'x'*500000}}) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )
        proc = await AcpAgentProcess.spawn(
            [sys.executable, "-u", str(script)], cwd=str(tmp_path)
        )
        try:
            result = await proc.initialize(timeout=20)
            assert len(result["blob"]) == 500000
        finally:
            await proc.close()

    async def test_an_agent_negotiating_a_newer_major_is_refused(self, tmp_path):
        script = tmp_path / "future.py"
        script.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    msg = json.loads(line)\n"
            "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':msg['id'],"
            "'result':{'protocolVersion':99}}) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )
        proc = await AcpAgentProcess.spawn(
            [sys.executable, "-u", str(script)], cwd=str(tmp_path)
        )
        try:
            with pytest.raises(AcpProtocolError, match="v99"):
                await proc.initialize(timeout=20)
        finally:
            await proc.close()

    async def test_json_rpc_errors_from_the_agent_are_raised(self, tmp_path):
        script = tmp_path / "grumpy.py"
        script.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    msg = json.loads(line)\n"
            "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':msg['id'],"
            "'error':{'code':-32000,'message':'not authenticated'}}) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )
        proc = await AcpAgentProcess.spawn(
            [sys.executable, "-u", str(script)], cwd=str(tmp_path)
        )
        try:
            with pytest.raises(JsonRpcError, match="not authenticated"):
                await proc.initialize(timeout=20)
        finally:
            await proc.close()

    async def test_a_silent_agent_times_out_rather_than_hanging(self, tmp_path):
        script = tmp_path / "mute.py"
        script.write_text("import sys, time\nsys.stdin.readline()\ntime.sleep(60)\n")
        proc = await AcpAgentProcess.spawn(
            [sys.executable, "-u", str(script)], cwd=str(tmp_path)
        )
        try:
            with pytest.raises(asyncio.TimeoutError):
                await proc.initialize(timeout=2)
        finally:
            await proc.close()

    async def test_messages_are_one_json_object_per_line(self, tmp_path):
        """The wire format itself, asserted on raw bytes."""
        script = tmp_path / "echo.py"
        script.write_text(
            "import sys, json\n"
            "line = sys.stdin.readline()\n"
            "sys.stderr.write(repr(line) + '\\n')\n"
            "sys.stderr.flush()\n"
            "msg = json.loads(line)\n"
            "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':msg['id'],"
            "'result':{'protocolVersion':1}}) + '\\n')\n"
            "sys.stdout.flush()\n"
            "import time; time.sleep(1)\n"
        )
        proc = await AcpAgentProcess.spawn(
            [sys.executable, "-u", str(script)], cwd=str(tmp_path)
        )
        try:
            await proc.initialize(timeout=20)
            await asyncio.sleep(0.3)
            # The child echoed repr() of the exact line it read, so this
            # asserts the framing on the bytes that crossed the pipe.
            line = ast.literal_eval(proc.stderr_tail)
            assert line.endswith("\n")
            assert "\n" not in line[:-1], "a message must be exactly one line"
            body = json.loads(line)
            assert body["jsonrpc"] == "2.0"
            assert body["method"] == "initialize"
        finally:
            await proc.close()


class TestEnvJailIsTheSpawnDefault:
    """``spawn(env=None)`` used to inherit the operator's environment.

    An external coding agent got the operator's real ``HOME`` and with
    it ``~/.claude``, ``~/.codex``, ``~/.ssh`` and every exported API
    key. ``security/env_jail.py`` existed with no production caller.
    These pin that the default changed, that a caller can still opt out
    explicitly, and that the throwaway HOME is cleaned up.

    The probe child dumps its own environment to a file, so every
    assertion is about what actually crossed the process boundary. It
    writes to a file rather than stdout because ``spawn`` has already
    handed stdout to the JSON-RPC peer's read loop.
    """

    PROBE = (
        "import json, os, sys\n"
        "open(sys.argv[1], 'w').write(json.dumps(dict(os.environ)))\n"
        "import time; time.sleep(5)\n"
    )

    async def _child_env(self, tmp_path, **spawn_kwargs):
        script = tmp_path / "probe_env.py"
        script.write_text(self.PROBE)
        dump = tmp_path / "child_env.json"
        proc = await AcpAgentProcess.spawn(
            [sys.executable, "-u", str(script), str(dump)],
            cwd=str(tmp_path),
            **spawn_kwargs,
        )
        for _ in range(200):
            if dump.exists() and dump.stat().st_size:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - only on a wedged child
            await proc.close()
            raise AssertionError(f"probe never wrote its env: {proc.stderr_tail}")
        return json.loads(dump.read_text()), proc

    async def test_the_child_does_not_get_the_operator_home(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", "/Users/pretend-operator")
        child_env, proc = await self._child_env(tmp_path)
        try:
            assert child_env["HOME"] != "/Users/pretend-operator"
            assert not Path(child_env["HOME"], ".claude").exists()
        finally:
            await proc.close()

    async def test_the_child_does_not_get_operator_secrets(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_travel")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_should_not_travel")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/should-not-travel.sock")
        child_env, proc = await self._child_env(tmp_path)
        try:
            for name in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK"):
                assert name not in child_env, f"{name} reached the agent"
        finally:
            await proc.close()

    async def test_the_child_keeps_the_model_key_it_needs_to_work(
        self, tmp_path, monkeypatch
    ):
        """The jail is a default, not a way to break every agent."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-for-the-agent")
        child_env, proc = await self._child_env(tmp_path)
        try:
            assert child_env["ANTHROPIC_API_KEY"] == "sk-ant-for-the-agent"
            assert child_env["PATH"] == os.environ["PATH"]
        finally:
            await proc.close()

    async def test_an_explicit_env_still_replaces_verbatim(
        self, tmp_path, monkeypatch
    ):
        """The opt-out ``bridges/sessions.py`` uses when env_jail is off."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_explicitly_passed")
        child_env, proc = await self._child_env(tmp_path, env=dict(os.environ))
        try:
            assert child_env["GITHUB_TOKEN"] == "ghp_explicitly_passed"
            assert child_env["HOME"] == os.environ["HOME"]
        finally:
            await proc.close()

    async def test_the_jail_directory_is_removed_on_close(self, tmp_path):
        child_env, proc = await self._child_env(tmp_path)
        jail_home = child_env["HOME"]
        assert Path(jail_home).is_dir()
        await proc.close()
        assert not Path(jail_home).exists()

    async def test_an_explicit_env_leaves_no_jail_to_clean_up(self, tmp_path):
        _child_env, proc = await self._child_env(tmp_path, env=dict(os.environ))
        try:
            assert proc._jail is None
        finally:
            await proc.close()

    async def test_a_missing_binary_does_not_leak_a_jail_directory(self, tmp_path):
        """The FileNotFoundError path runs before anything owns the jail."""
        before = set(Path(tempfile.gettempdir()).glob("feral-env-jail-*"))
        with pytest.raises(AcpProtocolError, match="not found"):
            await AcpAgentProcess.spawn(
                ["/nonexistent/definitely-not-here"], cwd=str(tmp_path)
            )
        after = set(Path(tempfile.gettempdir()).glob("feral-env-jail-*"))
        assert after == before
