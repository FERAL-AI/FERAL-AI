"""`feral app install` has to ask, and has to ask with the same facts.

``POST /api/apps/install`` requires an ``install_token`` minted by
``POST /api/apps/preview`` (see ``tests/test_app_install_consent.py`` for
why: an app's ``skill_dependencies`` install skills, and a skill executes
Python in the brain's own process). The CLI posted the old ungated shape
and got a 403, so ``feral app install`` did not work at all.

The fix is not "send the token". A terminal is a consent surface like the
install sheet is, so these tests pin what the terminal has to say:

1. The two steps happen in order and the app lands only after a yes.
2. The three dependency buckets are three visible decisions, not a count.
3. Declining installs nothing.
4. A dependency FERAL cannot verify prints the brain's own reason, the
   actions it breaks, and a next step, and the user may still proceed.
5. Non-interactive use is decided rather than accidental: no TTY and no
   ``--yes`` refuses without touching stdin, and ``--yes`` proceeds while
   still printing the disclosure it is answering for.

The HTTP calls run against the real routes through a TestClient, so a
change to the gate breaks these tests rather than passing a stub.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from agents.app_registry import AppRegistry, HybridGenerator
from cli import app_commands
from tests.test_app_install_consent import (  # reuse, do not duplicate
    FakeMarketplace,
    _skill,
    _write_app,
)


# No ``no_auto_feral_home`` here, unlike tests/test_app_install_consent.py:
# nothing in this module needs the real ``~/.feral``, and the autouse
# isolation fixture keeps an install run by a test out of it.


# ─────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────


class _TestClientHttpx:
    """The httpx surface ``cli.app_commands`` uses, wired to a TestClient.

    The CLI keeps talking to a real ASGI app over the real route
    handlers; only the socket is short-circuited. ``HTTPError`` is the
    genuine class so the CLI's own ``except`` clause is exercised.
    """

    HTTPError = httpx.HTTPError

    def __init__(self, client: TestClient):
        self.client = client
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json=None, timeout=None, headers=None):  # noqa: A002
        path = urlparse(url).path
        self.calls.append((path, dict(json or {})))
        return self.client.post(path, json=json, headers=headers or {})

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]

    def body_for(self, path: str) -> dict:
        for p, body in self.calls:
            if p == path:
                return body
        raise AssertionError(f"{path} was never called; called {self.paths()}")


class _ExplodingStdin:
    """A stdin that fails the test if the CLI reads it.

    "Does not hang" is the property under test, and a prompt that reads a
    closed pipe returns instantly rather than hanging, so waiting on a
    clock would prove nothing. Reading stdin at all is the defect.
    """

    def isatty(self) -> bool:
        return False

    def readline(self, *a, **k):
        raise AssertionError("the CLI read stdin with no TTY and no --yes")

    def read(self, *a, **k):
        raise AssertionError("the CLI read stdin with no TTY and no --yes")


class _Marketplace(FakeMarketplace):
    """``FakeMarketplace`` with production's permission copy.

    ``skills/marketplace.py`` puts ``describe_permissions(...)`` in every
    preview it answers, which is how one vocabulary reaches every client.
    The base fake labels a permission with its own id, which would let a
    CLI that invented its own wording pass. This does not.
    """

    async def preview_from_registry(self, kind, item_id):
        from models.skill_manifest import describe_permissions

        out = await super().preview_from_registry(kind, item_id)
        if out.get("success"):
            out["permission_details"] = describe_permissions(out.get("permissions") or [])
        return out


def _flat(text: str) -> str:
    """Collapse whitespace, so an assertion is about copy, not line breaks."""
    return " ".join(text.split())


@pytest.fixture()
def brain(tmp_path, monkeypatch):
    """A real AppRegistry behind the real routes, with the CLI pointed at it."""
    from api.routes import apps as apps_route

    getattr(apps_route, "_pending_app_previews", {}).clear()

    registry = AppRegistry(db_path=str(tmp_path / "apps.db"), apps_dir=tmp_path / "apps")
    registry.set_hybrid_generator(HybridGenerator(cache_dir=tmp_path / "cache"))

    skill_registry = MagicMock()
    skill_registry.skills = {}
    skill_registry.reload_skill = MagicMock(return_value=True)

    state = MagicMock()
    state.app_registry = registry
    state.skill_registry = skill_registry
    state.marketplace = None
    state.vault = None
    state.supervisor = None
    state.sessions = {}

    with patch("api.state.state", state), patch("api.routes.apps.state", state):
        from api.server import app

        client = TestClient(app, raise_server_exceptions=False)
        transport = _TestClientHttpx(client)
        monkeypatch.setattr(app_commands, "httpx", transport)
        monkeypatch.setattr(app_commands, "_brain_base_url", lambda *a, **k: "http://testserver")
        yield SimpleNamespace(
            registry=registry,
            state=state,
            http=transport,
            tmp=tmp_path,
        )

    getattr(apps_route, "_pending_app_previews", {}).clear()


@pytest.fixture()
def answers(monkeypatch):
    """Drive the confirm prompt, and record what it was asked."""
    asked: list[str] = []
    replies: list[bool] = []

    def _confirm(message, *, default=False):
        asked.append(message)
        return replies.pop(0) if replies else default

    monkeypatch.setattr("cli.ui_kit.is_interactive", lambda: True)
    monkeypatch.setattr("cli.ui_kit.confirm", _confirm)
    return SimpleNamespace(asked=asked, replies=replies)


def _install(path, **kwargs) -> int:
    """Run the command, returning the exit code (0 when it returns cleanly)."""
    try:
        app_commands.cmd_app_install(str(path), **kwargs)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


# ─────────────────────────────────────────────
# 1. Preview, then ask, then install with the token
# ─────────────────────────────────────────────


def test_install_previews_asks_then_spends_the_token(brain, answers, capsys):
    src = _write_app(brain.tmp)
    answers.replies.append(True)

    code = _install(src, unsigned=True)
    out = capsys.readouterr().out

    assert code == 0, out
    assert brain.http.paths() == ["/api/apps/preview", "/api/apps/install"]
    assert answers.asked, "the user was never asked"
    assert brain.registry.get("notes-app") is not None, out
    assert "notes-app" in out

    # The install spends the token and names no source: the source is
    # inside the token, bound to the bytes the preview described.
    body = brain.http.body_for("/api/apps/install")
    assert body.get("install_token")
    assert body["install_token"] == brain.http.calls[0][1].get("install_token", body["install_token"])
    assert not body.get("path")


def test_the_app_own_reach_is_printed_from_the_brains_copy(brain, answers, capsys):
    src = _write_app(brain.tmp)
    answers.replies.append(True)

    _install(src, unsigned=True)
    out = capsys.readouterr().out

    # models/skill_manifest.py + agents/app_registry.py own this wording.
    flat = _flat(out)
    assert "No network access of its own" in flat
    assert "cannot contact any server" in flat


# ─────────────────────────────────────────────
# 2. Three buckets are three decisions
# ─────────────────────────────────────────────


def test_new_skills_and_already_installed_skills_are_told_apart(brain, answers, capsys):
    brain.state.marketplace = _Marketplace(
        {"map_tiles": {"permissions": ["network"], "name": "Map Tiles"}}
    )
    brain.state.skill_registry.skills = {"trail_notes": _skill("trail_notes", ["filesystem"])}
    src = _write_app(brain.tmp, deps=["trail_notes", "map_tiles"])
    answers.replies.append(True)

    code = _install(src, unsigned=True)
    out = capsys.readouterr().out
    assert code == 0, out

    flat = _flat(out)
    assert "map_tiles" in flat and "trail_notes" in flat, out

    # Not a count: the two buckets are separately headed, and the new
    # code is named as new code rather than as a number.
    assert "Skills it will install (1)" in flat
    assert "Skills you already have (1)" in flat
    assert "runs its own Python inside FERAL" in flat
    assert "nothing new is installed for these" in flat
    # The new skill's reach is the brain's sentence for it, not an id.
    assert "Internet access" in flat
    assert "Contact servers on the internet" in flat
    assert brain.registry.get("notes-app") is not None


def test_a_skill_that_declares_no_permissions_is_still_code(brain, answers, capsys):
    brain.state.marketplace = _Marketplace({"quiet_skill": {"permissions": []}})
    src = _write_app(brain.tmp, deps=["quiet_skill"])
    answers.replies.append(True)

    _install(src, unsigned=True)
    out = capsys.readouterr().out

    flat = _flat(out)
    assert "quiet_skill" in flat
    assert "declares no permissions" in flat
    assert "still runs its own code inside FERAL" in flat


# ─────────────────────────────────────────────
# 3. No means nothing happened
# ─────────────────────────────────────────────


def test_declining_installs_nothing(brain, answers, capsys):
    brain.state.marketplace = _Marketplace({"map_tiles": {"permissions": ["network"]}})
    market = brain.state.marketplace
    src = _write_app(brain.tmp, deps=["map_tiles"])
    answers.replies.append(False)

    code = _install(src, unsigned=True)
    out = capsys.readouterr().out

    assert code == 1, out
    assert brain.http.paths() == ["/api/apps/preview"], "install must not be called after a no"
    assert brain.registry.get("notes-app") is None
    assert market.installed == [], "a declined app must not install its skills either"
    assert "nothing was installed" in _flat(out).lower()


# ─────────────────────────────────────────────
# 4. An unverifiable dependency is a signpost, not a dead end
# ─────────────────────────────────────────────


def test_unverifiable_dependency_prints_reason_impact_and_remediation(brain, answers, capsys):
    brain.state.marketplace = _Marketplace({})  # nothing published
    src = _write_app(brain.tmp, deps=["ghost_skill"])
    answers.replies.append(True)

    code = _install(src, unsigned=True)
    out = capsys.readouterr().out
    assert code == 0, out

    flat = _flat(out)
    # (1) the brain's own reason, not a generic string
    assert "not published in the FERAL registry" in flat
    # (2) what breaks without it, named
    assert "Sync with ghost_skill" in flat
    # (3) how to get it
    assert "feral publisher login" in flat
    assert "feral publish --skill" in flat
    # (4) the user still got to decide, and the app is installed degraded
    assert brain.registry.get("notes-app") is not None
    # (5) and the outcome says what it went without
    assert "Installed without ghost_skill" in flat
    assert "Apps page keeps showing what is missing" in flat
    listing = _manifest_listing(brain)
    assert [m["skill_id"] for m in listing["missing_skill_dependencies"]] == ["ghost_skill"]


def _manifest_listing(brain) -> dict:
    from api.routes.apps import _manifest_summary

    return _manifest_summary(brain.registry.get("notes-app"))


def test_the_prompt_says_the_install_is_degraded(brain, answers, capsys):
    brain.state.marketplace = _Marketplace({})
    src = _write_app(brain.tmp, deps=["ghost_skill"])
    answers.replies.append(True)

    _install(src, unsigned=True)
    capsys.readouterr()

    assert answers.asked, "no prompt was shown"
    assert "without" in answers.asked[-1].lower(), answers.asked


def test_a_signature_failure_offers_no_command(brain, answers, capsys):
    """Every install path runs the same verifier, so there is nothing to run."""
    brain.state.marketplace = _Marketplace({"bad_skill": {"signature_fails": True}})
    src = _write_app(brain.tmp, deps=["bad_skill"])
    answers.replies.append(True)

    _install(src, unsigned=True)
    out = capsys.readouterr().out

    flat = _flat(out)
    assert "does not match the publisher's signature" in flat
    assert "Retrying will not help" in flat
    assert "feral install bad_skill" not in flat


# ─────────────────────────────────────────────
# 5. Non-interactive use is designed, not accidental
# ─────────────────────────────────────────────


def test_no_tty_and_no_yes_refuses_without_reading_stdin(brain, monkeypatch, capsys):
    monkeypatch.setattr("cli.ui_kit.is_interactive", lambda: False)
    monkeypatch.setattr(
        "cli.ui_kit.confirm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted with no TTY")),
    )
    monkeypatch.setattr(sys, "stdin", _ExplodingStdin())
    brain.state.marketplace = _Marketplace({"map_tiles": {"permissions": ["network"]}})
    src = _write_app(brain.tmp, deps=["map_tiles"])

    code = _install(src, unsigned=True)
    out = capsys.readouterr().out

    flat = _flat(out)
    assert code == 2, out
    assert brain.http.paths() == ["/api/apps/preview"]
    assert brain.registry.get("notes-app") is None
    # It names the command that would have worked, with the flags it was
    # given, rather than saying "pass --yes" and leaving the rest out.
    assert f"feral app install {src} --unsigned --yes" in flat
    # The disclosure is still printed, so a scripted run leaves a record
    # of what it was refusing to consent to on someone's behalf.
    assert "map_tiles" in flat and "Internet access" in flat


def test_yes_installs_without_a_prompt_and_still_discloses(brain, monkeypatch, capsys):
    monkeypatch.setattr("cli.ui_kit.is_interactive", lambda: False)
    monkeypatch.setattr(
        "cli.ui_kit.confirm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted with --yes")),
    )
    monkeypatch.setattr(sys, "stdin", _ExplodingStdin())
    brain.state.marketplace = _Marketplace({"map_tiles": {"permissions": ["network"]}})
    src = _write_app(brain.tmp, deps=["map_tiles"])

    code = _install(src, unsigned=True, assume_yes=True)
    out = capsys.readouterr().out

    flat = _flat(out)
    assert code == 0, out
    assert brain.http.paths() == ["/api/apps/preview", "/api/apps/install"]
    assert brain.registry.get("notes-app") is not None
    assert "map_tiles" in flat and "Internet access" in flat


# ─────────────────────────────────────────────
# 6. A refusal names a next step that exists
# ─────────────────────────────────────────────


def test_an_unsigned_bundle_is_refused_with_a_flag_that_exists(brain, answers, capsys):
    src = _write_app(brain.tmp)

    code = _install(src)  # no --unsigned
    out = capsys.readouterr().out

    flat = _flat(out)
    assert code == 1, out
    assert brain.http.paths() == ["/api/apps/preview"]
    assert brain.registry.get("notes-app") is None
    assert "manifest is unsigned" in flat
    assert f"feral app install {src} --unsigned" in flat
    assert f"feral app sign {src} --key-id" in flat
    # The flags it names are flags the parser accepts.
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="subcommand")
    app_commands.register_app_subparser(sub)
    args = parser.parse_args(["app", "install", str(src), "--unsigned", "--yes"])
    assert args.app_unsigned is True
    assert args.app_assume_yes is True


def test_the_dispatcher_passes_the_new_flags_through(brain, monkeypatch):
    import argparse

    seen: dict = {}

    def _fake(path, host=None, port=None, **kwargs):
        seen.update({"path": path, "host": host, "port": port, **kwargs})

    monkeypatch.setattr(app_commands, "cmd_app_install", _fake)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="subcommand")
    app_commands.register_app_subparser(sub)
    args = parser.parse_args(["app", "install", "./x", "--yes", "--unsigned"])
    app_commands.dispatch_app_subcommand(args)

    assert seen["path"] == "./x"
    assert seen["assume_yes"] is True
    assert seen["unsigned"] is True


def test_a_tampered_bundle_is_offered_no_way_around_the_check(brain, answers, capsys):
    """--unsigned is for a bundle with no signature, not a broken one."""
    src = _write_app(brain.tmp, app_id="tampered-app")
    _sign_then_tamper(src)

    code = _install(src)
    out = capsys.readouterr().out
    flat = _flat(out)

    assert code == 1, out
    assert brain.registry.get("tampered-app") is None
    assert "does not match" in flat
    assert "Retrying will not help" in flat
    # Every install path runs this verifier, so there is nothing to run,
    # and --unsigned would be a way around a check that just fired.
    assert "--unsigned" not in flat
    assert "feral app install" not in flat


def test_a_dependency_that_fails_at_install_time_is_reported_precisely(
    brain, answers, capsys
):
    """The brain rolls the app back, but not the skills that did install."""
    brain.state.marketplace = _Marketplace({
        "map_tiles": {"permissions": ["network"]},
        "flaky_skill": {"permissions": ["filesystem"], "install_fails": True},
    })
    src = _write_app(brain.tmp, deps=["map_tiles", "flaky_skill"])
    answers.replies.append(True)

    code = _install(src, unsigned=True)
    out = capsys.readouterr().out
    flat = _flat(out)

    assert code == 1, out
    assert brain.registry.get("notes-app") is None
    assert "flaky_skill: install failed: disk full" in flat
    assert "The app was rolled back and is not installed." in flat
    assert "Any skill that did install before the failure is still installed" in flat
    assert "Nothing was installed." not in flat


def _sign_then_tamper(src: Path) -> None:
    """Write a manifest.signed.json whose signature no longer matches."""
    import json as _json

    from nacl.signing import SigningKey

    from genui.manifest_signing import sign as sign_manifest

    manifest = _json.loads((src / "manifest.json").read_text())
    envelope = sign_manifest(manifest, bytes(SigningKey.generate()), key_id="demo")
    payload = _json.loads(envelope.model_dump_json())
    payload["manifest"]["description"] = "swapped after signing"
    (src / "manifest.signed.json").write_text(_json.dumps(payload))


# ─────────────────────────────────────────────
# 7. The real process, with a real pipe on stdin
# ─────────────────────────────────────────────


_PREVIEW_BODY = {
    "success": True,
    "app": {
        "app_id": "piped-app",
        "version": "1.0.0",
        "author": "acme",
        "description": "",
        "brand": {"name": "Piped"},
        "entry_surface_id": "home",
        "surfaces": [{"surface_id": "home", "kind": "authored"}],
    },
    "source": {"origin": "path", "value": "."},
    "signature": {"verified": False, "reason": "", "key_id": "", "sha256": "b" * 64},
    "permissions": [],
    "permission_details": [
        {
            "id": "network:none",
            "label": "No network access of its own",
            "description": "Its surfaces cannot contact any server.",
            "known": True,
        }
    ],
    "skill_dependencies": {
        "declared": [],
        "already_installed": [],
        "to_install": [],
        "unavailable": [],
    },
    "degraded": False,
    "install_token": "stub-token",
    "expires_in": 300,
}

_INSTALL_BODY = {
    "success": True,
    "app": {"app_id": "piped-app", "version": "1.0.0"},
    "skill_dependencies": {
        "declared": [],
        "already_present": [],
        "installed": [],
        "failed": [],
        "unavailable": [],
    },
    "degraded": False,
}


class _StubBrain(BaseHTTPRequestHandler):
    """Canned preview/install, so the subprocess test is about the process."""

    seen: list[str] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        type(self).seen.append(self.path)
        body = _PREVIEW_BODY if self.path.endswith("/preview") else _INSTALL_BODY
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):  # silence the default stderr access log
        return


@pytest.fixture()
def stub_brain():
    _StubBrain.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubBrain)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_cli(stub_url: str, args: list[str], *, stdin) -> subprocess.CompletedProcess:
    core = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["FERAL_BRAIN_URL"] = stub_url
    env["PYTHONPATH"] = str(core) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "cli.main", "app", "install", *args],
        cwd=str(core),
        env=env,
        stdin=stdin,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_piped_stdin_does_not_hang(stub_brain, tmp_path):
    """A pipe on stdin is the scripted case, and it must answer, not wait."""
    src = _write_app(tmp_path, app_id="piped-app")

    read_fd, write_fd = os.pipe()
    try:
        # An open write end nobody writes to: a read here blocks forever.
        proc = _run_cli(stub_brain, [str(src), "--unsigned"], stdin=read_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--yes" in proc.stdout


def test_yes_over_a_pipe_installs(stub_brain, tmp_path):
    src = _write_app(tmp_path, app_id="piped-app")

    proc = _run_cli(
        stub_brain, [str(src), "--unsigned", "--yes"], stdin=subprocess.DEVNULL
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "piped-app" in proc.stdout
    assert _StubBrain.seen == ["/api/apps/preview", "/api/apps/install"], _StubBrain.seen
