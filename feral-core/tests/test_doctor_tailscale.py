"""Tailscale coverage in ``feral doctor``.

WHY this file exists
--------------------
An operator with no Tailscale installed ran ``feral access remote-up``,
waited 20 seconds, and was told::

    tailscale funnel --bg 9090 timed out after 20.0s

Nothing about that message says "Tailscale is not installed". Worse,
``feral doctor`` had *zero* Tailscale probes at the time, so the tool
whose job is to answer "is my install healthy?" reported all-green
while the operator's chosen pairing transport did not exist on the
machine.

These tests pin the layered probes that replaced that silence:

  binary present -> daemon running -> logged in -> funnel serving the
  brain port -> config agrees with reality

and two properties that matter as much as the probes themselves:

  * **Severity follows intent.** Missing Tailscale is ``_info`` for the
    WiFi-pairing majority and ``_fail`` only when the operator selected
    remote mode and is therefore depending on it.
  * **A diagnostic for a hang cannot hang.** Every probe runs on a
    <=3s budget, and a probe that exhausts it reports "did not answer"
    rather than being collapsed into "not installed" (a wedged daemon
    and a missing binary need completely different fixes).

Nothing here touches the real ``tailscale`` binary: the subprocess call
inside ``integrations.tailscale`` is replaced per-test, and
``shutil.which`` is stubbed for the name "tailscale" only (every other
lookup falls through to the real implementation so the rest of doctor
keeps behaving normally).
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import types

import pytest


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# What a healthy `tailscale status --json` looks like, trimmed to the
# fields doctor actually reads.
STATUS_RUNNING = json.dumps({
    "BackendState": "Running",
    "Self": {
        "DNSName": "leroy-mac.tail9f2c.ts.net.",
        "TailscaleIPs": ["100.101.102.103"],
    },
    "CurrentTailnet": {"Name": "tail9f2c.ts.net"},
})

STATUS_NEEDS_LOGIN = json.dumps({
    "BackendState": "NeedsLogin",
    "Self": {},
})

# macOS phrasing when the Tailscale app has never been launched. Note
# there is no socket path in it, which is why the integration's own
# ``_classify_stderr`` cannot recognise it and doctor carries its own
# pattern for this case.
DAEMON_DOWN_STDERR = (
    "failed to connect to local Tailscale service; is Tailscale running?"
)

# Linux phrasing, which *does* carry the socket path.
DAEMON_DOWN_STDERR_SOCKET = (
    "dial unix /var/run/tailscaled.socket: connect: no such file or directory"
)


def funnel_json(*ports: int) -> str:
    """A ``tailscale funnel status --json`` payload forwarding ``ports``."""
    if not ports:
        # Tailscale returns a bare `{}` when there is no serve config.
        return "{}"
    # One handler per port under a single host, which is the shape the
    # real CLI emits and exercises the multi-handler walk.
    handlers = {f"/{p}": {"Proxy": f"http://127.0.0.1:{p}"} for p in ports}
    web = {"leroy-mac.tail9f2c.ts.net:443": {"Handlers": handlers}}
    return json.dumps({"TCP": {"443": {"HTTPS": True}}, "Web": web})


# ── Fakes ─────────────────────────────────────────────────────────────


class FakeTailscaleCLI:
    """Stands in for the ``tailscale`` binary.

    ``responses`` maps a subcommand ("status" / "funnel") to either a
    ``(returncode, stdout, stderr)`` triple or the string ``"timeout"``,
    which raises the same ``subprocess.TimeoutExpired`` the real call
    would raise when the daemon stops answering.
    """

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def run(self, cmd, **kwargs):
        # `_run` prepends "tailscale" and may inject `--socket <path>`.
        args = [a for a in cmd[1:] if a != "--socket" and not a.endswith(".sock")]
        self.calls.append(args)
        self.timeouts.append(kwargs.get("timeout"))
        spec = self.responses.get(args[0])
        if spec is None:
            raise AssertionError(f"unexpected tailscale invocation: {args}")
        if spec == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        rc, out, err = spec
        return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout=out, stderr=err)


@pytest.fixture
def fake_tailscale(monkeypatch):
    """Install a fake ``tailscale`` CLI; returns a configure() callable."""

    def configure(*, installed: bool = True, responses: dict | None = None) -> FakeTailscaleCLI:
        from integrations import tailscale as ts_mod

        cli = FakeTailscaleCLI(responses or {})

        real_which = shutil.which

        def fake_which(cmd, *a, **kw):
            if cmd == "tailscale":
                return "/opt/homebrew/bin/tailscale" if installed else None
            return real_which(cmd, *a, **kw)

        monkeypatch.setattr(shutil, "which", fake_which)
        # Replace the subprocess *module reference* inside the
        # integration only, so no other doctor probe (Node.js, Chrome,
        # TCC) loses its real subprocess.
        monkeypatch.setattr(
            ts_mod,
            "subprocess",
            types.SimpleNamespace(
                run=cli.run,
                TimeoutExpired=subprocess.TimeoutExpired,
                CompletedProcess=subprocess.CompletedProcess,
                # _run passes stdin=subprocess.DEVNULL so a prompting
                # CLI cannot block on input that will never arrive. The
                # double has to model that or every probe dies on an
                # AttributeError instead of exercising the probe.
                DEVNULL=subprocess.DEVNULL,
            ),
        )
        # The userspace-socket probe must not depend on whatever is in
        # /tmp on the machine running the suite.
        monkeypatch.setattr("os.path.exists", lambda p: False if str(p).endswith(".sock") else _real_exists(p))
        return cli

    import os as _os
    _real_exists = _os.path.exists
    return configure


@pytest.fixture
def doctor_home(monkeypatch, tmp_path):
    """A FERAL_HOME whose only interesting content is settings.json.

    Everything network-facing that doctor touches outside the Tailscale
    section is stubbed so these tests measure the Tailscale rows and
    nothing else.
    """
    home = tmp_path / "tailscale-doctor-home"
    home.mkdir()
    monkeypatch.setenv("FERAL_HOME", str(home))
    monkeypatch.setenv("FERAL_PORT", "9090")
    monkeypatch.delenv("FERAL_BIND_HOST", raising=False)
    monkeypatch.delenv("FERAL_PUBLIC_BASE_URL", raising=False)
    for key in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "GROQ_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    # Ollama "running" so the LLM section is green without a vault.
    def _fake_urlopen(url, timeout=2):
        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    import time as _t
    from security import probe as _probe_mod

    async def _fake_probe(pid, **_kw):
        if pid == "ollama":
            return _probe_mod.ProbeResult(
                provider="ollama", ok=True, status_code=200, reason="ok",
                detail="Ollama running locally", probed_at=_t.time(), latency_ms=5.0,
            )
        return _probe_mod.ProbeResult(
            provider=pid, ok=False, status_code=None, reason="no_key",
            detail="not configured", probed_at=_t.time(), latency_ms=0.0,
        )

    monkeypatch.setattr(_probe_mod, "probe", _fake_probe)
    _probe_mod.clear_probe_cache()

    # The unrelated "Phone pairing" probe asks the devices route to
    # resolve a pair origin, which cannot succeed on a test box with no
    # brain listening. Pin it green so the exit code and the suggested
    # fixes in these tests are attributable to the Tailscale rows alone.
    from api.routes import devices as _devices_route

    monkeypatch.setattr(
        _devices_route, "_resolve_pair_origin", lambda: "https://pinned.example"
    )

    (home / "USER.md").write_text("Leroy, testing remote pairing.\n")
    return home


def write_settings(home, *, mode: str, tailnet_url: str | None = None) -> None:
    """Persist an access mode the way ``feral access`` would.

    The bind host is written to whatever the mode implies so the
    unrelated "Access mode coherence" probe stays green and cannot
    contaminate assertions about the Tailscale rows.
    """
    bind = "0.0.0.0" if mode == "local" else "127.0.0.1"
    access: dict = {"pairing_mode": mode}
    if tailnet_url is not None:
        access["tailscale"] = {"funnel": bool(tailnet_url), "tailnet_url": tailnet_url}
        access["remote_provider"] = "tailscale"
    (home / "settings.json").write_text(json.dumps({
        "access": access,
        "network": {"bind_host": bind, "tls": False},
    }))


@pytest.fixture
def run_doctor(monkeypatch):
    """Run ``cmd_doctor`` and return its plain-text output.

    ``cmd_doctor`` exits 1 when anything red fired, so SystemExit is
    swallowed here and surfaced as ``.exit_code`` on the result.
    """

    def _run() -> str:
        from rich.console import Console as _RichConsole

        buf = io.StringIO()
        console = _RichConsole(file=buf, force_terminal=False, color_system=None, width=400)
        monkeypatch.setattr("rich.console.Console", lambda *a, **kw: console)

        from cli.main import cmd_doctor

        exit_code = 0
        try:
            cmd_doctor()
        except SystemExit as exc:
            exit_code = int(exc.code or 0)

        text = ANSI_RE.sub("", buf.getvalue())
        out = _Output(text)
        out.exit_code = exit_code
        return out

    return _run


class _Output(str):
    """Doctor output plus the helpers every test here needs."""

    exit_code = 0

    def row(self, label: str) -> str:
        """The rendered line for a probe label (asserts it exists)."""
        for line in self.splitlines():
            body = line.strip()
            if len(body) > 2 and body[1:].strip().startswith(label):
                return body
        raise AssertionError(f"no doctor row for {label!r} in:\n{self}")

    def has_row(self, label: str) -> bool:
        try:
            self.row(label)
            return True
        except AssertionError:
            return False

    def severity(self, label: str) -> str:
        return self.row(label)[0]

    @property
    def fixes(self) -> str:
        """Everything under the 'Suggested fixes:' header, as one blob."""
        if "Suggested fixes:" not in self:
            return ""
        return self.split("Suggested fixes:", 1)[1]


PASS, INFO, WARN, FAIL = "✔", "ℹ", "⚠", "✘"


# ── 1. Binary present ─────────────────────────────────────────────────


class TestBinaryPresence:

    def test_not_installed_is_info_when_not_pairing_remotely(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """Not having Tailscale is the normal state for WiFi pairing.

        It must be visible, but it is not a problem and must not add a
        remediation line for something the operator never asked for.
        """
        write_settings(doctor_home, mode="localhost")
        fake_tailscale(installed=False)

        out = run_doctor()

        row = out.row("Tailscale binary")
        assert row.startswith(INFO), row
        assert "not installed" in row
        # Still actionable if they *want* it: the install command is named.
        assert ("brew install --cask tailscale" in row) or ("tailscale.com/install.sh" in row)
        assert out.exit_code == 0
        # No remediation is queued for an opt-in transport nobody selected.
        assert "feral access remote-up" not in out.fixes

    def test_not_installed_in_remote_mode_fails_with_install_command(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """Leroy's case, caught before he spends 20 seconds on it.

        The row must name the real install command AND the follow-up
        (`feral access remote-up`), because the install alone leaves him
        exactly where he was.
        """
        write_settings(doctor_home, mode="remote", tailnet_url="")
        cli = fake_tailscale(installed=False)

        out = run_doctor()

        row = out.row("Tailscale binary")
        assert row.startswith(FAIL), row
        assert "not installed" in row
        assert "remote" in row

        expected_install = (
            "brew install --cask tailscale" if sys.platform == "darwin"
            else "curl -fsSL https://tailscale.com/install.sh | sh"
        )
        assert out.exit_code == 1
        assert expected_install in out.fixes, out.fixes
        assert "feral access remote-up" in out.fixes

        # Nothing was shelled out to: a missing binary is answered from
        # PATH, not by waiting on a CLI that cannot run.
        assert cli.calls == []

    def test_installed_binary_reports_its_path(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        write_settings(doctor_home, mode="localhost")
        fake_tailscale(responses={"status": (0, STATUS_RUNNING, ""), "funnel": (0, "{}", "")})

        out = run_doctor()

        row = out.row("Tailscale binary")
        assert row.startswith(PASS), row
        assert "/opt/homebrew/bin/tailscale" in row


# ── 2. Daemon running ─────────────────────────────────────────────────


class TestDaemon:

    @pytest.mark.parametrize("stderr", [DAEMON_DOWN_STDERR, DAEMON_DOWN_STDERR_SOCKET])
    def test_daemon_down_in_remote_mode_fails_with_start_instruction(
        self, doctor_home, fake_tailscale, run_doctor, stderr
    ):
        """Both the macOS and Linux phrasings must classify as 'down'.

        The macOS one carries no socket path, which is why doctor cannot
        rely on the integration's classifier alone.
        """
        write_settings(doctor_home, mode="remote", tailnet_url="https://leroy-mac.tail9f2c.ts.net")
        cli = fake_tailscale(responses={"status": (1, "", stderr)})

        out = run_doctor()

        row = out.row("Tailscale daemon")
        assert row.startswith(FAIL), row
        assert "not running" in row
        assert "Start Tailscale" in out.fixes
        assert "tailscale up" in out.fixes
        assert out.exit_code == 1

        # A dead daemon cannot answer a funnel query either; doctor must
        # not spend a second probe budget proving that.
        assert [c[0] for c in cli.calls] == ["status"]

    def test_daemon_down_outside_remote_mode_is_info(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """Tailscale installed but not launched is a normal Mac state."""
        write_settings(doctor_home, mode="localhost")
        fake_tailscale(responses={"status": (1, "", DAEMON_DOWN_STDERR)})

        out = run_doctor()

        row = out.row("Tailscale daemon")
        assert row.startswith(INFO), row
        assert "not running" in row
        assert out.exit_code == 0
        assert WARN not in out.splitlines()[0]
        assert "Suggested fixes:" not in out


# ── 3. Logged in ──────────────────────────────────────────────────────


class TestAccount:

    def test_logged_out_json_in_remote_mode_fails(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """`status --json` answers with BackendState=NeedsLogin.

        The daemon is fine, so the daemon row stays green and only the
        account row goes red. Conflating the two would send the operator
        restarting a daemon that is working.
        """
        write_settings(doctor_home, mode="remote", tailnet_url="https://leroy-mac.tail9f2c.ts.net")
        fake_tailscale(responses={"status": (0, STATUS_NEEDS_LOGIN, "")})

        out = run_doctor()

        assert out.row("Tailscale daemon").startswith(PASS)
        row = out.row("Tailscale account")
        assert row.startswith(FAIL), row
        assert "logged out" in row.lower()
        assert "tailscale up" in out.fixes
        assert out.exit_code == 1

    def test_logged_out_stderr_in_remote_mode_fails(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """Older CLIs exit non-zero with "Logged out." on stderr."""
        write_settings(doctor_home, mode="remote", tailnet_url="https://leroy-mac.tail9f2c.ts.net")
        fake_tailscale(responses={"status": (1, "", "Logged out.")})

        out = run_doctor()

        assert out.row("Tailscale daemon").startswith(PASS)
        assert out.row("Tailscale account").startswith(FAIL)
        assert "tailscale up" in out.fixes

    def test_logged_out_outside_remote_mode_is_info(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        write_settings(doctor_home, mode="localhost")
        fake_tailscale(responses={"status": (0, STATUS_NEEDS_LOGIN, "")})

        out = run_doctor()

        assert out.row("Tailscale account").startswith(INFO)
        assert out.exit_code == 0

    def test_logged_in_reports_tailnet_name(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        write_settings(doctor_home, mode="localhost")
        fake_tailscale(responses={"status": (0, STATUS_RUNNING, ""), "funnel": (0, "{}", "")})

        out = run_doctor()

        row = out.row("Tailscale account")
        assert row.startswith(PASS), row
        assert "leroy-mac.tail9f2c.ts.net" in row
        assert "tail9f2c.ts.net" in row


# ── 4. Funnel active ──────────────────────────────────────────────────


class TestFunnel:

    def test_funnel_serving_brain_port_passes_with_public_url(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """The green case: everything the pair QR needs is live."""
        url = "https://leroy-mac.tail9f2c.ts.net"
        write_settings(doctor_home, mode="remote", tailnet_url=url)
        fake_tailscale(responses={
            "status": (0, STATUS_RUNNING, ""),
            "funnel": (0, funnel_json(9090), ""),
        })

        out = run_doctor()

        row = out.row("Tailscale Funnel")
        assert row.startswith(PASS), row
        assert ":9090" in row
        assert url in row
        assert out.row("Remote access coherence").startswith(PASS)
        assert out.exit_code == 0

    def test_no_funnel_in_remote_mode_fails_with_remote_up(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """Mode says remote, nothing is published. This is the state a
        failed `remote-up` leaves behind, and it used to render green."""
        write_settings(doctor_home, mode="remote", tailnet_url="")
        fake_tailscale(responses={
            "status": (0, STATUS_RUNNING, ""),
            "funnel": (0, "{}", ""),
        })

        out = run_doctor()

        row = out.row("Tailscale Funnel")
        assert row.startswith(FAIL), row
        assert "no funnel is serving the brain port :9090" in row
        assert "feral access remote-up" in out.fixes
        assert out.exit_code == 1

    def test_funnel_pointing_at_another_port_is_not_good_enough(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """A funnel that forwards :3000 does not make the brain reachable."""
        write_settings(doctor_home, mode="remote", tailnet_url="https://leroy-mac.tail9f2c.ts.net")
        fake_tailscale(responses={
            "status": (0, STATUS_RUNNING, ""),
            "funnel": (0, funnel_json(3000), ""),
        })

        out = run_doctor()

        row = out.row("Tailscale Funnel")
        assert row.startswith(FAIL), row
        assert ":3000" in row
        assert ":9090" in row
        assert "feral access remote-up" in out.fixes

    def test_no_funnel_outside_remote_mode_is_info(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        write_settings(doctor_home, mode="localhost")
        fake_tailscale(responses={
            "status": (0, STATUS_RUNNING, ""),
            "funnel": (0, "{}", ""),
        })

        out = run_doctor()

        assert out.row("Tailscale Funnel").startswith(INFO)
        assert out.exit_code == 0
        assert "Suggested fixes:" not in out


# ── 5. Mode coherence ─────────────────────────────────────────────────


class TestModeCoherence:

    def test_remote_mode_without_stored_url_fails(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """A funnel can be live while settings.json has no URL, and the
        pair QR is built from settings.json, not from the funnel."""
        write_settings(doctor_home, mode="remote", tailnet_url="")
        fake_tailscale(responses={
            "status": (0, STATUS_RUNNING, ""),
            "funnel": (0, funnel_json(9090), ""),
        })

        out = run_doctor()

        assert out.row("Tailscale Funnel").startswith(PASS)
        row = out.row("Remote access coherence")
        assert row.startswith(FAIL), row
        assert "access.tailscale.tailnet_url" in row
        assert "feral access remote-up" in out.fixes
        assert out.exit_code == 1

    def test_stored_url_with_dead_funnel_fails(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """The stale-URL case: phones are handed an address that answers
        nothing. The fix must offer both directions (republish or clear)."""
        write_settings(doctor_home, mode="remote", tailnet_url="https://leroy-mac.tail9f2c.ts.net")
        fake_tailscale(responses={
            "status": (0, STATUS_RUNNING, ""),
            "funnel": (0, "{}", ""),
        })

        out = run_doctor()

        row = out.row("Remote access coherence")
        assert row.startswith(FAIL), row
        assert "https://leroy-mac.tail9f2c.ts.net" in row
        assert "dead URL" in row
        assert "feral access remote-up" in out.fixes
        assert "feral access remote-down" in out.fixes

    def test_live_funnel_while_mode_is_local_warns(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """Nobody asked for the brain to be on the public internet."""
        write_settings(doctor_home, mode="localhost")
        fake_tailscale(responses={
            "status": (0, STATUS_RUNNING, ""),
            "funnel": (0, funnel_json(9090), ""),
        })

        out = run_doctor()

        row = out.row("Remote access coherence")
        assert row.startswith(WARN), row
        assert "publishing :9090 to the internet" in row
        assert "feral access remote-down" in out.fixes
        # A warning is not a failure: the brain still works.
        assert out.exit_code == 0

    def test_unknown_funnel_state_does_not_accuse(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """When the daemon is down we do not know the funnel state, so
        the coherence row reports 'unknown' instead of piling a second
        red line onto the daemon failure that already explains it."""
        write_settings(doctor_home, mode="remote", tailnet_url="https://leroy-mac.tail9f2c.ts.net")
        fake_tailscale(responses={"status": (1, "", DAEMON_DOWN_STDERR)})

        out = run_doctor()

        row = out.row("Remote access coherence")
        assert row.startswith(INFO), row
        assert "unknown" in row


# ── 6. Timeouts are their own state ───────────────────────────────────


class TestProbeTimeouts:

    def test_status_timeout_is_not_reported_as_not_installed(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """A wedged daemon and a missing binary need different fixes.

        This is the exact confusion the original incident produced: a
        20s timeout that said nothing about what was actually wrong.
        """
        write_settings(doctor_home, mode="remote", tailnet_url="https://leroy-mac.tail9f2c.ts.net")
        cli = fake_tailscale(responses={"status": "timeout"})

        out = run_doctor()

        assert out.row("Tailscale binary").startswith(PASS)
        row = out.row("Tailscale daemon")
        assert row.startswith(FAIL), row
        assert "did not answer" in row
        assert "wedged" in row
        assert "not installed" not in row
        assert "Restart Tailscale" in out.fixes
        assert out.exit_code == 1

        # And it gave up fast.
        assert all(t is not None and 0 < t <= 3.0 for t in cli.timeouts), cli.timeouts

    def test_status_timeout_outside_remote_mode_warns(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """Still not the operator's fault, but a wedged daemon is a real
        degradation rather than the expected fresh-install state."""
        write_settings(doctor_home, mode="localhost")
        fake_tailscale(responses={"status": "timeout"})

        out = run_doctor()

        row = out.row("Tailscale daemon")
        assert row.startswith(WARN), row
        assert "did not answer" in row
        assert out.exit_code == 0

    def test_funnel_timeout_in_remote_mode_fails(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        write_settings(doctor_home, mode="remote", tailnet_url="https://leroy-mac.tail9f2c.ts.net")
        cli = fake_tailscale(responses={
            "status": (0, STATUS_RUNNING, ""),
            "funnel": "timeout",
        })

        out = run_doctor()

        assert out.row("Tailscale account").startswith(PASS)
        row = out.row("Tailscale Funnel")
        assert row.startswith(FAIL), row
        assert "did not answer" in row
        assert "Restart Tailscale" in out.fixes
        assert out.exit_code == 1
        assert all(t is not None and 0 < t <= 3.0 for t in cli.timeouts), cli.timeouts

    def test_every_probe_uses_a_short_budget(
        self, doctor_home, fake_tailscale, run_doctor
    ):
        """The whole section must cost well under the 20s the broken
        command spent. Two probes at <=3s each is the ceiling."""
        write_settings(doctor_home, mode="remote", tailnet_url="https://leroy-mac.tail9f2c.ts.net")
        cli = fake_tailscale(responses={
            "status": (0, STATUS_RUNNING, ""),
            "funnel": (0, funnel_json(9090), ""),
        })

        run_doctor()

        assert cli.timeouts, "the Tailscale section ran no probes at all"
        assert all(t is not None and 0 < t <= 3.0 for t in cli.timeouts), cli.timeouts
        assert sum(cli.timeouts) < 20.0


# ── 7. The section is always present ──────────────────────────────────


def test_section_renders_even_with_tailscale_absent(
    doctor_home, fake_tailscale, run_doctor
):
    """The bug being fixed was silence, so the header must always show."""
    write_settings(doctor_home, mode="localhost")
    fake_tailscale(installed=False)

    out = run_doctor()

    assert "Tailscale (remote access)" in out
    assert out.has_row("Tailscale binary")
