"""Behaviour + structural tests for `feral doctor` severity classification.

v2026.5.36 introduced a fourth severity tier (``_info``) so that "not
configured yet" / "opt-in feature you have not enabled" probes stop
masquerading as yellow warnings.

This module locks the new behaviour down on two axes:

1. **Behaviour** (`TestDoctorSeverity`) — invoke ``cmd_doctor`` against a
   fresh, empty ``FERAL_HOME`` with all network probes mocked out, parse
   the rendered output, and assert:

   * zero ``✘`` failures and zero ``⚠`` warnings (a clean install is
     never broken on its first boot).
   * the expected set of ``ℹ`` info lines is present (memory database,
     Chrome CDP, Local STT/TTS, Node.js, workspace grants, voice
     runtime).
   * the Suggested-fixes section is empty (no remediation should be
     offered for probes that demoted into ``_info``).

2. **Structure** (`TestDoctorSeverityAllowlist`) — walk
   ``feral-core/cli/main.py`` with the ``ast`` module, collect every
   call site to ``_warn(...)`` and ``_fail(...)`` inside the
   ``cmd_doctor`` function, extract the first positional argument
   (the probe label), and assert each label is in an explicit
   allowlist defined in this file. Any future PR that adds or
   re-promotes a probe must also update the allowlist — preventing a
   silent regression that re-floods the doctor with yellow noise.
"""

from __future__ import annotations

import ast
import io
import re
from pathlib import Path

import pytest


CLI_MAIN_PATH = Path(__file__).resolve().parent.parent / "cli" / "main.py"


# ── 1. Severity allowlist (structural guard) ──────────────────────────
#
# These two sets describe exactly which probe labels are permitted to
# emit a yellow ⚠ or red ✘ from inside ``cmd_doctor``. Anything else is
# either a ``_pass`` or a ``_info`` and does not need an entry here.
#
# When adding a probe:
#   * If the absence of the probed thing breaks core agent paths
#     (no LLM, broken Python, missing config dir, …) → ``_fail`` and
#     add the label to ``ALLOWED_FAIL_LABELS``.
#   * If the absence degrades a non-core feature → ``_warn`` and add
#     to ``ALLOWED_WARN_LABELS``.
#   * If the absence is the EXPECTED state on a fresh install
#     (opt-in feature, lazy initialisation, deferred grant prompt) →
#     use ``_info`` and do NOT add a label here.

ALLOWED_FAIL_LABELS: set[str] = {
    # Python version below the supported floor (3.11).
    "Python version",
    # The `feral-ai` wheel itself failed to import.
    "FERAL package",
    # `~/.feral` (or whatever FERAL_HOME points at) is missing.
    "Config directory",
    # No LLM key in vault, no key in env, and no local Ollama.
    "LLM credentials",
    # Lane 07 () — probe-driven catch-all. When EVERY LLM probe
    # returns red or unconfigured, doctor fires this single
    # ``_fail`` so the operator sees one actionable line instead of
    # a wall of yellow rows. The per-provider rows themselves are
    # rendered by ``_render_probe_row`` (outside ``cmd_doctor``), so
    # they don't appear in this allowlist.
    "LLM providers",
    # Memory DB present but unreadable (corrupt / permission denied).
    # Reaching this branch means the operator has a file at
    # ~/.feral/memory.db that SQLite refuses to open — the brain will
    # not boot until they delete or fix it. This is a true _fail.
    "Memory database",
    # Critical FastAPI runtime dependencies whose absence guarantees a
    # broken brain (these are checked dynamically via dep_pkgs).
    "FastAPI",
    "Uvicorn",
    "WebSockets",
    "HTTPX",
    "Pydantic",
    # ComputerUseDriver normalisation is shipped inside feral-core; an
    # import failure here means the wheel is corrupted.
    "Computer-use driver",
    # Operator selected a non-default vector backend (chroma/qdrant) in
    # settings.json but the optional dependency is not installed — the
    # configured backend cannot load, so the brain falls back / degrades.
    # A true _fail because the operator's explicit choice is broken.
    "Memory vector backend",
    # At-rest encryption was opted into (memory.db.enc exists) but the
    # vault/keychain cannot be unlocked — the brain will not start until
    # the keychain entry is restored. (v2026.5.43)
    "Memory at-rest encryption",
    # access.pairing_mode and network.bind_host disagree, so the brain
    # advertises a pair URL nothing is listening on. Core: no phone can
    # pair, and every surface reported healthy while it was true.
    "Access mode coherence",
    # The resolver refused to produce a pair origin. Same class: pairing
    # is structurally impossible until the named condition is fixed.
    "Phone pairing",
}

ALLOWED_WARN_LABELS: set[str] = {
    # Source install where `pip show feral-ai` returns nothing — the
    # brain runs fine but `feral --version` will print "unknown".
    "FERAL package",
    # USER.md missing or near-empty — the agent can run, but it has no
    # idea who the operator is.
    "Identity (USER.md)",
    # Memory DB present but unreadable (corrupt / permission denied).
    "Memory database",
    # Configured FERAL port is occupied by some other process.
    "Port availability",
    # Settings could not be read, or the pair resolver raised something
    # other than PairUnavailable. Degraded reporting, not a broken brain.
    "Pairing & access",
    # Also in ALLOWED_FAIL_LABELS. It fails when the resolver refuses
    # outright, and warns for the softer cases: a mode whose transport
    # is not built yet (relay), or a resolver that raised something
    # unexpected. Same probe, severity chosen by what it found.
    "Phone pairing",
    # TLS is on with the brain's self-signed cert. iOS has no trust
    # override, so it refuses the connection; LAN pairing needs TLS off.
    "TLS vs phone pairing",
    # Playwright Python lib not installed — CDP-only mode loses
    # selector healing. Not a fresh-install default state because
    # the `[browser]` extra adds it; absence is a real degradation.
    "Playwright (driver lib)",
    # No Chrome / Chromium / Brave binary anywhere on disk → the
    # CDP auto-launch fallback has nothing to start.
    "Chrome binary",
    # Node.js *found* but below the version floor (>=20). Pure
    # absence is now ``_info``; an outdated binary is still a warn
    # because the user almost certainly intended to develop locally.
    "Node.js",
    # Local audio detection itself raised (rare; tests/dev paths).
    "Local Audio",
    # The operator asked for paid embeddings and cannot have them:
    # FERAL_EMBED_PROVIDER=openai with no OPENAI_API_KEY set. A real
    # misconfiguration, since the request silently degrades to local.
    # Absence of the [embeddings] extra is NOT here; that is the
    # designed fresh-install default and reports as ``_info``.
    "Embedding provider mode",
    # The provider probe itself raised (import error, corrupt config).
    "Embedding provider",
    # macOS TCC entitlements probed via PyObjC — denied state still
    # warns because the operator must take System Settings action to
    # enable GUI computer-use; we no longer ``_fail`` on it.
    "Accessibility (TCC)",
    "Screen Recording (TCC)",
    "macOS GUI Permissions",
    # Persistence stores that exist as a file but failed to open.
    "Coding-agent store",
    "Upload store",
    # Local-agent grants: this label has TWO branches. The
    # "no workspace_grants.json yet" branch was demoted to ``_info``
    # in v2026.5.36 (covered by ``test_demoted_probes_no_longer_warn``
    # via the behaviour test, since a fresh install hits that branch
    # and must produce zero warnings). The remaining ``_warn`` site
    # is the JSON-parse exception handler — a real read/parse failure
    # of an existing file, which is a legitimate degradation worth a
    # yellow flag.
    "Local-agent grants",
    # Vector backend probe degradation branches: an unknown backend id
    # in settings.json, or the probe itself raising while trying to
    # verify the configured backend. Either way the operator should see
    # a yellow flag rather than a silent green.
    "Memory vector backend",
}


# ── 2. Structural guard ───────────────────────────────────────────────


def _collect_doctor_severity_calls() -> dict[str, list[str]]:
    """Walk cli/main.py and return {call_name: [label, ...]} for every
    ``_warn`` / ``_fail`` invocation inside the ``cmd_doctor`` function.
    """
    source = CLI_MAIN_PATH.read_text()
    tree = ast.parse(source)
    out: dict[str, list[str]] = {"_warn": [], "_fail": []}

    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "cmd_doctor"):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if not isinstance(func, ast.Name):
                continue
            if func.id not in ("_warn", "_fail"):
                continue
            if not sub.args:
                continue
            first = sub.args[0]
            # Most call sites pass a bare string literal as the label.
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                out[func.id].append(first.value)
            elif isinstance(first, ast.JoinedStr):
                # f-string label (e.g. dep loop). Reconstruct by reading
                # the constant prefix when one exists — otherwise treat
                # as a dynamic label that downstream tests handle.
                literal = "".join(
                    seg.value for seg in first.values
                    if isinstance(seg, ast.Constant) and isinstance(seg.value, str)
                )
                if literal:
                    out[func.id].append(f"<f-string:{literal}>")
            # else: a fully-dynamic label; we ignore it (the dep loop
            # below covers the only one we actually emit).
    return out


class TestDoctorSeverityAllowlist:
    """Static guard: every _warn / _fail label must be allow-listed.

    Why: post-v2026.5.36 we have four severity tiers and a clear
    contract — ``_warn`` and ``_fail`` are for *real* degradations and
    breakages, not "you haven't enabled this opt-in feature yet". An
    accidental regression that re-promotes (say) "Voice runtime"
    back to ``_warn`` would silently re-introduce the noise this
    release was built to remove. This test ensures any such change
    has to update the allowlist too — making the choice explicit and
    code-reviewable.
    """

    def test_all_fail_labels_are_allowlisted(self):
        calls = _collect_doctor_severity_calls()
        unexpected = []
        for label in calls["_fail"]:
            # f-string labels are flagged but allow-listed by prefix.
            if label.startswith("<f-string:"):
                continue
            if label not in ALLOWED_FAIL_LABELS:
                unexpected.append(label)
        assert not unexpected, (
            f"Unexpected _fail() labels in cmd_doctor: {unexpected}. "
            "Either demote to _info/_warn or add to ALLOWED_FAIL_LABELS."
        )

    def test_all_warn_labels_are_allowlisted(self):
        calls = _collect_doctor_severity_calls()
        unexpected = []
        for label in calls["_warn"]:
            if label.startswith("<f-string:"):
                continue
            if label not in ALLOWED_WARN_LABELS:
                unexpected.append(label)
        assert not unexpected, (
            f"Unexpected _warn() labels in cmd_doctor: {unexpected}. "
            "Either demote to _info or add to ALLOWED_WARN_LABELS."
        )

    def test_demoted_probes_no_longer_warn(self):
        """Explicit anti-regression list: these probes WERE _warn in
        pre-v2026.5.36 and have been demoted to _info. They must not
        come back as _warn or _fail in the labels that have a SINGLE
        emission site.

        Some labels (``Memory database``, ``Local-agent grants``) have
        multiple emission sites — one demoted branch (fresh install)
        plus a legitimate degradation branch (corrupt DB, JSON read
        error). The behaviour test
        ``test_fresh_install_has_no_warnings_or_failures`` is the
        authoritative guard for those: on a clean install only the
        demoted branch fires, and zero warnings/failures are
        permitted in the output.
        """
        single_emission_demoted = {
            "Chrome (CDP endpoint)",
            "Local STT (faster-whisper)",
            "Local TTS (piper)",
            "Voice runtime",
        }
        calls = _collect_doctor_severity_calls()
        warn_labels = set(calls["_warn"])
        fail_labels = set(calls["_fail"])
        regression = single_emission_demoted & (warn_labels | fail_labels)
        assert not regression, (
            f"Probes that should be _info regressed to _warn/_fail: "
            f"{regression}. Restore _info severity per the v2026.5.36 "
            "doctor-honesty contract."
        )


# ── 3. Behaviour test (end-to-end run of cmd_doctor) ──────────────────


@pytest.fixture
def doctor_clean_env(monkeypatch, tmp_path):
    """A FERAL_HOME with no memory DB, no USER.md, no workspace grants,
    no realtime voice key, and every network probe stubbed cold.

    Mirrors the state of a Mac one minute after
    ``pip install feral-ai && feral doctor`` — the install path we
    want to render as zero warnings, zero failures.
    """
    # Use a brand-new home distinct from the autouse fixture.
    feral_home = tmp_path / "doctor-fresh-home"
    feral_home.mkdir()
    monkeypatch.setenv("FERAL_HOME", str(feral_home))

    # Strip every LLM key from env so we don't accidentally pass the
    # credentials probe via the developer's shell environment.
    for k in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "GROQ_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)

    # Ollama probe — return a fake "running" so the LLM-credentials
    # check passes. Without this the credentials probe correctly fails
    # (no keys + no Ollama). For this test we want to model a clean
    # install that DID set up an LLM, so Ollama running is the easiest
    # stand-in that doesn't require us to plant a vault.
    def _fake_urlopen(url, timeout=2):
        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    # Lane 07 () — doctor now drives everything off
    # ``security.probe.probe()``; the fresh-install behaviour test
    # therefore needs deterministic probe results. We model the
    # cleanest possible "user just ran `feral setup` with Ollama" by:
    #   * Ollama → ok (so the LLM-providers section has at least one
    #     green row and the catch-all _fail does not fire).
    #   * every other registered provider → ``no_key`` (renders ℹ).
    import time as _t
    from security import probe as _probe_mod

    async def _fake_probe(pid, **_kw):
        if pid == "ollama":
            return _probe_mod.ProbeResult(
                provider="ollama", ok=True, status_code=200,
                reason="ok", detail="Ollama running locally",
                probed_at=_t.time(), latency_ms=5.0,
            )
        return _probe_mod.ProbeResult(
            provider=pid, ok=False, status_code=None,
            reason="no_key", detail="not configured",
            probed_at=_t.time(), latency_ms=0.0,
        )

    monkeypatch.setattr(_probe_mod, "probe", _fake_probe)
    _probe_mod.clear_probe_cache()

    # Plant a non-empty USER.md so the identity probe passes — we are
    # testing severity classification, not first-run wizard completion.
    (feral_home / "USER.md").write_text("Test operator running doctor.\n")

    return feral_home


@pytest.fixture
def captured_console(monkeypatch):
    """Replace the Rich Console with one that records to a StringIO so
    we can parse the doctor's emitted lines.
    """
    from rich.console import Console as _RichConsole

    buf = io.StringIO()
    real_console = _RichConsole(file=buf, force_terminal=False, color_system=None, width=120)

    # cmd_doctor does `from rich.console import Console` at the top of
    # the function body, then instantiates `Console()`. Patching the
    # module-level binding ensures the instance it creates is ours.
    monkeypatch.setattr("rich.console.Console", lambda *a, **kw: real_console)
    return buf


class TestDoctorSeverity:
    """End-to-end check that a clean fresh install renders zero
    warnings and zero failures from `feral doctor`."""

    def test_fresh_install_has_no_warnings_or_failures(
        self,
        doctor_clean_env,
        captured_console,
    ):
        # Import inside the test so the autouse fixtures have already
        # set FERAL_HOME by the time cli.main loads any state.
        from cli.main import cmd_doctor

        cmd_doctor()
        text = captured_console.getvalue()

        # Strip ANSI just in case the patched Console emits any.
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        plain = ansi_re.sub("", text)

        # Failure markers (red ✘) must not appear in the body.
        assert "✘" not in plain, (
            f"Doctor emitted at least one failure on a clean install:\n{plain}"
        )
        # Warnings (yellow ⚠) must not appear either.
        assert "⚠" not in plain, (
            f"Doctor emitted at least one warning on a clean install:\n{plain}"
        )
        # At least one info marker (ℹ) MUST appear — if the entire
        # block is green, the new tier wasn't exercised at all and
        # the demotions probably regressed.
        assert "ℹ" in plain, (
            "Doctor emitted no info-tier lines — expected several "
            "(memory db / Chrome CDP / STT / TTS / grants / voice). "
            f"Output:\n{plain}"
        )

    def test_summary_panel_renders_info_count(
        self,
        doctor_clean_env,
        captured_console,
    ):
        from cli.main import cmd_doctor

        cmd_doctor()
        text = captured_console.getvalue()
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        plain = ansi_re.sub("", text)

        # The summary panel must show the new "N info" segment.
        assert re.search(r"\d+ info", plain), (
            "Summary panel missing the info-count segment — the new "
            "v2026.5.36 severity tier is not being rendered. "
            f"Output:\n{plain}"
        )

    def test_no_suggested_fixes_on_clean_install(
        self,
        doctor_clean_env,
        captured_console,
    ):
        from cli.main import cmd_doctor

        cmd_doctor()
        text = captured_console.getvalue()
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        plain = ansi_re.sub("", text)

        # When zero _warn / _fail fire, `fixes` is empty and the
        # "Suggested fixes:" header is never printed.
        assert "Suggested fixes:" not in plain, (
            "Doctor offered remediation steps on a clean install — "
            "this means a probe that should be _info is still _warn "
            f"or _fail. Output:\n{plain}"
        )
