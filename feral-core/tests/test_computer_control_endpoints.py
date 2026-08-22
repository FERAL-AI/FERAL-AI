"""End-to-end tests for FERAL's computer-control surface.

Covers three things that were previously untested and, in the first
case, actively wrong:

1. **window_list must not lie.** ``gui_computer_use._get_windows`` ran an
   AppleScript, read only its stdout, and returned ``[]`` from a bare
   ``except Exception``. ``_window_list`` then wrapped that in
   ``{"success": True, "status_code": 200}``. On the dev Mac the script
   failed with ``System Events got an error: osascript is not allowed
   assistive access. (-25211)`` and the model was told, with a 200, that
   the machine had no windows open. Even on the success path each row
   was ``{"raw": <one comma-joined blob for the whole machine>}`` — no
   title, no bounds — while the manifest promised "titles and bounds".

2. **The Accessibility probe must name its subject.**
   ``AXIsProcessTrustedWithOptions`` reports the *Python* process.
   ``osascript`` is a different binary with a different TCC identity, and
   the two disagreed live: ``granted`` from the Python probe in the same
   second osascript returned -25211. ``feral doctor`` showed one green
   row over a broken capability.

3. **The duplicate control surface.** ``gui_computer_use`` and
   ``desktop_automation`` both expose click / type / scroll / move. The
   contract is that ``desktop_automation`` is a shim that delegates, so
   both routes must land on the same implementation.

Tests that need a real Mac are marked ``darwin_only``; everything else
runs on the CI matrix. Nothing here clicks, types, or presses a key: a
test suite that drives the host's mouse would fire into whatever window
happens to be focused.
"""

from __future__ import annotations

import asyncio
import base64
import platform
import subprocess

import pytest

from skills.impl.gui_computer_use import (
    GUIComputerUseSkill,
    _as_str,
    collect_windows,
    run_osascript,
)

darwin_only = pytest.mark.skipif(
    platform.system() != "Darwin", reason="macOS-only capability"
)


def _run(coro):
    return asyncio.run(coro)


def _display_is_asleep() -> bool:
    """True when the main display is asleep, so a capture is uniformly black.

    A blank screenshot has two very different causes: Screen Recording
    permission is denied, which is a real defect worth failing on, and
    the display simply went to sleep, which is not. They are
    indistinguishable from the pixels alone.

    This matters because `StayAwake` uses `caffeinate -i`, which inhibits
    idle *system* sleep and deliberately not display sleep, so on a full
    suite run of several minutes the screen can and does sleep. That is
    exactly how this test failed once in a full run while passing in
    isolation and in the run before it.

    Returns False when the probe is unavailable, so an environment
    without pyobjc keeps the strict assertion rather than skipping.
    """
    try:
        import Quartz
        return bool(Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()))
    except Exception:
        return False


# ── 1. window_list honesty ───────────────────────────────────────


class TestWindowListHonesty:
    def test_failure_is_not_reported_as_an_empty_desktop(self, monkeypatch):
        """A backend failure must surface as a failure, not as zero windows.

        This is the regression that motivated the whole change: the old
        code could only ever answer ``success=True, windows=[]``.
        """
        import skills.impl.gui_computer_use as gui

        monkeypatch.setattr(gui.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            gui, "_windows_via_quartz",
            lambda: (None, "PyObjC Quartz not importable: boom"),
        )
        monkeypatch.setattr(
            gui, "_windows_via_applescript",
            lambda: (None, "osascript exit 1", None),
        )

        result = _run(GUIComputerUseSkill().execute("window_list", {}, {}))

        assert result["success"] is False
        assert result["status_code"] != 200
        assert "boom" in result["error"] or "osascript" in result["error"]
        # The attempt log has to name every backend so the failure is
        # diagnosable without re-running anything.
        backends = {a["backend"] for a in result["data"]["attempts"]}
        assert backends == {"coregraphics", "applescript"}

    def test_accessibility_denial_mints_a_permission_card(self, monkeypatch):
        """-25211 must become a tcc_card, not an opaque 500."""
        import skills.impl.gui_computer_use as gui
        from agents.tcc_card import build_tcc_card, parse_tcc_error

        monkeypatch.setattr(gui.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            gui, "_windows_via_quartz", lambda: (None, "Quartz unavailable"),
        )
        monkeypatch.setattr(
            gui, "_windows_via_applescript",
            lambda: (
                None,
                "System Events got an error: osascript is not allowed "
                "assistive access. (-25211)",
                "accessibility",
            ),
        )

        result = _run(GUIComputerUseSkill().execute("window_list", {}, {}))

        assert result["success"] is False
        assert result["status_code"] == 403
        assert result["error"] == "tcc_denied:accessibility"
        assert parse_tcc_error(result["error"]) == "accessibility"
        card = build_tcc_card("accessibility")
        assert card["type"] == "tcc_card"
        assert "Privacy_Accessibility" in card["macos_deeplink"]

    def test_zero_windows_is_a_success_not_a_failure(self, monkeypatch):
        """The inverse lie is equally forbidden.

        A backend that succeeds and finds nothing open is a real answer.
        Reporting *that* as an error would be the same defect mirrored.
        """
        import skills.impl.gui_computer_use as gui

        monkeypatch.setattr(gui.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(gui, "_windows_via_quartz", lambda: ([], None))
        monkeypatch.setattr(
            gui, "_windows_via_applescript", lambda: ([], None, None),
        )

        result = _run(GUIComputerUseSkill().execute("window_list", {}, {}))
        assert result["success"] is True
        assert result["data"]["count"] == 0

    def test_titled_backend_wins_over_untitled(self, monkeypatch):
        """CoreGraphics returns bounds without Screen Recording but blanks
        every title. When that happens, System Events' titled rows win."""
        import skills.impl.gui_computer_use as gui

        untitled = [{"app": "Safari", "title": "", "x": 0, "y": 0,
                     "width": 10, "height": 10, "pid": 1,
                     "window_id": 2, "focused": False}]
        titled = [{"app": "Safari", "title": "Apple", "x": 0, "y": 0,
                   "width": 10, "height": 10, "pid": None,
                   "window_id": None, "focused": False}]
        monkeypatch.setattr(gui.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(gui, "_windows_via_quartz", lambda: (untitled, None))
        monkeypatch.setattr(
            gui, "_windows_via_applescript", lambda: (titled, None, None),
        )

        listing = collect_windows()
        assert listing.ok is True
        assert listing.source == "applescript"
        assert listing.windows[0]["title"] == "Apple"

    def test_non_darwin_says_so(self, monkeypatch):
        import skills.impl.gui_computer_use as gui

        monkeypatch.setattr(gui.platform, "system", lambda: "Linux")
        listing = collect_windows()
        assert listing.ok is False
        assert "macOS" in (listing.error or "")


@darwin_only
class TestWindowListOnThisMac:
    """Executed against the real machine, not a mock."""

    def test_enumerates_real_windows_with_titles_and_bounds(self):
        result = _run(GUIComputerUseSkill().execute("window_list", {}, {}))

        assert result["success"] is True, result["error"]
        assert result["status_code"] == 200
        windows = result["data"]["windows"]
        assert result["data"]["source"] in {"coregraphics", "applescript"}

        # A Mac running a test suite has at least a terminal on screen.
        assert windows, "no windows found while a test runner is on screen"
        for w in windows:
            assert set(w) >= {
                "app", "title", "x", "y", "width", "height", "focused",
            }
            assert isinstance(w["x"], int) and isinstance(w["y"], int)
            assert isinstance(w["width"], int) and isinstance(w["height"], int)
            # The pre-fix shape. Its presence means the parse regressed.
            assert "raw" not in w
        assert any(w["title"] for w in windows), "every title was empty"
        assert any(w["width"] > 0 and w["height"] > 0 for w in windows)

    def test_the_old_aggregate_applescript_is_still_broken(self):
        """Documents *why* the query shape changed.

        The pre-fix script is still on this machine's OS and still fails.
        If a future macOS makes it work, this test flips and the comment
        in ``_WINDOW_LIST_APPLESCRIPT`` can be revisited.
        """
        res = run_osascript(
            'tell application "System Events" to get {name, position, size} '
            "of every window of every process whose visible is true"
        )
        assert res.ok is False, (
            "the aggregate window query now succeeds; re-evaluate "
            "_WINDOW_LIST_APPLESCRIPT"
        )

    def test_per_process_applescript_backend_works(self):
        from skills.impl.gui_computer_use import _windows_via_applescript

        windows, error, tcc = _windows_via_applescript()
        assert error is None, error
        assert tcc is None
        assert windows is not None
        for w in windows:
            assert isinstance(w["x"], int)

    def test_coregraphics_backend_works(self):
        from skills.impl.gui_computer_use import _windows_via_quartz

        windows, error = _windows_via_quartz()
        assert error is None, error
        assert windows is not None

    def test_screenshot_returns_a_real_jpeg(self):
        result = _run(GUIComputerUseSkill().execute("screenshot", {}, {}))
        assert result["success"] is True, result["error"]
        raw = base64.b64decode(result["data"]["image_base64"])
        assert len(raw) > 5_000
        # JPEG SOI marker. PIL is a base dependency, so the resize +
        # re-encode path is the one that runs.
        assert raw[:2] == b"\xff\xd8"

    def test_screenshot_is_not_blank(self):
        from PIL import Image, ImageStat

        result = _run(GUIComputerUseSkill().execute("screenshot", {}, {}))
        assert result["success"] is True, result["error"]
        img = Image.open(
            __import__("io").BytesIO(
                base64.b64decode(result["data"]["image_base64"])
            )
        )
        assert img.width <= 1920
        # A capture that came back as a uniform rectangle means Screen
        # Recording is denied (macOS hands back wallpaper) or the encode
        # dropped the frame. A sleeping display produces the same uniform
        # image for a reason that is not a defect, so it is separated out
        # rather than folded into the assertion.
        if max(ImageStat.Stat(img).stddev) <= 5 and _display_is_asleep():
            pytest.skip(
                "the display is asleep, so every capture is uniformly black; "
                "caffeinate -i inhibits system sleep but deliberately not "
                "display sleep"
            )
        assert max(ImageStat.Stat(img).stddev) > 5

    def test_cursor_position_is_real(self):
        result = _run(GUIComputerUseSkill().execute("cursor_position", {}, {}))
        assert result["success"] is True, result["error"]
        assert isinstance(result["data"]["x"], int)
        assert isinstance(result["data"]["y"], int)


# ── window_focus ─────────────────────────────────────────────────


class TestScreenshotEncoding:
    """``screencapture -t png`` writes RGBA on macOS, and JPEG has no
    alpha channel. ``capture_screenshot_bytes`` called ``img.save(...,
    "JPEG")`` straight on the decoded image, so every capture raised
    ``OSError: cannot write mode RGBA as JPEG`` and the endpoint returned
    a 500 — verified live, 100% of calls, before the flatten was added.
    """

    @staticmethod
    def _png(mode: str, size=(64, 48)) -> bytes:
        import io

        from PIL import Image

        src = Image.new("RGBA", size, (10, 200, 30, 128)).convert(mode)
        buf = io.BytesIO()
        src.save(buf, format="PNG")
        return buf.getvalue()

    @pytest.mark.parametrize("mode", ["RGBA", "LA", "P", "RGB", "L"])
    def test_every_source_mode_encodes_to_jpeg(self, mode):
        from skills.impl.gui_computer_use import encode_for_vlm

        encoded = encode_for_vlm(self._png(mode))
        assert encoded[:2] == b"\xff\xd8", f"{mode} did not encode to JPEG"

    def test_rgba_was_the_failing_case(self):
        """Pins the exact pre-fix failure so it cannot come back."""
        import io

        from PIL import Image

        from skills.impl.gui_computer_use import encode_for_vlm

        raw = self._png("RGBA")
        assert Image.open(io.BytesIO(raw)).mode == "RGBA"
        with pytest.raises(OSError, match="cannot write mode RGBA as JPEG"):
            Image.open(io.BytesIO(raw)).save(io.BytesIO(), format="JPEG")
        assert encode_for_vlm(raw)[:2] == b"\xff\xd8"

    def test_oversized_capture_is_downscaled(self):
        import io

        from PIL import Image

        from skills.impl.gui_computer_use import (
            _SCREENSHOT_MAX_WIDTH, encode_for_vlm,
        )

        raw = self._png("RGBA", size=(_SCREENSHOT_MAX_WIDTH * 2, 400))
        out = Image.open(io.BytesIO(encode_for_vlm(raw)))
        assert out.width == _SCREENSHOT_MAX_WIDTH

    def test_missing_pillow_returns_the_raw_bytes(self, monkeypatch):
        import builtins

        from skills.impl.gui_computer_use import encode_for_vlm

        real_import = builtins.__import__

        def _no_pil(name, *a, **k):
            if name == "PIL":
                raise ImportError("no pillow")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_pil)
        raw = b"not-an-image"
        assert encode_for_vlm(raw) == raw


class TestWindowFocus:
    def test_missing_title_is_a_400(self):
        result = _run(GUIComputerUseSkill().execute("window_focus", {}, {}))
        assert result["success"] is False
        assert result["status_code"] == 400

    def test_applescript_string_escaping(self):
        """A window title containing a quote used to break the script.

        The pre-fix code interpolated the caller's title straight into an
        AppleScript literal.
        """
        assert _as_str('He said "hi"') == '"He said \\"hi\\""'
        assert _as_str("back\\slash") == '"back\\\\slash"'

    @darwin_only
    def test_unknown_window_is_a_404_with_candidates(self):
        result = _run(GUIComputerUseSkill().execute(
            "window_focus", {"title": "NoSuchAppZzzz42"}, {},
        ))
        assert result["success"] is False
        assert result["status_code"] == 404
        assert "candidates" in result["data"]


# ── 2. Accessibility probe honesty ───────────────────────────────


class TestAccessibilityProbeHonesty:
    def test_python_and_osascript_are_separate_rows(self):
        from security.macos_permissions import all_gui_permission_statuses

        names = {s.permission for s in all_gui_permission_statuses()}
        assert "accessibility" in names
        assert "accessibility_osascript" in names

    @darwin_only
    def test_each_accessibility_row_names_its_subject(self):
        from security.macos_permissions import (
            check_accessibility, check_accessibility_osascript,
        )

        py = check_accessibility()
        osa = check_accessibility_osascript()
        assert py.subject and "python-host" in py.subject
        assert osa.subject and "osascript" in osa.subject
        assert py.subject != osa.subject
        assert py.to_dict()["subject"] == py.subject

    @darwin_only
    def test_disabling_the_live_probe_yields_unknown_never_granted(
        self, monkeypatch,
    ):
        from security.macos_permissions import check_accessibility_osascript

        monkeypatch.setenv("FERAL_TCC_OSASCRIPT_PROBE", "0")
        status = check_accessibility_osascript()
        assert status.status == "unknown"
        assert "cannot be inferred" in (status.error or "")

    @darwin_only
    def test_osascript_probe_returns_a_real_verdict(self):
        from security.macos_permissions import check_accessibility_osascript

        status = check_accessibility_osascript()
        assert status.status in {"granted", "denied", "unknown"}
        # The probe must actually have shelled out, not guessed from the
        # Python process's AX state.
        assert status.api == "osascript AX probe"


class TestAccessibilityDenialPatterns:
    """``skills/desktop_control/applescript.py`` had no -25211 pattern, so
    an Accessibility denial surfaced as a raw 500 with opaque stderr."""

    @pytest.mark.parametrize("stderr", [
        "System Events got an error: osascript is not allowed assistive "
        "access. (-25211)",
        "System Events got an error: FERAL is not allowed to send "
        "keystrokes. (-25211)",
        "execution error: errAXAPIDisabled",
    ])
    def test_denial_strings_are_classified(self, stderr):
        from skills.desktop_control.applescript import _is_accessibility_denial

        assert _is_accessibility_denial(stderr) is True

    @pytest.mark.parametrize("stderr", [
        "Not authorized to send Apple events to Music.",
        "execution error: Can't get window 1. (-1728)",
        "",
    ])
    def test_unrelated_errors_are_not_misclassified(self, stderr):
        from skills.desktop_control.applescript import _is_accessibility_denial

        assert _is_accessibility_denial(stderr) is False

    def test_envelope_prefers_the_accessibility_card(self):
        from skills.desktop_control.applescript import AppleScriptResult

        env = AppleScriptResult(
            success=False, stdout="", exit_code=1, duration_ms=1,
            stderr="osascript is not allowed assistive access. (-25211)",
            tcc_permission="accessibility",
        ).to_envelope(action="window_list")
        assert env["status_code"] == 403
        assert env["error"] == "tcc_denied:accessibility"

    def test_automation_denial_still_maps_to_the_bundle(self):
        from skills.desktop_control.applescript import AppleScriptResult

        env = AppleScriptResult(
            success=False, stdout="", exit_code=1, duration_ms=1,
            stderr="Not authorized to send Apple events to Music.",
            tcc_target_bundle="com.apple.Music",
        ).to_envelope(action="play")
        assert env["error"] == "tcc_denied:automation:com.apple.Music"


# ── run_osascript contract ───────────────────────────────────────


class TestRunOsascript:
    @darwin_only
    def test_reports_failure_instead_of_silent_empty_stdout(self):
        """The exact class of bug being fixed: a failing script produces
        empty stdout, and the old code called that "no windows"."""
        res = run_osascript("this is not valid applescript at all")
        assert res.ok is False
        assert res.returncode != 0
        assert res.stderr

    @darwin_only
    def test_success_carries_stdout(self):
        res = run_osascript("return 6 * 7")
        assert res.ok is True
        assert res.stdout.strip() == "42"

    def test_missing_binary_is_reported(self, monkeypatch):
        import skills.impl.gui_computer_use as gui

        def _boom(*a, **k):
            raise FileNotFoundError

        monkeypatch.setattr(gui.subprocess, "run", _boom)
        res = run_osascript("return 1")
        assert res.ok is False
        assert "not found" in res.stderr

    def test_timeout_is_reported(self, monkeypatch):
        import skills.impl.gui_computer_use as gui

        def _boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="osascript", timeout=1)

        monkeypatch.setattr(gui.subprocess, "run", _boom)
        res = run_osascript("delay 100", timeout_s=1)
        assert res.ok is False
        assert "timed out" in res.stderr


# ── 3. Duplicate control surface ─────────────────────────────────


class TestDuplicateSurface:
    """Both skills are registered and both are reachable. The invariant
    is that they cannot *diverge*: desktop_automation must delegate."""

    def test_both_skills_register(self):
        from skills.impl import get_implementation
        import skills.impl.desktop_automation  # noqa: F401
        import skills.impl.gui_computer_use  # noqa: F401

        assert get_implementation("gui_computer_use") is not None
        assert get_implementation("desktop_automation") is not None

    def test_overlapping_endpoints_are_documented(self):
        """Names the exact overlap so a future addition to either
        manifest has to acknowledge the other."""
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "skills" / "manifests"
        gui = json.loads((root / "gui_computer_use.json").read_text())
        desk = json.loads((root / "desktop_automation.json").read_text())
        gui_ids = {e["id"] for e in gui["endpoints"]}
        desk_ids = {e["id"] for e in desk["endpoints"]}

        # desktop_automation is a strict subset of gui_computer_use's
        # capability set, modulo endpoint naming.
        alias = {
            "click_screen": "mouse_click",
            "double_click": "mouse_double_click",
            "right_click": "mouse_right_click",
            "move_mouse": "mouse_move",
            "type_text": "type_text",
            "key_combo": "key_press",
            "scroll": "scroll",
            "get_cursor_position": "cursor_position",
        }
        assert desk_ids == set(alias)
        assert set(alias.values()) <= gui_ids

    def test_desktop_automation_delegates_to_gui_computer_use(self):
        """A click through the shim must reach the DPI-scaled path.

        Not a style point: the shim's original implementation used raw,
        unscaled pyautogui, so on a Retina Mac the same request landed at
        half the intended coordinates depending on which of the two
        skills the router picked.
        """
        from skills.impl import get_implementation

        gui = get_implementation("gui_computer_use")
        seen: list[tuple] = []

        async def _fake_execute(endpoint_id, args, vault):
            seen.append((endpoint_id, dict(args)))
            return {"success": True, "status_code": 200, "data": {}, "error": None}

        original = gui.execute
        gui.execute = _fake_execute
        try:
            shim = get_implementation("desktop_automation")
            _run(shim.execute("click_screen", {"x": 200, "y": 300}, {}))
            _run(shim.execute("key_combo", {"keys": "cmd+c"}, {}))
            _run(shim.execute("get_cursor_position", {}, {}))
        finally:
            gui.execute = original

        endpoints = [e for e, _ in seen]
        assert endpoints == ["mouse_click", "key_press", "cursor_position"]
        assert seen[0][1]["x"] == 200 and seen[0][1]["y"] == 300

    def test_unknown_endpoint_is_a_404_on_both(self):
        from skills.impl import get_implementation

        for skill_id in ("gui_computer_use", "desktop_automation"):
            impl = get_implementation(skill_id)
            result = _run(impl.execute("no_such_endpoint", {}, {}))
            assert result["success"] is False
            assert result["status_code"] == 404


# ── Routing: which skill does a natural-language task reach? ─────


_ROUTING_CASES = [
    ("take a screenshot of my screen", "gui_computer_use"),
    ("what windows do I have open", "gui_computer_use"),
    ("list windows", "gui_computer_use"),
    ("what apps are running on my mac", "desktop_control"),
    ("run pytest in the repo", "coding_tools"),
    ("read the file skills/registry.py", "coding_tools"),
    ("search the codebase for TODO comments", "coding_tools"),
    ("search the web for the weather in Berlin", "web_search"),
]


class TestNaturalLanguageRouting:
    """The registry is a keyword scorer, so routing is deterministic and
    testable without an LLM. These assertions pin the surface each task
    reaches today; a manifest edit that steals one of them fails here.
    """

    @staticmethod
    def _registry():
        from pathlib import Path

        from skills.registry import SkillRegistry

        reg = SkillRegistry()
        reg.load_from_directory(
            Path(__file__).resolve().parents[1] / "skills" / "manifests"
        )
        return reg

    @pytest.mark.parametrize("query,expected", _ROUTING_CASES)
    def test_expected_skill_is_a_top_candidate(self, query, expected):
        reg = self._registry()
        top = [s.skill_id for s in reg.find_skills_for_query(query, top_k=5)]
        assert expected in top, f"{query!r} routed to {top}"

    # ``test_gui_computer_use_is_not_pinned_but_its_shim_is`` lived here.
    # It documented the routing hazard (the deprecated 8-endpoint shim
    # pinned in ``ALWAYS_INCLUDE_SKILLS`` while the canonical
    # 11-endpoint surface was not) and said to delete it if the
    # situation ever flipped. It has: ``gui_computer_use`` is pinned now
    # and the nine duplicated trigger phrases are gone from the shim.
    # The replacement assertions live in
    # ``tests/test_desktop_surface_and_process_routing.py``.
