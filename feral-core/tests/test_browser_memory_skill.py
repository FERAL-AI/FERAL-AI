"""Tests for the agent-facing site-memory skill and the browser recall/capture hook.

Two surfaces are covered:

* ``skills/impl/browser_memory.py`` + ``skills/manifests/browser_memory.json``
  — the endpoints the LLM can call directly.
* ``api.state.BrainState._execute_browser_action`` — the single hook that
  joins the CDP controller to the knowledge store. It is exercised against a
  stubbed dispatch so no Chrome is required, which is the point: the hook's
  behaviour must not depend on a live browser.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Skill surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def skill():
    from skills.impl.browser_memory import BrowserMemorySkill

    return BrowserMemorySkill()


def _manifest() -> dict:
    path = Path(__file__).resolve().parent.parent / "skills" / "manifests" / "browser_memory.json"
    return json.loads(path.read_text())


def test_manifest_is_valid_skill_manifest():
    from models.skill_manifest import SkillManifest

    manifest = SkillManifest(**_manifest())
    assert manifest.skill_id == "browser_memory"
    assert {e.id for e in manifest.endpoints} == {
        "recall", "remember", "search", "list_domains", "stats",
        "genesis_candidates", "forget",
    }


async def test_every_manifest_endpoint_is_implemented(skill):
    """A manifest endpoint with no dispatch arm is a tool that always errors."""
    for endpoint in _manifest()["endpoints"]:
        out = await skill.execute(endpoint["id"], {}, {})
        assert out["error"] != f"unknown endpoint: {endpoint['id']}", endpoint["id"]


async def test_unknown_endpoint_is_rejected(skill):
    out = await skill.execute("definitely_not_real", {}, {})
    assert out["success"] is False
    assert out["status_code"] == 404


async def test_remember_then_recall_round_trip(skill):
    written = await skill.execute(
        "remember",
        {
            "url": "https://admin.shopify.com/store/acme/products",
            "title": "Country picker is a listbox overlay",
            "body": "select_option fails. Click the trigger, re-snapshot, click by name.",
            "topic": "not_native_select",
            "tags": ["polaris"],
        },
        {},
    )
    assert written["success"] is True
    assert written["data"]["scope_key"] == "host:admin.shopify.com"

    read = await skill.execute(
        "recall", {"url": "https://admin.shopify.com/store/acme/settings"}, {}
    )
    assert read["success"] is True
    titles = [n["title"] for n in read["data"]["notes"]]
    assert "Country picker is a listbox overlay" in titles
    assert read["data"]["scope_chain"][0] == "host:admin.shopify.com"
    assert "Country picker" in read["data"]["briefing"]


async def test_remember_accepts_an_explicit_domain_scope(skill):
    out = await skill.execute(
        "remember",
        {
            "url": "domain:shopify.com",
            "title": "Shopify SSO redirect",
            "body": "Login bounces through accounts.shopify.com.",
        },
        {},
    )
    assert out["data"]["scope_key"] == "domain:shopify.com"
    read = await skill.execute("recall", {"url": "https://partners.shopify.com/x"}, {})
    assert "Shopify SSO redirect" in [n["title"] for n in read["data"]["notes"]]


async def test_required_params_are_enforced(skill):
    assert (await skill.execute("recall", {}, {}))["success"] is False
    assert (await skill.execute("remember", {"url": "https://x.com"}, {}))["success"] is False
    assert (await skill.execute("search", {}, {}))["success"] is False
    assert (await skill.execute("forget", {}, {}))["success"] is False


async def test_search_list_stats_and_forget(skill):
    await skill.execute(
        "remember",
        {"url": "https://weird.example/app", "title": "Zzz widget", "body": "notes"},
        {},
    )
    found = await skill.execute("search", {"query": "Zzz widget"}, {})
    assert found["data"]["count"] >= 1
    note_id = found["data"]["notes"][0]["note_id"]

    domains = await skill.execute("list_domains", {}, {})
    assert "host:weird.example" in {s["scope_key"] for s in domains["data"]["scopes"]}

    stats = await skill.execute("stats", {}, {})
    assert stats["data"]["notes"] > 0
    assert json.dumps(stats["data"])  # must survive the result envelope

    gone = await skill.execute("forget", {"note_id": note_id}, {})
    assert gone["data"]["forgotten"] is True


async def test_genesis_candidates_endpoint_is_proposal_only(skill):
    from memory.browser_domain_memory import capture_failure, get_store

    store = get_store()
    for _ in range(3):
        capture_failure(
            store,
            url="https://repeat.example/checkout",
            endpoint_id="click",
            args={"ref_or_selector": "#pay"},
            result={"success": False, "error": '<div class="modal"> intercepts pointer events'},
        )
    out = await skill.execute("genesis_candidates", {}, {})
    assert out["success"] is True
    assert out["data"]["count"] == 1
    candidate = out["data"]["candidates"][0]
    assert candidate["requires_human_approval"] is True
    assert candidate["auto_execute"] is False
    assert "code" not in candidate and "python_code" not in candidate
    assert "nothing was executed" in out["data"]["note"].lower()


async def test_skill_is_registered_for_llm_tool_use():
    import skills.impl as impl

    assert impl.get_implementation("browser_memory") is not None


# ---------------------------------------------------------------------------
# The browser hook in api.state
# ---------------------------------------------------------------------------


class _StubBrowser:
    def __init__(self, url: str):
        self._url = url

    async def get_page_info(self):
        return {"url": self._url, "title": "stub"}


def _hooked_state(dispatch_result: dict, page_url: str):
    """A BrainState with only the pieces the hook touches.

    Built with ``__new__`` deliberately: ``BrainState.__init__`` boots the
    whole brain, and the hook under test must work without any of it.
    """
    from api.state import BrainState

    st = BrainState.__new__(BrainState)
    st.browser = _StubBrowser(page_url)
    st._browser_last_url = ""

    async def _fake_dispatch(endpoint_id, args):
        return dict(dispatch_result)

    st._dispatch_browser_action = _fake_dispatch
    return st


async def test_navigate_success_attaches_site_briefing():
    from memory.browser_domain_memory import get_store

    get_store().add_note(
        scope="host:known.example",
        topic="checkout",
        title="Payment step is in a cross-origin iframe",
        body="Use list_iframes + execute_in_iframe on the Stripe frame.",
    )
    st = _hooked_state({"success": True, "url": "https://known.example/cart"}, "https://known.example/cart")
    out = await st._execute_browser_action("navigate", {"url": "https://known.example/cart"})
    assert out["success"] is True
    assert "Payment step is in a cross-origin iframe" in out["domain_knowledge"]
    assert out["domain_knowledge_scope"][0] == "host:known.example"


async def test_navigate_to_an_unknown_site_costs_nothing():
    """First visit must not replay every general technique into the context."""
    st = _hooked_state({"success": True}, "https://brand-new.example/")
    out = await st._execute_browser_action("navigate", {"url": "https://brand-new.example/"})
    assert "domain_knowledge" not in out


async def test_failed_click_is_captured_and_explained():
    from memory.browser_domain_memory import get_store

    st = _hooked_state(
        {
            "success": False,
            "error": 'Timeout 5000ms exceeded.\n  - <div id="cookie"> intercepts pointer events',
        },
        "https://capture.example/checkout",
    )
    out = await st._execute_browser_action("click", {"ref_or_selector": "#pay"})
    assert out["success"] is False
    # The failure was recorded against the site...
    notes = get_store().recall("https://capture.example/checkout")["notes"]
    captured = [n for n in notes if n["topic"] == "click_intercepted"]
    assert captured and captured[0]["scope_key"] == "host:capture.example"
    assert captured[0]["evidence"][0]["target"] == "#pay"
    # ...and the guidance came back in the same tool result.
    assert "domain_knowledge" in out
    assert "overlay" in out["domain_knowledge"].lower()


async def test_failure_recall_pulls_the_matching_general_technique():
    """A selector that never resolves should surface the shadow DOM note."""
    st = _hooked_state(
        {"success": False, "error": "Element not found: .cta"},
        "https://webcomponents.example/app",
    )
    out = await st._execute_browser_action("click", {"ref_or_selector": ".cta"})
    assert "Shadow DOM" in out["domain_knowledge"]


async def test_non_diagnostic_failure_records_nothing():
    from memory.browser_domain_memory import get_store

    before = get_store().stats()["notes"]
    st = _hooked_state(
        {"success": False, "error": "Cannot connect to Chrome. Start it with --remote-debugging-port=9222"},
        "https://offline.example/",
    )
    out = await st._execute_browser_action("click", {"ref_or_selector": "#a"})
    assert "domain_knowledge" not in out
    assert get_store().stats()["notes"] == before


async def test_hook_never_breaks_the_browser_action(monkeypatch):
    """Site memory is an enhancement; a broken store must not fail a click."""
    import memory.browser_domain_memory as bdm

    def _boom():
        raise RuntimeError("store is on fire")

    monkeypatch.setattr(bdm, "get_store", _boom)
    st = _hooked_state({"success": True, "clicked": "#a"}, "https://x.example/")
    out = await st._execute_browser_action("navigate", {"url": "https://x.example/"})
    assert out["success"] is True
    assert "domain_knowledge" not in out

    st2 = _hooked_state({"success": False, "error": "Element not found: #a"}, "https://x.example/")
    out2 = await st2._execute_browser_action("click", {"ref_or_selector": "#a"})
    assert out2["error"] == "Element not found: #a"


async def test_hook_preserves_the_original_result_payload():
    st = _hooked_state(
        {"success": True, "clicked": "#a", "extra": [1, 2, 3]},
        "https://passthrough.example/",
    )
    out = await st._execute_browser_action("click", {"ref_or_selector": "#a"})
    assert out["clicked"] == "#a"
    assert out["extra"] == [1, 2, 3]


async def test_hook_does_not_intercept_screencast_or_recording_endpoints():
    """The video-recording work owns its own endpoints; we must not touch them."""
    from memory.browser_domain_memory import get_store

    before = get_store().stats()["notes"]
    st = _hooked_state(
        {"success": False, "error": "Element is not attached to the DOM"},
        "https://video.example/",
    )
    for endpoint in ("trace_start", "har_stop", "screenshot", "evaluate", "download_next"):
        out = await st._execute_browser_action(endpoint, {})
        assert "domain_knowledge" not in out, endpoint
    assert get_store().stats()["notes"] == before
