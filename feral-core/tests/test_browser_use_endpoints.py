"""The browser skill's declared surface must equal its routed surface.

Why this file exists
--------------------
``skills/impl/browser_use.py`` is a 2 000-line CDP driver: it launches
Chrome, screencasts frames, resolves ARIA refs to selectors, records HAR
and video. None of that is reachable by the model unless two things agree:

  1. a manifest DECLARES an endpoint id, and
  2. something ROUTES that id to a controller method.

Before this file, (1) came from a dict hardcoded in
``skills/impl/browser_use.py`` and (2) came from a 30-branch if/elif in
``api/state.py``. Nothing compared them. An id declared with no branch fell
through to ``method(**args)`` and raised TypeError at call time; an id with
no method at all returned a bare "Unknown browser action" string that the
model could only discover by making the call. Both are runtime 404s the
model pays for in a failed turn.

The three properties pinned here:

* **No declared-but-unrouted endpoint.** Every id in
  ``skills/manifests/browser_use.json`` is a key of
  ``BrowserController._DISPATCH`` and lands on a real coroutine method.
* **No routed-but-undeclared endpoint.** Code nothing can call is not a
  capability; it is dead weight that reads like one.
* **Descriptions do not lie.** This is the defect class the repo just
  fixed elsewhere: a manifest description that told the model to do
  something the policy then refused, without saying so. Every endpoint
  that a surface deny list blocks, or that needs Playwright or ffmpeg,
  must say so in the text the model actually reads.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.skill_manifest import SkillManifest  # noqa: E402
from skills.impl.browser_use import (  # noqa: E402
    BROWSER_ENDPOINT_ALIASES,
    BROWSER_MANIFEST_PATH,
    BrowserController,
    _fallback_browser_manifest,
    get_browser_skill_manifest,
)

MANIFEST_RAW = json.loads(BROWSER_MANIFEST_PATH.read_text(encoding="utf-8"))
MANIFEST = SkillManifest(**MANIFEST_RAW)
ENDPOINTS = {e.id: e for e in MANIFEST.endpoints}
DISPATCH = BrowserController._DISPATCH


class _Recorder:
    """Stands in for a BrowserController and records the call a lambda makes.

    The dispatch entries are ``lambda c, a: c.some_method(...)``, so passing
    one of these as ``c`` proves which method an endpoint targets without a
    browser, an event loop, or a mock library guessing at signatures.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __getattr__(self, name: str):
        def _capture(*args, **kwargs):
            self.calls.append((name, args, kwargs))

            async def _noop():
                return {"success": True}

            return _noop()

        return _capture


def _route(endpoint_id: str, args: dict) -> tuple:
    recorder = _Recorder()
    coro = DISPATCH[endpoint_id](recorder, args)
    coro.close()
    assert len(recorder.calls) == 1, (
        f"{endpoint_id} made {len(recorder.calls)} controller calls, expected 1"
    )
    return recorder.calls[0]


class TestManifestIsTheSourceOfTruth:
    def test_manifest_file_exists_and_validates(self) -> None:
        assert BROWSER_MANIFEST_PATH.is_file(), BROWSER_MANIFEST_PATH
        assert MANIFEST.skill_id == "browser"
        assert MANIFEST.endpoints

    def test_loader_returns_the_json_not_the_fallback(self) -> None:
        loaded = get_browser_skill_manifest()
        assert loaded["skill_id"] == "browser"
        assert len(loaded["endpoints"]) == len(MANIFEST_RAW["endpoints"])

    def test_fallback_manifest_is_a_strict_subset(self) -> None:
        """The in-code fallback must never advertise an id the JSON drops.

        The fallback runs on a stripped install with no manifests
        directory. If it ever declared an endpoint the real manifest does
        not, that install would offer the model a tool the normal install
        does not have.
        """
        fallback_ids = {e["id"] for e in _fallback_browser_manifest()["endpoints"]}
        assert fallback_ids <= set(ENDPOINTS), sorted(fallback_ids - set(ENDPOINTS))

    def test_browser_counts_as_a_first_party_skill(self) -> None:
        """Shipping the manifest is what makes the skill's declarations bind.

        ``security.safety_resolver`` ignores a ``safety_tier`` from a
        manifest that does not ship in this repo, and
        ``skills.result_budget`` clamps such a manifest's declared budget.
        Both trust checks read ``skills/manifests/*.json``, so without this
        file every safety_tier and result_budget below is inert.
        """
        from skills.result_budget import builtin_skill_ids

        builtin_skill_ids.__globals__["_builtin_skill_ids"] = None
        assert "browser" in builtin_skill_ids()


class TestEveryDeclaredEndpointRoutes:
    @pytest.mark.parametrize("endpoint_id", sorted(ENDPOINTS))
    def test_declared_endpoint_is_dispatched(self, endpoint_id: str) -> None:
        assert endpoint_id in DISPATCH, (
            f"Manifest declares {endpoint_id!r} but BrowserController.execute "
            "does not route it, so the model would get a runtime 404."
        )

    @pytest.mark.parametrize("endpoint_id", sorted(DISPATCH))
    def test_dispatched_endpoint_is_declared(self, endpoint_id: str) -> None:
        assert endpoint_id in ENDPOINTS, (
            f"execute() routes {endpoint_id!r} but no manifest declares it, so "
            "nothing can ever call it."
        )

    @pytest.mark.parametrize("endpoint_id", sorted(ENDPOINTS))
    def test_endpoint_lands_on_a_real_coroutine_method(self, endpoint_id: str) -> None:
        name, _args, _kwargs = _route(endpoint_id, {})
        method = getattr(BrowserController, name, None)
        assert method is not None, (
            f"{endpoint_id} routes to BrowserController.{name}, which does not exist."
        )
        assert inspect.iscoroutinefunction(method), (
            f"BrowserController.{name} is not awaitable; execute() awaits it."
        )

    @pytest.mark.parametrize("endpoint_id", sorted(ENDPOINTS))
    def test_declared_params_are_actually_forwarded(self, endpoint_id: str) -> None:
        """A declared param the dispatch never reads is a silent dropped arg.

        That is exactly the ``navigate``/``wait_until`` bug this repo already
        fixed once: the manifest advertised the argument, the model passed
        it, and the dispatch dropped it on the floor with no error.
        """
        source = inspect.getsource(DISPATCH[endpoint_id])
        for param in ENDPOINTS[endpoint_id].params:
            assert f'"{param.name}"' in source, (
                f"{endpoint_id} declares param {param.name!r} but its dispatch "
                f"entry never reads it:\n{source}"
            )

    def test_unknown_endpoint_returns_a_named_error(self) -> None:
        """Never a raise, never a silent success, and it names the id."""
        import asyncio

        result = asyncio.run(BrowserController().execute("no_such_endpoint", {}))
        assert result["success"] is False
        assert "no_such_endpoint" in result["error"]

    def test_aliased_endpoints_reach_the_aliased_method(self) -> None:
        for endpoint_id, method_name in BROWSER_ENDPOINT_ALIASES.items():
            assert endpoint_id in DISPATCH, endpoint_id
            called, _a, _k = _route(endpoint_id, {})
            assert called == method_name, (
                f"{endpoint_id} should route to {method_name}, routed to {called}"
            )

    def test_brain_alias_mirror_matches_the_live_map(self) -> None:
        """api/state.py restates the alias map; it must not drift.

        The literals stay in api/state.py because it is the documented
        agent-visible surface of BrainState and because
        tests/test_brain_browser_alias_map.py reads them out of that source.
        This test is what stops the copy from going stale.
        """
        from api.state import BrainState

        assert BrainState._BROWSER_ENDPOINT_ALIASES == BROWSER_ENDPOINT_ALIASES


class TestArgumentMarshalling:
    def test_navigate_forwards_wait_until(self) -> None:
        name, args, kwargs = _route(
            "navigate", {"url": "https://example.com", "wait_until": "networkidle"},
        )
        assert name == "navigate"
        assert args[0] == "https://example.com"
        assert kwargs["wait_until"] == "networkidle"

    def test_missing_optional_args_fall_back_to_documented_defaults(self) -> None:
        _n, args, kwargs = _route("scroll", {})
        assert args == ("down", 500)
        _n, _a, kwargs = _route("wait_for_selector", {"ref_or_selector": "#x"})
        assert kwargs["timeout_ms"] == 5000
        assert kwargs["state"] == "visible"

    def test_garbage_numeric_arg_does_not_crash_dispatch(self) -> None:
        """A model that sends "many" for an integer gets the default, not a 500."""
        _n, args, _k = _route("scroll", {"amount": "not-a-number"})
        assert args[1] == 500

    def test_string_booleans_are_coerced(self) -> None:
        _n, args, _k = _route("screenshot", {"full_page": "true"})
        assert args[0] is True
        _n, args, _k = _route("screenshot", {"full_page": "false"})
        assert args[0] is False


class TestSafetyDeclarationsAreHonest:
    READ_ONLY_EXPECTED = {
        "get_page_info", "get_page_text", "snapshot", "find", "screenshot",
        "get_page_pdf", "list_tabs", "list_iframes", "get_console_logs",
        "network_log", "list_recordings", "get_dialogs",
    }
    MUTATING = {
        "navigate", "click", "click_at", "drag", "type_text", "type_keys",
        "press_key", "fill_form", "select_option", "upload_file", "evaluate",
        "execute_in_iframe", "new_tab", "close_tab", "save_session",
        "restore_session", "set_download_path", "download_next",
        "start_recording", "har_start", "trace_start",
        "dialog_policy", "handle_dialog",
    }

    @pytest.mark.parametrize("endpoint_id", sorted(ENDPOINTS))
    def test_every_endpoint_declares_a_tier(self, endpoint_id: str) -> None:
        assert ENDPOINTS[endpoint_id].safety_tier in ("safe", "confirm", "deny"), (
            f"{endpoint_id} declares no safety_tier, so policy falls back to a "
            "substring guess on its name."
        )

    def test_read_only_hints_match_the_reading_endpoints(self) -> None:
        declared = {e.id for e in MANIFEST.endpoints if e.read_only_hint}
        assert declared == self.READ_ONLY_EXPECTED, {
            "unexpectedly_read_only": sorted(declared - self.READ_ONLY_EXPECTED),
            "missing_read_only": sorted(self.READ_ONLY_EXPECTED - declared),
        }

    @pytest.mark.parametrize("endpoint_id", sorted(MUTATING))
    def test_mutating_endpoints_are_confirm_and_not_read_only(self, endpoint_id: str) -> None:
        endpoint = ENDPOINTS[endpoint_id]
        assert endpoint.safety_tier == "confirm", endpoint_id
        assert endpoint.read_only_hint is False, endpoint_id

    def test_reading_a_page_is_not_the_same_as_clicking_buy(self) -> None:
        assert ENDPOINTS["get_page_text"].safety_tier == "safe"
        assert ENDPOINTS["click"].safety_tier == "confirm"
        assert ENDPOINTS["upload_file"].requires_user_approval is True
        assert ENDPOINTS["evaluate"].requires_user_approval is True


class TestDescriptionsMatchThePolicy:
    """The manifest must not promise what a policy will refuse."""

    def test_evaluate_names_the_surfaces_that_hard_deny_it(self) -> None:
        from security.dangerous_tools import SURFACE_DENY_LISTS, is_tool_allowed

        text = ENDPOINTS["evaluate"].description
        denied = sorted(
            surface for surface in SURFACE_DENY_LISTS
            if not is_tool_allowed("browser__evaluate", surface)
        )
        assert denied, "browser__evaluate is no longer surface-denied anywhere"
        for surface in denied:
            assert surface in text, (
                f"browser__evaluate is hard-denied on {surface!r} but the "
                "endpoint description never says so, which is the manifest-"
                "lies-to-the-model defect."
            )

    def test_evaluate_really_is_denied_where_the_description_says(self) -> None:
        from security.safety_resolver import LEVEL_DENY, resolve_policy

        for surface in ("http_api", "mcp", "cron"):
            decision = resolve_policy("browser__evaluate", {}, surface=surface)
            assert decision.level == LEVEL_DENY, (surface, decision.level)
            assert decision.sources.get("surface_deny") is True

    def test_navigate_still_asks_before_it_runs(self) -> None:
        """Pinned: several suites assume browser__navigate needs approval."""
        from security.safety_resolver import LEVEL_CONFIRM, resolve_policy

        decision = resolve_policy("browser__navigate", {}, surface="websocket")
        assert decision.level == LEVEL_CONFIRM

    @pytest.mark.parametrize(
        "endpoint_id",
        ["trace_start", "har_start", "download_next", "execute_in_iframe"],
    )
    def test_playwright_only_endpoints_say_so(self, endpoint_id: str) -> None:
        assert "Playwright" in ENDPOINTS[endpoint_id].description, endpoint_id

    @pytest.mark.parametrize("endpoint_id", ["stop_recording", "assemble_recording"])
    def test_ffmpeg_dependent_endpoints_say_so(self, endpoint_id: str) -> None:
        assert "ffmpeg" in ENDPOINTS[endpoint_id].description, endpoint_id

    def test_skill_description_states_the_browser_precondition(self) -> None:
        text = MANIFEST.description
        assert "9222" in text
        assert "Cannot connect to Chrome" in text

    @pytest.mark.parametrize("endpoint_id", sorted(ENDPOINTS))
    def test_descriptions_are_substantive(self, endpoint_id: str) -> None:
        assert len(ENDPOINTS[endpoint_id].description) >= 80, endpoint_id


class TestTriggerPhrases:
    @pytest.mark.parametrize(
        "phrase",
        [
            "read this page", "click the login button", "fill in this form",
            "what's on this page", "scroll down", "open a new tab",
        ],
    )
    def test_real_user_sentences_are_covered(self, phrase: str) -> None:
        assert phrase in MANIFEST.trigger_phrases


class TestResultBudgets:
    def test_page_reading_endpoints_escape_the_2000_char_default(self) -> None:
        """The default tier truncates a result to 2 000 characters.

        A real page's ARIA tree and text are both far larger than that, so
        without an explicit tier the two endpoints whose entire purpose is
        returning page content would hand the model a fragment.
        """
        from skills.result_budget import budget_for

        for endpoint_id in ("get_page_text", "snapshot"):
            budget = budget_for("browser", endpoint_id, MANIFEST)
            assert budget.max_result_chars > 2_000, endpoint_id

    def test_untrusted_page_content_is_still_bounded(self) -> None:
        from skills.result_budget import budget_for

        budget = budget_for("browser", "get_page_text", MANIFEST)
        assert budget.max_result_chars <= 120_000


class TestRegressionsFoundByDrivingARealBrowser:
    """Three defects an end-to-end run against Chrome 14x exposed.

    All three were invisible to the existing unit tests because the test
    doubles answered the way the code hoped Chrome would, and all three
    made an endpoint report the wrong thing rather than crash.
    """

    def test_initialize_attaches_to_a_page_target(self) -> None:
        """CDP must attach to a TAB, not to the browser target.

        ``/json/version`` hands back the browser endpoint, which implements
        neither Page nor Runtime nor Accessibility nor Network. Against a
        real Chrome that made ``snapshot`` return success=False on every
        call (it has no Playwright fallback, so the ARIA tree the agent
        needs to address elements never worked), made
        ``get_console_logs`` always return zero entries, and made
        ``network_monitor_start`` raise "'Network.enable' wasn't found"
        out of the skill.
        """
        import asyncio

        seen: dict = {}

        class _FakeCDP:
            connected = False
            is_page_target = True

            async def connect(self, prefer_page: bool = False) -> bool:
                seen["prefer_page"] = prefer_page
                return False

            def add_event_listener(self, _listener):
                pass

        controller = BrowserController()
        controller._cdp = _FakeCDP()
        controller._auto_launch_chrome = lambda: _false()

        async def _false():
            return False

        asyncio.run(controller.initialize())
        assert seen.get("prefer_page") is True, (
            "initialize() connected to the browser target; every CDP-domain "
            "endpoint on this controller is dead in that mode."
        )

    def test_new_tab_uses_put_not_get(self) -> None:
        """Chrome M111+ answers ``GET /json/new`` with a 405 and a text body.

        The old GET failed on every current Chrome, and it failed as a JSON
        decode error swallowed into ``return None`` - so a rejected verb was
        indistinguishable from a browser that was not running.
        """
        import asyncio

        import skills.impl.browser_use as mod

        calls: list[tuple[str, str]] = []

        class _Resp:
            status = 200

            async def text(self):
                return json.dumps({"id": "TAB-1"})

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, url):
                calls.append(("GET", url))
                return _Resp()

            def put(self, url):
                calls.append(("PUT", url))
                return _Resp()

        class _FakeAiohttp:
            ClientSession = _Session

        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

        def _patched_import(name, *args, **kwargs):
            if name == "aiohttp":
                return _FakeAiohttp
            return real_import(name, *args, **kwargs)

        import builtins

        builtins.__import__ = _patched_import
        try:
            # activate=False: this test is about the HTTP verb, and a fresh
            # controller has no live CDP socket to move onto the new tab.
            result = asyncio.run(
                mod.BrowserController().new_tab("https://example.com", activate=False)
            )
        finally:
            builtins.__import__ = real_import

        assert result["success"] is True, result
        assert result["tab_id"] == "TAB-1"
        assert calls and calls[0][0] == "PUT", calls

    def test_go_back_trusts_the_url_not_the_response(self) -> None:
        """A bfcache restore fires no `load` and returns no Response.

        Against a real Chrome, ``page.go_back()`` raised
        "Timeout 15000ms exceeded ... navigated to https://example.com/" -
        the successful navigation was named inside the timeout error - and
        the endpoint reported failure for a Back that had plainly worked.
        """
        import asyncio

        class _FakePage:
            def __init__(self):
                self._url = "https://iana.org/"
                self.kwargs = {}

            @property
            def url(self):
                return self._url

            async def go_back(self, **kwargs):
                self.kwargs = kwargs
                self._url = "https://example.com/"
                raise TimeoutError("Timeout 15000ms exceeded")

            async def title(self):
                return "Example Domain"

        controller = BrowserController()
        page = _FakePage()
        controller._page = page
        result = asyncio.run(controller.go_back())
        assert result["success"] is True, result
        assert result["url"] == "https://example.com/"
        assert page.kwargs.get("wait_until") == "commit", (
            "go_back must not wait for `load`; a bfcache restore never fires it."
        )

    def test_go_back_still_fails_when_the_page_did_not_move(self) -> None:
        import asyncio

        class _StuckPage:
            url = "https://example.com/"

            async def go_back(self, **kwargs):
                raise TimeoutError("Timeout 15000ms exceeded")

        controller = BrowserController()
        controller._page = _StuckPage()
        result = asyncio.run(controller.go_back())
        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_a_raising_endpoint_becomes_a_result_not_an_exception(self) -> None:
        """enable_network_monitor talks straight to CDP and does not catch.

        A raw "'Network.enable' wasn't found" propagating out of the skill
        aborts the whole tool call; the model gets a stack trace instead of
        a result it can read and route around.
        """
        import asyncio

        class _Boom(BrowserController):
            async def enable_network_monitor(self):
                raise Exception("'Network.enable' wasn't found")

        result = asyncio.run(_Boom().execute("network_monitor_start", {}))
        assert result["success"] is False
        assert "Network.enable" in result["error"]
