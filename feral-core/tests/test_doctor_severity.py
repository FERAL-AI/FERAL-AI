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
    # Under 512 MiB free on the volume holding ~/.feral. Red rather than
    # yellow because at that point writes actually start failing: the
    # memory store, the embedding queue, screen captures and background
    # job output all grow on every turn and none of them ask permission.
    # A full disk otherwise presents as "the machine got slow", which is
    # what happened on the author's Mac at 99% used with nothing in the
    # brain noticing or saying so.
    "Disk space",
    # Python version below the supported floor (3.11).
    "Python version",
    # The interpreter's SQLite was built without FTS5. MemoryStore and
    # KnowledgeGraph create five `CREATE VIRTUAL TABLE ... USING fts5`
    # tables during construction, so the brain raises SQLiteFeatureError
    # at boot and never serves a request. Same bar as "Python version":
    # nothing works until the operator changes interpreter.
    # NOTE the deliberate asymmetry with the sibling row "SQLite loadable
    # extensions", which is _info and appears in no allowlist: that one
    # only costs resident memory, and F-17 measured the numpy path it
    # falls back to as the faster of the two.
    "SQLite FTS5",
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
    # Multi-emission label, same shape as "Memory database" below: the
    # fresh-install branch is still _info ("no realtime provider key set"),
    # and this entry covers the branch where a realtime key IS configured
    # and every realtime probe rejected it. That row used to be a green
    # _pass derived from key presence alone, so an install with a rotated
    # OpenAI key printed a red "OpenAI Realtime: key rejected by API" in
    # the Voice providers section and a green "Voice runtime — key set"
    # here, and the green one is the row named after the feature.
    "Voice runtime",
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
    # llm.provider and llm.base_url naming different providers means
    # every call authenticates with the wrong key. Core: the brain has no
    # working LLM, and it reports the provider as healthy.
    "LLM endpoint",
    "Access mode coherence",
    # The resolver refused to produce a pair origin. Same class: pairing
    # is structurally impossible until the named condition is fixed.
    "Phone pairing",
    # ── Tailscale (Mode C remote pairing) ──
    #
    # Every one of these only reaches _fail when the operator has
    # selected access mode "remote", i.e. they have declared that
    # Tailscale Funnel IS their pairing transport. In that mode a
    # missing binary / dead daemon / logged-out node / absent funnel
    # each make phone pairing structurally impossible, which is the
    # same bar "Phone pairing" and "Access mode coherence" clear.
    # In any other mode these probes report _info, because not having
    # Tailscale is the normal state for someone pairing over WiFi.
    "Tailscale binary",
    "Tailscale daemon",
    "Tailscale account",
    "Tailscale Funnel",
    # Remote mode with no stored funnel URL, or a stored URL that no
    # live funnel backs. Both hand the phone an address that answers
    # nothing while every other probe stays green: exactly the silent
    # failure this section was added to end.
    "Remote access coherence",
}

ALLOWED_WARN_LABELS: set[str] = {
    # Stored vectors written at a width the active embedding provider
    # does not emit. Yellow rather than red because the brain runs and
    # every query still returns rows: the affected tier silently falls
    # back to keyword-only, which is precisely why it needs saying out
    # loud. Measured on the author's brain on 2026-09-05, 312 of 334
    # entities were unreachable this way while doctor showed a green
    # tick for the provider row directly above. Not _info, because a
    # store that disagrees with its provider is a malfunction with a
    # known remedy (`feral memory reembed`), not an un-enabled opt-in.
    # It cannot fire on a fresh install: an empty store has no columns
    # to be stale.
    "Stored embeddings",
    # Under 2 GiB free. Yellow because nothing is broken yet, but the
    # store grows on every turn, so this is the last point at which the
    # operator can act without losing work. The same label is also in
    # ALLOWED_FAIL_LABELS: it escalates to red below 512 MiB.
    "Disk space",
    # The FTS5 / loadable-extension probes themselves raised. Yellow and
    # not red on purpose: this is "doctor could not determine the answer",
    # which is a different statement from "the feature is missing". The
    # missing-feature branches are "SQLite FTS5" (_fail) and "SQLite
    # loadable extensions" (_info), and neither routes through here.
    "SQLite build features",
    # Source install where `pip show feral-ai` returns nothing — the
    # brain runs fine but `feral --version` will print "unknown".
    "FERAL package",
    # The running process is executing a different version from the one
    # installed on disk, i.e. somebody ran `pip install --upgrade` and
    # did not restart. Yellow, not red: the brain works, it is just not
    # the brain the operator thinks they are running, and the whole
    # point is that this state is otherwise completely silent. Measured
    # on a real install, a brain served for two days from code that
    # predated four releases.
    "Running version",
    # USER.md missing or near-empty — the agent can run, but it has no
    # idea who the operator is.
    "Identity (USER.md)",
    # Memory DB present but unreadable (corrupt / permission denied).
    "Memory database",
    # Configured FERAL port is occupied by some other process.
    "Port availability",
    # Settings could not be read, or the pair resolver raised something
    # other than PairUnavailable. Degraded reporting, not a broken brain.
    # settings.json unreadable, so coherence could not be checked.
    "LLM endpoint",
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
    # Also in ALLOWED_FAIL_LABELS, and multi-branch for the same reason
    # as "Local-agent grants" below. Fresh install (no realtime key) is
    # still _info. This entry is the branch where a key is configured but
    # the realtime probe registry produced no result for it, so doctor
    # can say the key is present and cannot say it works. Naming that gap
    # is the point: the previous code resolved it by calling it green.
    "Voice runtime",
    # The vault raised while doctor was reading a realtime key back. That
    # is not the same answer as "no key is configured", and collapsing
    # the two told an operator to set a key they had already set.
    "Voice runtime credential lookup",
    # Local-agent grants: this label has TWO branches. The
    # "no workspace_grants.json yet" branch was demoted to ``_info``
    # in v2026.5.36 (covered by ``test_demoted_probes_no_longer_warn``
    # via the behaviour test, since a fresh install hits that branch
    # and must produce zero warnings). The remaining ``_warn`` site
    # is the JSON-parse exception handler — a real read/parse failure
    # of an existing file, which is a legitimate degradation worth a
    # yellow flag.
    "Local-agent grants",
    # Local voice engines: two branches each. "installed, no weights
    # yet" is the ordinary state after `pip install 'feral-ai[stt]'`
    # and is ``_info``. The ``_warn`` site fires only when the operator
    # has *selected* that local engine and its model is missing, in
    # which case voice turns produce no transcript and voice replies
    # produce no audio. That is a real degradation of a configured
    # capability, and it is the same condition audio_pipeline already
    # logs at startup, so doctor saying nothing about it was the gap.
    "Local STT (faster-whisper)",
    "Local TTS (piper)",
    # Vector backend probe degradation branches: an unknown backend id
    # in settings.json, or the probe itself raising while trying to
    # verify the configured backend. Either way the operator should see
    # a yellow flag rather than a silent green.
    "Memory vector backend",
    # ── Tailscale (Mode C remote pairing) ──
    #
    # The shipped integrations.tailscale module failed to import. The
    # brain still runs; only the remote-access probes go dark.
    "Tailscale integration",
    # Also in ALLOWED_FAIL_LABELS. Tailscale is installed but the CLI
    # either ran out of the 2.5s probe budget or errored, while the
    # operator is NOT in remote mode. Nothing they depend on is broken,
    # but a wedged daemon is a real degradation and is not the expected
    # fresh-install state (which is "not installed" -> _info).
    "Tailscale daemon",
    "Tailscale Funnel",
    # Also in ALLOWED_FAIL_LABELS. Warns for the reverse incoherence: a
    # Funnel is publishing the brain port to the internet while the
    # access mode says loopback/LAN. Nothing is broken, but nobody asked
    # for that exposure, so it must not be silent.
    "Remote access coherence",
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

        Some labels (``Memory database``, ``Local-agent grants``,
        ``Voice runtime``) have multiple emission sites — one demoted
        branch (fresh install) plus a legitimate degradation branch
        (corrupt DB, JSON read error, a configured realtime key that the
        provider rejects). The behaviour test
        ``test_fresh_install_has_no_warnings_or_failures`` is the
        authoritative guard for those: on a clean install only the
        demoted branch fires, and zero warnings/failures are
        permitted in the output.

        ``Voice runtime`` left this set when the row stopped being
        answerable from key presence. Its fresh-install branch is still
        ``_info`` and the behaviour test still pins that; what it no
        longer does is report green for a key that has been rotated,
        which is a claim the row was making while the Voice providers
        section three blocks up rendered the same key as rejected.
        """
        single_emission_demoted = {
            "Chrome (CDP endpoint)",
        }
        # ``Local STT`` and ``Local TTS`` left this set when each gained
        # a second emission site. Installed-with-no-weights is the
        # normal shape of `pip install 'feral-ai[stt]'` and stays
        # ``_info``; the new ``_warn`` fires only when that engine is
        # also the operator's *selected* provider, which means voice
        # produces no transcript or no audio. A clean install cannot hit
        # that branch (nothing is selected), so
        # ``test_fresh_install_has_no_warnings_or_failures`` remains the
        # authoritative guard, exactly as it is for ``Local-agent
        # grants`` and ``Memory database``.
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


def _config_override_env_names() -> set[str]:
    """Every env var ``ConfigLoader._apply_env_overrides`` maps to a setting.

    The mapping is a dict local to that method, so it cannot be
    imported. Reading the names out of the source keeps this in step
    with it; a stale hand-written copy is what would let this rot.
    ``FERAL_HOME`` is deliberately excluded: the fixture sets it.
    """
    src = CLI_MAIN_PATH.parent.parent / "config" / "loader.py"
    names = set(re.findall(r'"([A-Z][A-Z0-9_]*)":\s*\(', src.read_text()))
    names.discard("FERAL_HOME")
    return names



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

    # And every config override, because a fresh install has none of
    # those either. Setting FERAL_HOME alone does not make the process
    # look fresh: ``ConfigLoader`` publishes the merged settings back
    # into ``os.environ`` by design (``export_as_env``), and env beats
    # settings.json, so any earlier test that wrote a setting leaves it
    # overriding this fixture's empty home for the rest of the session.
    #
    # Measured in the full suite: ``FERAL_TTS_PROVIDER`` survived as
    # "piper", so doctor reported
    #
    #     Local TTS (piper)  selected as your TTS provider but no voice
    #     is downloaded, so voice replies produce no audio
    #
    # against a home that had never selected anything. The test failed
    # in the full run and passed alone, which is the signature of
    # exactly this. It reproduces on an untouched tree.
    #
    # The names are read out of the loader's own override table rather
    # than restated here, so adding an override there cannot silently
    # drift from this list.
    for k in _config_override_env_names():
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

    # The memory vector backend probe loads the sqlite-vec EXTENSION to
    # find out whether it is usable, which is a property of the
    # interpreter this suite happens to run on, not of the install being
    # modelled: pyenv builds Python without
    # ``enable_load_extension`` on macOS, python.org and Homebrew do
    # not. Left live, "a clean install emits zero warnings" would be
    # true or false depending on whose machine ran it. Pin it to the
    # healthy host this fixture describes; the degraded host is asserted
    # in tests/test_doctor_vector_backend_truth.py.
    from memory.vector_index_backends import sqlite_vec as _sqlite_vec_mod

    monkeypatch.setattr(_sqlite_vec_mod, "sqlite_vec_available", lambda: True)

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


class TestLocalVoiceSeverityDependsOnSelection:
    """A missing model matters only if you chose that engine.

    Two states look identical on the filesystem: `pip install
    'feral-ai[stt]'` with no weights yet, which is the normal shape of a
    fresh install, and "the engine you selected cannot run", which means
    voice turns produce no transcript. Doctor said nothing about either,
    and reported the list of *selectable* models as though they were
    downloaded.

    Warning on both would turn a clean install yellow and break the
    v2026.5.36 contract. Warning on neither is what shipped, and it left
    a configured-but-unusable engine invisible until the user spoke.
    """

    CAPS_NO_WEIGHTS = {
        "local_stt": False, "local_tts": False,
        "stt_models": ["tiny", "base", "small"],
        "tts_voices": ["en_US-lessac-medium"],
        "stt_models_present": [], "tts_voices_present": [],
        "stt_importable": True, "tts_importable": True,
    }

    def _doctor_lines(self, monkeypatch, selected):
        import contextlib
        import io
        from unittest.mock import patch

        import cli.main as m

        buf = io.StringIO()
        with patch(
            "perception.audio_pipeline.detect_local_audio_capabilities",
            return_value=dict(self.CAPS_NO_WEIGHTS),
        ), patch.object(m, "_local_voice_selected", return_value=selected):
            with contextlib.redirect_stdout(buf):
                try:
                    m.cmd_doctor()
                except SystemExit:
                    pass
        return [
            ln.strip() for ln in buf.getvalue().splitlines()
            if "Local STT" in ln or "Local TTS" in ln
        ]

    def test_selected_but_missing_is_a_warning(self, monkeypatch):
        lines = self._doctor_lines(monkeypatch, (True, True))
        assert lines, "doctor printed no Local Audio rows"
        assert all("⚠" in ln for ln in lines), lines

    def test_merely_installed_is_not_a_warning(self, monkeypatch):
        """The fresh-install shape must stay quiet."""
        lines = self._doctor_lines(monkeypatch, (False, False))
        assert lines, "doctor printed no Local Audio rows"
        assert all("ℹ" in ln for ln in lines), lines
        assert not any("⚠" in ln or "✘" in ln for ln in lines), lines

    def test_the_selection_probe_never_raises(self):
        """Doctor must describe a broken machine, not crash on one."""
        from unittest.mock import patch

        import cli.main as m

        with patch("config.loader.load_settings", side_effect=OSError("no disk")):
            assert m._local_voice_selected() == (False, False)

    def test_the_selection_probe_understands_both_spellings(self, monkeypatch):
        """`faster-whisper` and `faster_whisper` are the same engine."""
        from unittest.mock import patch

        import cli.main as m

        for spelling in ("faster-whisper", "faster_whisper", "local"):
            with patch(
                "config.loader.load_settings",
                return_value={"audio": {"stt_provider": spelling, "tts_provider": "openai"}},
            ):
                stt, tts = m._local_voice_selected()
                assert stt is True, f"{spelling!r} not recognised as a local STT pick"
                assert tts is False
