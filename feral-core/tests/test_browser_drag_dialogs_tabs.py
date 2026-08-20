"""Drag, native dialogs, per-tab isolation, and the FERAL_HOME fix.

Four gaps in ``skills/impl/browser_use.py``, all of which shared one
property: the browser reported success while doing the wrong thing, or
nothing at all.

* **Drag** did not exist. Reordering a list, moving a card, setting a
  slider and dropping on a drop zone are one gesture that click and hover
  cannot express.
* **Native dialogs** were never answered on the CDP path. ``alert()``,
  ``confirm()``, ``prompt()`` and ``beforeunload`` block the renderer
  until a client answers, so an unanswered dialog made the tab
  indistinguishable from a hung one and every later ``Runtime.evaluate``
  simply never returned.
* **switch_tab moved the browser but not the controller.** ``self._page``
  and ``self._cdp`` stayed on the original tab, so every endpoint after a
  switch acted on the tab the user was no longer looking at and reported
  success. Wrong-target-but-successful is the worst failure a driver can
  have, because nothing above it can tell.
* **save_cookies ignored FERAL_HOME.** It built ``Path.home()/".feral"``
  by hand, so an isolated run wrote live session cookies into the
  operator's real home directory.

These tests use fakes, not a browser: they pin the contracts that the
end-to-end run against Chrome 151 exercised, so a regression is caught
without one.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.impl.browser_use import (  # noqa: E402
    DEFAULT_DIALOG_POLICY,
    DIALOG_POLICIES,
    BrowserController,
    CDPConnection,
    feral_browser_root,
)

MANIFEST = json.loads(
    (ROOT / "skills" / "manifests" / "browser_use.json").read_text(encoding="utf-8")
)
ENDPOINTS = {e["id"]: e for e in MANIFEST["endpoints"]}


class FakeCDP:
    """Records every CDP command and answers from a scripted table."""

    def __init__(self, responses: dict | None = None, connected: bool = True):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}
        self.connected = connected
        self.is_page_target = True
        self.listeners: list = []

    async def send_command(self, method, params=None, timeout=30.0):
        self.calls.append((method, dict(params or {})))
        value = self.responses.get(method)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(params or {})
        return value if value is not None else {}

    def add_event_listener(self, listener):
        self.listeners.append(listener)

    async def disconnect(self):
        self.connected = False


def run(coro):
    return asyncio.run(coro)


# ── Gap 4: FERAL_HOME ────────────────────────────────────────────────


class TestEverythingWritesUnderFeralHome:
    def test_no_hand_rolled_home_path_survives_in_the_module(self) -> None:
        """One stray ``Path.home()`` is enough to leak into the real home.

        ``_recordings_root`` already used the resolver while
        ``_artifacts_root`` and ``save_cookies`` did not, in the same
        class, which is how the split went unnoticed.
        """
        import ast

        path = ROOT / "skills" / "impl" / "browser_use.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = [
            f"line {node.lineno}: Path.home()"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "home"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "Path"
        ]
        assert not offenders, (
            "browser_use.py must reach the data home through "
            "config.loader.feral_data_home (via feral_browser_root), never "
            "Path.home():\n  " + "\n  ".join(offenders)
        )

    def test_root_follows_feral_home(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / "relocated"))
        assert feral_browser_root() == tmp_path / "relocated" / "browser"

    @pytest.mark.parametrize(
        "attr,leaf",
        [("_artifacts_root", "artifacts"), ("_recordings_root", "recordings")],
    )
    def test_directory_properties_follow_feral_home(
        self, attr, leaf, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
        got = getattr(BrowserController(), attr)
        assert got == tmp_path / "home" / "browser" / leaf
        assert got.is_dir()

    def test_save_cookies_writes_under_feral_home(self, tmp_path, monkeypatch) -> None:
        """The exact regression: an isolated run wrote to the real ~/.feral."""
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
        controller = BrowserController()
        controller._cdp = FakeCDP(
            {"Network.getAllCookies": {"cookies": [{"name": "a", "value": "b"}]}}
        )
        result = run(controller.save_cookies("acme"))
        expected = tmp_path / "home" / "browser" / "cookies" / "acme.json"
        assert result["success"] is True
        assert result["path"] == str(expected)
        assert json.loads(expected.read_text())[0]["name"] == "a"
        assert not (Path.home() / ".feral" / "browser" / "cookies" / "acme.json").exists()

    def test_restore_cookies_reads_the_same_place(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
        controller = BrowserController()
        controller._cdp = FakeCDP({"Network.getAllCookies": {"cookies": [{"n": 1}]}})
        run(controller.save_cookies("acme"))
        assert run(controller.restore_cookies("acme"))["count"] == 1

    def test_download_path_defaults_under_feral_home(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
        controller = BrowserController()
        controller._cdp = FakeCDP()
        result = run(controller.set_download_path(""))
        assert result["download_path"] == str(
            tmp_path / "home" / "browser" / "downloads"
        )


# ── Gap 1: drag ──────────────────────────────────────────────────────


class TestDragRefusesRatherThanGuessing:
    @pytest.mark.parametrize(
        "args,expected",
        [
            ({"to_ref_or_selector": "#drop"}, "from_ref_or_selector"),
            ({"from_ref_or_selector": "#card"}, "to_ref_or_selector"),
            ({"from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4, "mode": "html5"},
             "targets elements, not points"),
            ({"from_ref_or_selector": "a", "to_ref_or_selector": "b", "mode": "sideways"},
             "mode must be"),
        ],
    )
    def test_impossible_requests_are_named_not_attempted(self, args, expected) -> None:
        result = run(BrowserController().drag(**args))
        assert result["success"] is False
        assert expected in result["error"]

    def test_html5_without_playwright_says_which_driver_is_missing(self) -> None:
        """Chrome does not synthesise dragstart from CDP mouse events.

        Silently doing a mouse drag instead would report success on a
        native drop zone while nothing moved.
        """
        controller = BrowserController()
        controller._page = None
        result = run(controller.drag(
            from_ref_or_selector="#a", to_ref_or_selector="#b", mode="html5",
        ))
        assert result["success"] is False
        assert "Playwright" in result["error"]
        assert "mode=mouse" in result["error"]

    def test_unlocatable_endpoint_is_an_error_not_a_drag_to_origin(self) -> None:
        """A drag to (0, 0) looks like a drag that did the wrong thing."""
        controller = BrowserController()
        controller._page = None
        controller._cdp = FakeCDP({"Runtime.evaluate": {"result": {"value": None}}})
        result = run(controller.drag(
            from_ref_or_selector="#gone", to_x=10, to_y=10, mode="mouse",
        ))
        assert result["success"] is False
        assert "#gone" in result["error"]


class TestDragProducesARealGesture:
    def _drag(self, **kwargs) -> list[tuple[str, dict]]:
        controller = BrowserController()
        controller._page = None
        controller._cdp = FakeCDP()
        run(controller.drag(
            from_x=0, from_y=0, to_x=100, to_y=50, mode="mouse",
            hold_ms=0, settle_ms=0, **kwargs,
        ))
        return [c for c in controller._cdp.calls if c[0] == "Input.dispatchMouseEvent"]

    def test_press_then_moves_then_release(self) -> None:
        events = self._drag(steps=5)
        types = [p["type"] for _m, p in events]
        assert types[0] == "mouseMoved"
        assert types[1] == "mousePressed"
        assert types[-1] == "mouseReleased"
        assert types.count("mouseMoved") >= 6, types

    def test_intermediate_moves_exist(self) -> None:
        """A single jump from A to B is ignored by most drag code.

        The library sees a press and a release in different places with
        nothing between them and treats it as a click on the source.
        """
        events = self._drag(steps=8)
        xs = [p["x"] for _m, p in events]
        between = [x for x in xs if 0 < x < 100]
        assert len(between) >= 7, xs

    def test_moves_carry_the_held_button_bitmask(self) -> None:
        """``buttons: 1`` is load-bearing.

        Without it Chrome delivers the moves with no button held, so any
        handler checking ``event.buttons`` never starts the drag.
        """
        events = self._drag(steps=4)
        moves = [p for _m, p in events if p["type"] == "mouseMoved"]
        held = [p for p in moves if p.get("buttons") == 1]
        assert len(held) >= 4, moves
        assert moves[0].get("buttons") == 0, "the approach move must not hold a button"

    def test_final_position_is_the_target(self) -> None:
        events = self._drag(steps=3)
        _method, last = events[-1]
        assert (last["x"], last["y"]) == (100.0, 50.0)

    def test_steps_are_clamped_to_something_a_page_can_follow(self) -> None:
        assert len([e for e in self._drag(steps=0) if e[1]["type"] == "mouseMoved"]) >= 3
        assert len(self._drag(steps=10_000)) <= 105


# ── Gap 2: native dialogs ────────────────────────────────────────────


class TestDialogPolicy:
    def test_the_default_is_dismiss_not_accept(self) -> None:
        """Auto-accepting is dangerous: "Delete account?" is a confirm().

        Dismiss is safe for every type: alert ignores the distinction,
        confirm reads as Cancel, prompt as Cancel, and beforeunload as
        stay-on-the-page, which cancels a navigation rather than throwing
        away the user's work.
        """
        assert DEFAULT_DIALOG_POLICY == "dismiss"
        assert BrowserController()._dialog_policy == "dismiss"

    def test_accept_is_reachable_but_flagged_as_dangerous(self) -> None:
        result = run(BrowserController().set_dialog_policy(policy="accept"))
        assert result["success"] is True
        assert "DANGEROUS" in result["note"]

    def test_manual_says_the_page_stays_blocked(self) -> None:
        result = run(BrowserController().set_dialog_policy(
            policy="manual", manual_timeout_s=5,
        ))
        assert result["manual_timeout_s"] == 5
        assert "BLOCKED" in result["note"]

    def test_unknown_policy_is_refused_and_lists_the_real_ones(self) -> None:
        result = run(BrowserController().set_dialog_policy(policy="whatever"))
        assert result["success"] is False
        for policy in DIALOG_POLICIES:
            assert policy in result["error"]

    def test_a_bad_timeout_falls_back_instead_of_raising(self) -> None:
        result = run(BrowserController().set_dialog_policy(
            policy="manual", manual_timeout_s="soon",
        ))
        assert result["success"] is True
        assert result["manual_timeout_s"] > 0


class TestDialogsAreAnswered:
    def _opened(self, controller, dtype="confirm", message="Delete account?"):
        return controller._on_cdp_dialog_event({
            "method": "Page.javascriptDialogOpening",
            "params": {"type": dtype, "message": message, "url": "https://x.test"},
        })

    def test_a_dialog_is_answered_and_the_page_unblocked(self) -> None:
        controller = BrowserController()
        controller._cdp = FakeCDP()
        run(self._opened(controller))
        answered = [c for c in controller._cdp.calls
                    if c[0] == "Page.handleJavaScriptDialog"]
        assert answered, "nothing answered the dialog; the renderer stays blocked"
        assert answered[0][1] == {"accept": False}
        assert controller._pending_dialog is None

    def test_prompt_text_is_only_sent_for_a_prompt(self) -> None:
        """An explicit promptText on a beforeunload made Chrome 151 never
        answer, so page.goto sat out its whole timeout with the tab
        blocked."""
        controller = BrowserController()
        controller._cdp = FakeCDP()
        controller._dialog_policy = "accept"
        run(self._opened(controller, dtype="beforeunload", message=""))
        _method, params = [c for c in controller._cdp.calls
                           if c[0] == "Page.handleJavaScriptDialog"][0]
        assert params == {"accept": True}, params

    def test_prompt_text_is_sent_for_a_prompt(self) -> None:
        controller = BrowserController()
        controller._cdp = FakeCDP()
        run(controller.set_dialog_policy(policy="accept", prompt_text="hello"))
        run(self._opened(controller, dtype="prompt", message="name?"))
        _method, params = [c for c in controller._cdp.calls
                           if c[0] == "Page.handleJavaScriptDialog"][0]
        assert params == {"accept": True, "promptText": "hello"}

    def test_manual_parks_the_dialog_for_handle_dialog(self) -> None:
        controller = BrowserController()
        controller._cdp = FakeCDP()
        controller._dialog_policy = "manual"
        controller._dialog_manual_timeout_s = 600

        async def scenario():
            await self._opened(controller)
            assert controller._pending_dialog is not None
            assert not [c for c in controller._cdp.calls
                        if c[0] == "Page.handleJavaScriptDialog"]
            out = await controller.handle_dialog(action="accept")
            for task in list(controller._bg_tasks):
                task.cancel()
            return out

        result = run(scenario())
        assert result["success"] is True
        assert result["dialog"]["action"] == "accept"
        assert result["dialog"]["handled_by"] == "manual"

    def test_the_model_is_told_a_dialog_happened(self) -> None:
        """A cancelled confirm() means the action behind it did not run,
        and nothing in the page text says so."""
        controller = BrowserController()
        controller._cdp = FakeCDP()

        async def scenario():
            await self._opened(controller)
            return await controller._with_dialog_events({"success": True})

        result = run(scenario())
        assert result["dialog_events"][0]["message"] == "Delete account?"
        assert result["dialog_events"][0]["action"] == "dismiss"
        assert result["dialog_policy"] == "dismiss"

    def test_each_dialog_is_reported_exactly_once(self) -> None:
        controller = BrowserController()
        controller._cdp = FakeCDP()

        async def scenario():
            await self._opened(controller)
            first = await controller._with_dialog_events({"success": True})
            second = await controller._with_dialog_events({"success": True})
            return first, second

        first, second = run(scenario())
        assert len(first["dialog_events"]) == 1
        assert "dialog_events" not in second

    def test_a_dismissed_beforeunload_explains_the_failed_navigation(self) -> None:
        controller = BrowserController()
        controller._cdp = FakeCDP()

        async def scenario():
            await self._opened(controller, dtype="beforeunload", message="")
            return await controller._with_dialog_events(
                {"success": False, "error": "net::ERR_ABORTED"}
            )

        result = run(scenario())
        assert "CANCELS the navigation" in result["dialog_hint"]

    def test_the_same_dialog_delivered_twice_is_answered_once(self) -> None:
        """Chrome delivers the open event to every attached CDP client."""
        controller = BrowserController()
        controller._cdp = FakeCDP()
        controller._dialog_policy = "manual"
        controller._dialog_manual_timeout_s = 600

        async def scenario():
            await self._opened(controller)
            await self._opened(controller)
            for task in list(controller._bg_tasks):
                task.cancel()

        run(scenario())
        assert len(controller._dialog_log) == 1

    def test_a_second_real_dialog_is_not_swallowed_as_an_echo(self) -> None:
        """The regression a time-window dedupe caused.

        A page that raised the same beforeunload on two navigation
        attempts a few hundred milliseconds apart had the second one
        classified as an echo, so nothing answered it and the tab hung
        with the modal up.
        """
        controller = BrowserController()
        controller._cdp = FakeCDP()

        async def scenario():
            await self._opened(controller, dtype="beforeunload", message="")
            await self._opened(controller, dtype="beforeunload", message="")

        run(scenario())
        assert len(controller._dialog_log) == 2
        answered = [c for c in controller._cdp.calls
                    if c[0] == "Page.handleJavaScriptDialog"]
        assert len(answered) == 2


class TestClearingADialogNobodySaw:
    def test_handle_dialog_clears_blind_when_nothing_is_tracked(self) -> None:
        """A dialog already on screen when FERAL attached fired its open
        event before anyone was listening. Refusing here would leave the
        agent staring at a tab that answers nothing."""
        controller = BrowserController()
        controller._cdp = FakeCDP()
        result = run(controller.handle_dialog(action="dismiss"))
        assert result["success"] is True
        assert result["untracked"] is True
        assert ("Page.handleJavaScriptDialog", {"accept": False}) in controller._cdp.calls

    def test_no_dialog_at_all_is_reported_honestly(self) -> None:
        controller = BrowserController()
        controller._cdp = FakeCDP({
            "Page.handleJavaScriptDialog": Exception("No dialog is showing"),
        })
        result = run(controller.handle_dialog(action="dismiss"))
        assert result["success"] is False
        assert result["no_dialog_open"] is True

    def test_a_blocked_renderer_is_surfaced_with_the_remedy(self) -> None:
        """A blocked page and a hung page look identical from outside."""
        controller = BrowserController()
        controller._cdp = FakeCDP({
            "Runtime.evaluate": asyncio.TimeoutError(),
        })
        result = run(controller.get_dialogs())
        assert result["page_blocked"] is True
        assert "handle_dialog" in result["blocked_hint"]

    def test_bad_action_is_refused(self) -> None:
        result = run(BrowserController().handle_dialog(action="maybe"))
        assert result["success"] is False
        assert "accept" in result["error"]


# ── Gap 3: per-tab isolation ─────────────────────────────────────────


class TestTargetIdentity:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("ws://localhost:9222/devtools/page/ABC123", "ABC123"),
            ("ws://localhost:9222/devtools/browser/XYZ", ""),
            ("", ""),
        ],
    )
    def test_target_id_is_read_off_the_socket_url(self, url, expected) -> None:
        conn = CDPConnection()
        conn._page_ws_url = url
        assert conn.target_id == expected


class TestSwitchTabActuallySwitches:
    def test_attach_moves_both_halves_and_invalidates_refs(self, monkeypatch) -> None:
        """ARIA refs are backend node ids in the OLD document.

        Reusing one after a switch resolves to nothing, or worse to a
        coincidentally valid node in the new page.
        """
        controller = BrowserController()
        old = FakeCDP()
        controller._cdp = old
        controller._attached_target_id = "TAB-OLD"
        controller._aria_refs["ax0"] = {"backend_id": 7, "selector": "#stale"}

        fresh = FakeCDP()

        class _Fresh(CDPConnection):
            async def connect(self, prefer_page=False, target_id=""):
                self._page_ws_url = f"ws://h/devtools/page/{target_id}"
                return True

        made = {}

        def _factory(host=None, port=None):
            conn = _Fresh(host=host, port=port)
            made["conn"] = conn
            return conn

        monkeypatch.setattr(
            "skills.impl.browser_use.CDPConnection", _factory,
        )
        result = run(controller.attach_to_tab("TAB-NEW"))
        assert result["success"] is True
        assert result["aria_refs_invalidated"] is True
        assert controller._attached_target_id == "TAB-NEW"
        assert controller._cdp is made["conn"]
        assert controller._aria_refs == {}
        assert old.connected is False, "the old tab's socket must be released"
        assert fresh is not controller._cdp

    def test_switch_refuses_while_a_recording_shares_the_socket(self) -> None:
        controller = BrowserController()
        controller._recording = {"recording_id": "demo", "owns_cdp": False}
        result = run(controller.attach_to_tab("TAB-2"))
        assert result["success"] is False
        assert "stop_recording" in result["error"]

    def test_switch_refuses_while_har_owns_the_page(self) -> None:
        controller = BrowserController()
        controller._har_active = True
        result = run(controller.attach_to_tab("TAB-2"))
        assert result["success"] is False
        assert "har_stop" in result["error"]

    def test_empty_tab_id_is_refused_with_a_pointer_to_list_tabs(self) -> None:
        result = run(BrowserController().switch_tab(""))
        assert result["success"] is False
        assert "list_tabs" in result["error"]

    def test_switch_tab_returns_a_dict_the_memory_hook_can_read(self) -> None:
        """A bare bool bypassed api.state's per-domain capture entirely."""
        result = run(BrowserController().switch_tab(""))
        assert isinstance(result, dict)
        assert result["success"] is False


class TestCloseTabDoesNotStrandTheController:
    def test_closing_a_tab_needs_an_id(self) -> None:
        result = run(BrowserController().close_tab(""))
        assert result["success"] is False
        assert "list_tabs" in result["error"]


# ── Snapshot: filter, pagination, coordinates, ambiguity ─────────────


def _ax_nodes(count: int, role: str = "button") -> list[dict]:
    return [
        {
            "role": {"value": role},
            "name": {"value": f"item {i}"},
            "depth": 0,
            "backendDOMNodeId": 1000 + i,
            "nodeId": i,
        }
        for i in range(count)
    ]


class TestSnapshotStopsHidingElements:
    """The old code cut the tree at a hard ``nodes[:500]`` head slice.

    No filter, no offset, and no signal that anything had been dropped,
    so on a real app the elements the agent needed fell off the end and
    were unreachable, with the call still reporting success.
    """

    def _snapshot(self, nodes, **kwargs):
        controller = BrowserController()
        controller._cdp = FakeCDP({"Accessibility.getFullAXTree": {"nodes": nodes}})
        return controller, run(controller.snapshot(
            include_coordinates=False, **kwargs
        ))

    def test_truncation_is_announced_in_the_text_the_model_reads(self) -> None:
        _c, result = self._snapshot(_ax_nodes(600), max_nodes=100)
        assert result["truncated"] is True
        assert result["total_matched"] == 600
        assert result["next_offset"] == 100
        assert "TRUNCATED" in result["aria_tree"]
        assert "offset=100" in result["aria_tree"]

    def test_the_tail_of_a_big_page_is_reachable(self) -> None:
        """Element 599 was unreachable under the old 500-node head cut."""
        _c, result = self._snapshot(_ax_nodes(600), max_nodes=50, offset=550)
        assert 'item 599' in result["aria_tree"]
        assert result["truncated"] is False

    def test_refs_are_numbered_absolutely_across_pages(self) -> None:
        """Two different elements answering to ax0 in one page load is a
        trap, not a convenience."""
        _c, page2 = self._snapshot(_ax_nodes(600), max_nodes=10, offset=10)
        assert "[ax10]" in page2["aria_tree"]
        assert "[ax0]" not in page2["aria_tree"]

    def test_interactive_is_the_default_and_all_is_wider(self) -> None:
        nodes = _ax_nodes(5, role="button") + _ax_nodes(40, role="StaticText")
        _c, interactive = self._snapshot(nodes)
        _c2, everything = self._snapshot(nodes, filter="all")
        assert interactive["total_matched"] == 5
        assert everything["total_matched"] == 45
        assert "filter=interactive" in interactive["aria_tree"]

    def test_noise_roles_never_appear_under_either_filter(self) -> None:
        _c, result = self._snapshot(_ax_nodes(30, role="generic"), filter="all")
        assert result["total_matched"] == 0

    def test_an_unknown_filter_is_refused(self) -> None:
        _c, result = self._snapshot(_ax_nodes(3), filter="everything")
        assert result["success"] is False
        assert "interactive" in result["error"]


class TestSelectorsAreVerifiedUnique:
    """``f"{tag}.{first_class}"`` with no uniqueness check meant
    ``div.row`` on a 40-row table was the selector for every row, and
    ``querySelector`` silently took the first. A wrong-element click
    looks exactly like a successful one."""

    def _resolve(self, in_page_value):
        controller = BrowserController()
        controller._aria_refs["ax0"] = {
            "backend_id": 42, "name": "Row", "role": "row", "selector": "",
        }
        controller._cdp = FakeCDP({
            "DOM.resolveNode": {"object": {"objectId": "obj-1"}},
            "Runtime.callFunctionOn": {"result": {"value": in_page_value}},
        })
        run(controller._resolve_aria_selectors(include_coordinates=True))
        return controller

    def test_a_unique_selector_and_its_coordinates_are_recorded(self) -> None:
        controller = self._resolve({
            "selector": "html > body > div:nth-of-type(4)", "unique": True,
            "match_count": 1, "x": 120.0, "y": 240.0, "width": 80, "height": 20,
            "visible": True,
        })
        info = controller._aria_refs["ax0"]
        assert info["selector_unique"] is True
        assert info["center"] == {"x": 120.0, "y": 240.0}

    def test_an_ambiguous_selector_is_flagged_everywhere_the_model_looks(self) -> None:
        controller = self._resolve({
            "selector": "div.row", "unique": False, "match_count": 40,
            "x": 10.0, "y": 20.0, "width": 5, "height": 5, "visible": True,
        })
        assert controller._aria_refs["ax0"]["selector_unique"] is False
        annotated = controller._annotate_aria_text('[ax0] row: "Row"')
        assert "!ambiguous" in annotated
        assert "@10,20" in annotated

    def test_the_describe_node_fallback_never_claims_uniqueness(self) -> None:
        """That path cannot verify, and unknown is not the same as unique."""
        controller = BrowserController()
        controller._aria_refs["ax0"] = {
            "backend_id": 42, "name": "Row", "selector": "",
        }
        controller._cdp = FakeCDP({
            "DOM.resolveNode": Exception("no Runtime domain"),
            "DOM.describeNode": {
                "node": {"localName": "div", "attributes": ["class", "row alt"]},
            },
        })
        run(controller._resolve_aria_selectors(include_coordinates=True))
        assert controller._aria_refs["ax0"]["selector"] == "div.row"
        assert controller._aria_refs["ax0"]["selector_unique"] is None

    def test_offscreen_refs_are_marked(self) -> None:
        controller = self._resolve({
            "selector": "#deep", "unique": True, "match_count": 1,
            "x": 5.0, "y": 9000.0, "width": 10, "height": 10, "visible": False,
        })
        assert "!offscreen" in controller._annotate_aria_text('[ax0] row: "Row"')


# ── Manifest honesty for the new surface ─────────────────────────────


class TestTheManifestTellsTheTruthAboutTheNewEndpoints:
    @pytest.mark.parametrize(
        "endpoint_id", ["drag", "dialog_policy", "handle_dialog", "get_dialogs"],
    )
    def test_the_new_endpoints_are_declared_and_routed(self, endpoint_id) -> None:
        assert endpoint_id in ENDPOINTS
        assert endpoint_id in BrowserController._DISPATCH

    def test_drag_names_the_playwright_precondition(self) -> None:
        text = ENDPOINTS["drag"]["description"]
        assert "Playwright" in text
        assert "html5" in text and "mouse" in text

    def test_drag_warns_that_success_is_not_proof_the_drop_landed(self) -> None:
        assert "verify" in ENDPOINTS["drag"]["description"].lower()

    def test_the_dialog_default_is_documented_where_the_model_reads_it(self) -> None:
        text = ENDPOINTS["dialog_policy"]["description"]
        assert "default policy is dismiss" in text
        assert "DANGEROUS" in text
        assert "confirm()" in text

    def test_handle_dialog_documents_the_blind_clear(self) -> None:
        text = ENDPOINTS["handle_dialog"]["description"]
        assert "already on screen" in text
        assert "no_dialog_open" in text

    def test_switch_tab_no_longer_documents_the_bug_as_a_limitation(self) -> None:
        """The manifest used to describe the wrong-tab behaviour as
        intended, which taught the model to work around a defect."""
        text = ENDPOINTS["switch_tab"]["description"]
        assert "move this controller onto it" in text
        assert "does NOT move" not in text
        assert "does NOT move" not in ENDPOINTS["new_tab"]["description"]

    def test_new_tab_documents_the_load_wait(self) -> None:
        assert "readyState" in ENDPOINTS["new_tab"]["description"]

    def test_session_endpoints_no_longer_promise_a_hardcoded_home(self) -> None:
        for endpoint_id in ("save_session", "restore_session"):
            text = ENDPOINTS[endpoint_id]["description"]
            assert "$FERAL_HOME/browser/cookies" in text

    @pytest.mark.parametrize(
        "endpoint_id", ["drag", "dialog_policy", "handle_dialog", "get_dialogs"],
    )
    def test_every_declared_param_is_forwarded(self, endpoint_id) -> None:
        import inspect

        source = inspect.getsource(BrowserController._DISPATCH[endpoint_id])
        for param in ENDPOINTS[endpoint_id]["params"]:
            assert f'"{param["name"]}"' in source, (
                f"{endpoint_id} declares {param['name']!r} but the dispatch entry "
                "never reads it, so the model's argument is dropped silently."
            )
