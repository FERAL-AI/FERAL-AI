"""The macOS accessibility skill: declared surface, refusals, and a live Mac.

Three groups of tests here, and they exist for different reasons.

1. **Contract.** ``skills/manifests/macos_ax.json`` and the dispatch table
   in ``skills/impl/macos_ax.py`` are two halves of one promise. An id in
   the manifest with no dispatch key is a runtime 404 the model only
   discovers by spending a turn on it; a dispatch key no manifest
   declares is code nothing can reach. Both directions are asserted.

2. **Honest refusals.** The defect class this module was written against
   is a call that fails and says it succeeded (the ``window_list``
   regression: ``success: True`` with an empty list for a query that had
   errored). Every failure path here must produce ``success: False`` with
   a status code, and a missing Accessibility grant must produce the
   exact ``tcc_denied:accessibility`` token ``agents/tcc_card`` parses,
   because a 500 with an opaque AX number gets no permission card.

3. **The real Mac.** Everything above can pass on a tree of mocks while
   the AX API is being called wrongly. The tests marked ``live_macos``
   snapshot Finder and act on a ref, and skip (never fail) on a host with
   no Accessibility grant.
"""

from __future__ import annotations

import asyncio
import json
import platform
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.tcc_card import card_for_action_result, parse_tcc_error  # noqa: E402
from models.skill_manifest import SkillManifest  # noqa: E402
from skills.impl import macos_ax  # noqa: E402
from skills.impl.macos_ax import (  # noqa: E402
    ACTIONABLE_ACTIONS,
    AXNode,
    MACOS_AX_ENDPOINTS,
    MacOSAccessibilitySkill,
    normalise_ref,
)

MANIFEST_PATH = ROOT / "skills" / "manifests" / "macos_ax.json"
MANIFEST_RAW = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
MANIFEST = SkillManifest(**MANIFEST_RAW)
ENDPOINTS = {e.id: e for e in MANIFEST.endpoints}

SKILL = MacOSAccessibilitySkill()


def run(endpoint_id: str, **args) -> dict:
    return asyncio.run(SKILL.execute(endpoint_id, args, {}))


@pytest.fixture(autouse=True)
def _assume_the_grant(monkeypatch):
    """Contract tests must not depend on this machine's TCC state.

    Without this, every argument-validation assertion below would pass on
    a granted Mac and turn into a 403 on an ungranted one, which makes CI
    results a property of the build agent's System Settings. Tests that
    care about the denial re-patch it to False themselves; the live tests
    are gated on the real grant at collection time, so forcing True here
    cannot make one of them run on a host that lacks it.
    """
    monkeypatch.setattr(macos_ax, "accessibility_trusted", lambda: True)


def dispatch_keys() -> set:
    """The ids ``execute`` actually routes, read from the live dispatch dict.

    Taken by calling ``execute`` with an unknown id: the error names every
    known endpoint, so the table cannot drift away from this test by
    someone editing a literal in two places.
    """
    result = asyncio.run(SKILL.execute("__no_such_endpoint__", {}, {}))
    assert result["success"] is False
    _, _, listed = result["error"].partition("Known endpoints: ")
    return {part.strip() for part in listed.split(",") if part.strip()}


# ── 1. the declared surface equals the routed surface ─────────────

def test_manifest_skill_id_matches_the_implementation():
    assert MANIFEST.skill_id == SKILL.skill_id == "macos_ax"
    assert MANIFEST_PATH.stem == MANIFEST.skill_id, (
        "the loader indexes some manifests by file stem; keeping stem and "
        "skill_id identical removes a whole class of resolution bug"
    )


def test_every_declared_endpoint_is_dispatched():
    """A declared-but-unrouted endpoint is a 404 the model pays a turn for."""
    undispatched = sorted(set(ENDPOINTS) - dispatch_keys())
    assert not undispatched, (
        f"macos_ax.json declares {undispatched} but execute() routes "
        f"{sorted(dispatch_keys())}"
    )


def test_every_dispatched_endpoint_is_declared():
    """Routed-but-undeclared code is not a capability, it is dead weight."""
    undeclared = sorted(dispatch_keys() - set(ENDPOINTS))
    assert not undeclared, (
        f"execute() routes {undeclared}, which no manifest endpoint declares, "
        f"so nothing can ever call it"
    )


def test_the_exported_endpoint_set_agrees_with_both_sides():
    assert set(MACOS_AX_ENDPOINTS) == set(ENDPOINTS) == dispatch_keys()


def test_unknown_endpoint_is_a_404_that_names_the_alternatives():
    result = run("teleport")
    assert result["success"] is False
    assert result["status_code"] == 404
    assert "teleport" in result["error"]
    for known in MACOS_AX_ENDPOINTS:
        assert known in result["error"]


# ── 2. the manifest says what the runtime will actually do ────────

def test_manifest_states_the_accessibility_precondition_and_what_it_looks_like():
    """The bug class of this whole session: a manifest that tells the model
    to do something the runtime refuses, without saying so."""
    prose = MANIFEST.description
    assert "Accessibility" in prose
    assert "tcc_denied:accessibility" in prose, (
        "the model has to be able to recognise the refusal it will get"
    )
    assert "System Settings" in prose and "Privacy & Security" in prose, (
        "a permission error the user cannot act on is not an error message"
    )
    assert "check_permission" in prose


def test_manifest_names_the_other_preconditions():
    prose = MANIFEST.description.lower()
    assert "macos only" in prose or "macos-only" in prose
    assert "already be running" in prose, "nothing here launches an app"
    assert "electron" in prose, "the poor-tree case has to be named"
    assert "untrusted" in prose, "AX labels include web page text"


def test_acting_endpoints_are_not_marked_safe():
    """Reading a tree is not the same as pressing a button."""
    for reading in ("snapshot", "find", "list_windows", "describe",
                    "check_permission"):
        assert ENDPOINTS[reading].safety_tier == "safe", reading
        assert ENDPOINTS[reading].read_only_hint is True, reading
    for acting in ("click", "set_value", "perform_action"):
        assert ENDPOINTS[acting].safety_tier == "confirm", acting
        assert ENDPOINTS[acting].read_only_hint is False, acting
        assert ENDPOINTS[acting].requires_user_approval is True, acting


def test_no_endpoint_ships_blank_guidance():
    for endpoint in MANIFEST.endpoints:
        assert endpoint.description.strip(), endpoint.id
        assert endpoint.returns_description.strip(), endpoint.id
        for param in endpoint.params:
            assert param.description.strip(), f"{endpoint.id}.{param.name}"


def test_no_optional_param_relies_on_a_manifest_default():
    """Manifest ``default`` is never merged into args before dispatch.

    VERIFIED in this tree: ``EndpointParam.default`` is read in exactly
    one place, ``SkillRegistry._manifest_to_tools``, and only to decorate
    the JSON schema shown to the model. An impl that declares
    ``default: "interactive"`` and then reads ``args["filter"]`` gets
    ``None``. Declaring one here would be a lie about who applies it, so
    this manifest declares none and the impl owns every default.
    """
    declaring = [
        f"{e['id']}.{p['name']}"
        for e in MANIFEST_RAW["endpoints"]
        for p in e.get("params", [])
        if p.get("default") is not None
    ]
    assert not declaring, (
        f"{declaring} declare a manifest default that nothing applies; "
        f"handle the default inside skills/impl/macos_ax.py instead"
    )


def test_omitting_every_optional_param_still_dispatches():
    """The other half: the impl, not the manifest, owns the defaults.

    ``snapshot`` carries seven optional params. Calling it with none of
    them must reach the app lookup and fail there, not fail earlier on a
    missing argument.

    The status that proves that is platform-dependent, and asserting the
    macOS one flatly is why this failed on every Linux CI run: there the
    call gets past argument handling and stops at the platform check
    instead, returning 501 because pyobjc's bindings do not exist. Both
    are "it dispatched"; neither is an argument error.

    Kept running on Linux rather than marked live_macos, because the
    assertion below it is the one this test is really named after and it
    holds on both: no optional parameter name may appear in the error.
    Skipping would take that off CI entirely.
    """
    result = run("snapshot", app="__definitely_not_running__")
    expected = 404 if platform.system() == "Darwin" else 501
    assert result["status_code"] == expected, (
        f"should fail on the app (404) or the platform (501), not on args; "
        f"got {result['status_code']}: {result.get('error')}"
    )
    for param in ("filter", "max_nodes", "offset", "max_depth", "timeout_s"):
        assert param not in (result["error"] or "")


def test_trigger_phrases_cover_the_sentences_people_say():
    phrases = " | ".join(MANIFEST.trigger_phrases).lower()
    for expected in (
        "what's on my screen", "what can i click", "what buttons",
        "click the back button", "type into the address bar",
    ):
        assert expected in phrases, expected


def test_trigger_phrases_are_not_a_copy_of_another_skill():
    coding = json.loads(
        (ROOT / "skills" / "manifests" / "coding_tools.json").read_text()
    )
    assert set(MANIFEST.trigger_phrases) != set(coding.get("trigger_phrases", []))


def test_manifest_permissions_are_the_ones_this_skill_really_uses():
    declared = {str(p) for p in MANIFEST.permissions}
    assert "screen" in declared, "it reads the contents of windows"
    assert "input_control" in declared, "click and set_value operate the UI"


# ── 3. refusals are refusals ──────────────────────────────────────

@pytest.mark.parametrize("endpoint_id", sorted(MACOS_AX_ENDPOINTS))
def test_every_endpoint_refuses_with_the_tcc_token_when_untrusted(
    endpoint_id, monkeypatch,
):
    """Without the grant, nothing may pretend to have worked."""
    monkeypatch.setattr(macos_ax, "accessibility_trusted", lambda: False)
    result = run(endpoint_id, ref="ax1", query="x", action="AXPress", value="x")
    assert result["success"] is False, endpoint_id
    assert result["status_code"] == 403, endpoint_id
    assert result["error"] == "tcc_denied:accessibility", endpoint_id


def test_the_denial_mints_a_permission_card(monkeypatch):
    """The token is only useful if ``agents/tcc_card`` recognises it."""
    monkeypatch.setattr(macos_ax, "accessibility_trusted", lambda: False)
    result = run("snapshot")
    assert parse_tcc_error(result["error"]) == "accessibility"
    card = card_for_action_result(
        result, skill_id="macos_ax", action="macos_ax.snapshot",
    )
    assert card is not None
    assert card["type"] == "tcc_card"
    assert card["permission_key"] == "accessibility"
    assert "Privacy_Accessibility" in card["macos_deeplink"]


def test_a_non_macos_host_gets_501_not_a_traceback(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    result = run("check_permission")
    assert result["success"] is False
    assert result["status_code"] == 501
    assert "macOS-only" in result["error"]


@pytest.mark.parametrize("bad", ["", None, "nonsense", "12", "ax", "[ax]"])
def test_a_malformed_ref_is_rejected_before_anything_is_pressed(bad):
    result = run("click", ref=bad)
    assert result["success"] is False
    assert result["status_code"] == 400
    assert "snapshot" in result["error"], "the fix has to be in the message"


def test_an_unissued_ref_is_a_404_not_a_click():
    result = run("click", ref="ax999999999")
    assert result["success"] is False
    assert result["status_code"] == 404


def test_every_failure_envelope_has_the_four_fields():
    for result in (
        run("teleport"),
        run("click", ref="nonsense"),
        run("find", app="Finder"),          # missing query
        run("snapshot", app="__nope__"),
    ):
        assert set(result) == {"success", "status_code", "data", "error"}
        assert result["success"] is False
        assert isinstance(result["status_code"], int)
        assert result["error"]


def test_find_without_a_query_is_a_400():
    result = run("find", app="Finder")
    assert result["success"] is False
    assert result["status_code"] == 400
    assert "query" in result["error"]


def test_an_invalid_filter_is_refused_rather_than_silently_coerced():
    result = run("snapshot", app="Finder", filter="sometimes")
    assert result["success"] is False
    assert result["status_code"] == 400
    assert "interactive" in result["error"] and "all" in result["error"]


# ── 4. ref handling ───────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("ax12", "ax12"),
    ("[ax12]", "ax12"),
    ("  [ax12] ", "ax12"),
    ("AX12", "ax12"),
    ("ax0", "ax0"),
    ("ax12x", ""),
    ("selector", ""),
    (None, ""),
    (12, ""),
])
def test_normalise_ref_accepts_what_the_snapshot_printed(raw, expected):
    """The tree prints ``[ax12]``. Refusing the brackets it just showed the
    model would be a self-inflicted failure."""
    assert normalise_ref(raw) == expected


class _StubElement:
    """A hashable stand-in for an AXUIElementRef."""

    def __init__(self, name: str) -> None:
        self.name = name


def _stub_ref(monkeypatch, *, recorded_role: str, live_role: str,
              subrole: str = "", secure: bool = False) -> str:
    element = _StubElement(recorded_role)
    node = AXNode(
        ref="", role=recorded_role, subrole=subrole, label="Stub",
        label_source="AXTitle", depth=1, actions=["AXPress"], secure=secure,
    )
    ref = macos_ax._REFS.add(element, node, "StubApp", 4242)
    monkeypatch.setattr(macos_ax, "_role", lambda el: live_role)
    monkeypatch.setattr(macos_ax, "_subrole", lambda el: subrole)
    return ref


def test_a_ref_whose_element_changed_role_is_409_and_presses_nothing(monkeypatch):
    """The failure that matters most: pressing what now occupies the slot."""
    monkeypatch.setattr(macos_ax, "accessibility_trusted", lambda: True)
    ref = _stub_ref(monkeypatch, recorded_role="AXButton", live_role="AXTextField")
    result = run("click", ref=ref)
    assert result["success"] is False
    assert result["status_code"] == 409
    assert "NOT performed" in result["error"]
    assert result["data"]["expected_role"] == "AXButton"
    assert result["data"]["actual_role"] == "AXTextField"


def test_a_dead_element_is_409_not_a_crash(monkeypatch):
    monkeypatch.setattr(macos_ax, "accessibility_trusted", lambda: True)
    ref = _stub_ref(monkeypatch, recorded_role="AXButton", live_role="")
    result = run("click", ref=ref)
    assert result["success"] is False
    assert result["status_code"] == 409
    assert "re-snapshot" in result["error"].lower()


@pytest.mark.parametrize("endpoint_id,extra", [
    ("click", {}),
    ("set_value", {"value": "hunter2"}),
    ("perform_action", {"action": "AXPress"}),
    ("describe", {}),
])
def test_a_password_field_is_never_touched(endpoint_id, extra, monkeypatch):
    monkeypatch.setattr(macos_ax, "accessibility_trusted", lambda: True)
    ref = _stub_ref(
        monkeypatch,
        recorded_role="AXTextField", live_role="AXTextField",
        subrole="AXSecureTextField", secure=True,
    )
    result = run(endpoint_id, ref=ref, **extra)
    assert result["success"] is False
    assert result["status_code"] == 403
    assert "secure text field" in result["error"]


def test_refs_are_never_reused_across_snapshots():
    """A ref from an old snapshot must not silently mean a new element."""
    store = macos_ax._RefStore(limit=10)
    node = AXNode(ref="", role="AXButton", subrole="", label="a",
                  label_source="AXTitle", depth=0)
    first = store.add(_StubElement("a"), node, "App", 1)
    second = store.add(_StubElement("b"), node, "App", 1)
    assert first != second
    # even after eviction the counter does not wind back
    for index in range(20):
        store.add(_StubElement(str(index)), node, "App", 1)
    assert store.get(first) is None, "evicted refs must resolve to nothing"
    latest = store.add(_StubElement("z"), node, "App", 1)
    assert latest not in (first, second)


def test_an_evicted_ref_is_404_rather_than_a_wrong_element(monkeypatch):
    monkeypatch.setattr(macos_ax, "accessibility_trusted", lambda: True)
    small = macos_ax._RefStore(limit=2)
    monkeypatch.setattr(macos_ax, "_REFS", small)
    node = AXNode(ref="", role="AXButton", subrole="", label="a",
                  label_source="AXTitle", depth=0)
    stale = small.add(_StubElement("a"), node, "App", 1)
    for index in range(3):
        small.add(_StubElement(str(index)), node, "App", 1)
    result = run("click", ref=stale)
    assert result["status_code"] == 404
    assert "aged out" in result["error"]


# ── 5. the node model ─────────────────────────────────────────────

def _node(**kwargs) -> AXNode:
    base = dict(ref="ax1", role="AXGroup", subrole="", label="",
                label_source="", depth=0)
    base.update(kwargs)
    return AXNode(**base)


def test_interactivity_is_not_just_having_any_action():
    """Measured on this Mac: 547 of Finder's 677 elements publish *some*
    action, because AXCancel and AXShowMenu are near-universal. A filter
    that keeps those keeps everything."""
    assert "AXShowMenu" not in ACTIONABLE_ACTIONS
    assert "AXCancel" not in ACTIONABLE_ACTIONS
    assert not _node(actions=["AXShowMenu", "AXCancel"]).is_interactive()
    assert _node(actions=["AXPress"]).is_interactive()
    assert _node(role="AXTextField", actions=[]).is_interactive()
    assert not _node(role="AXStaticText", actions=[]).is_interactive()


def test_a_line_carries_ref_role_label_and_bounds():
    line = _node(
        role="AXButton", label="Back", depth=2,
        bounds={"x": 24.0, "y": 105.0, "width": 28.0, "height": 28.0},
        actions=["AXPress"],
    ).to_line()
    assert "[ax1]" in line
    assert "AXButton" in line
    assert '"Back"' in line
    assert "(24,105 28x28)" in line


def test_a_secure_field_never_prints_its_value():
    line = _node(
        role="AXTextField", subrole="AXSecureTextField",
        label="hunter2", secure=True,
    ).to_line()
    assert "hunter2" not in line
    assert "value withheld" in line


def test_a_disabled_control_says_so():
    assert "(disabled)" in _node(role="AXButton", label="Forward",
                                enabled=False).to_line()


# ── 6. the walk cannot run away ───────────────────────────────────
#
# None of these three properties can be provoked from a real app on
# demand, and all three are the difference between a bounded tool call
# and a hung orchestrator turn, so they are pinned against a synthetic
# tree instead.

class _FakeElement:
    def __init__(self, name: str) -> None:
        self.name = name
        self.children: list = []


def _patch_children(monkeypatch) -> None:
    def fake_attr(element, attribute):
        if attribute == "AXChildren":
            return 0, list(getattr(element, "children", []))
        return -25205, None

    monkeypatch.setattr(macos_ax, "_attr", fake_attr)


def _budget(**kwargs):
    import time as _time

    params = dict(deadline=_time.monotonic() + 30, max_depth=200)
    params.update(kwargs)
    return macos_ax._WalkBudget(**params)


def test_a_cycle_in_the_tree_does_not_loop_forever(monkeypatch):
    """AX trees are documented as trees and are not always trees.

    A toolbar that publishes its overflow menu as a child of both itself
    and the window produces a parent in its own descendant list. Without
    identity tracking that recurses until the stack dies.
    """
    _patch_children(monkeypatch)
    a, b = _FakeElement("a"), _FakeElement("b")
    a.children = [b]
    b.children = [a]  # the cycle
    budget = _budget()
    visited = list(macos_ax._walk(a, budget))
    assert [element.name for element, _depth in visited] == ["a", "b"]
    assert budget.visits == 2


def test_the_same_element_reached_twice_is_yielded_once(monkeypatch):
    """A diamond, not a cycle: shared children are common in real trees."""
    _patch_children(monkeypatch)
    root, left, right, shared = (_FakeElement(n) for n in "rlxs")
    root.children = [left, right]
    left.children = [shared]
    right.children = [shared]
    names = [e.name for e, _d in macos_ax._walk(root, _budget())]
    assert names.count("s") == 1


def test_the_visit_cap_stops_a_tree_that_answers_too_fast(monkeypatch):
    """The clock alone is not enough.

    A malformed or generated tree can serve tens of thousands of elements
    inside an 8 second budget, and the result would be an unbounded node
    list built in memory before any cap was applied.
    """
    _patch_children(monkeypatch)
    root = _FakeElement("root")
    root.children = [
        _FakeElement(str(i)) for i in range(macos_ax.MAX_VISITED_ELEMENTS + 500)
    ]
    budget = _budget()
    visited = list(macos_ax._walk(root, budget))
    assert len(visited) <= macos_ax.MAX_VISITED_ELEMENTS + 1
    assert budget.hit_visit_cap is True


def test_the_depth_cap_is_recorded_rather_than_silently_applied(monkeypatch):
    _patch_children(monkeypatch)
    node = root = _FakeElement("0")
    for index in range(1, 20):
        child = _FakeElement(str(index))
        node.children = [child]
        node = child
    budget = _budget(max_depth=5)
    visited = list(macos_ax._walk(root, budget))
    assert len(visited) == 6, "depths 0..5 inclusive"
    assert budget.hit_depth is True


def test_an_expired_clock_stops_the_walk(monkeypatch):
    import time as _time

    _patch_children(monkeypatch)
    root = _FakeElement("root")
    root.children = [_FakeElement(str(i)) for i in range(100)]
    budget = _budget(deadline=_time.monotonic() - 1)
    assert list(macos_ax._walk(root, budget)) == []
    assert budget.hit_timeout is True


# ── 7. the real Mac ───────────────────────────────────────────────

live_macos = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="the AX API only exists on macOS",
)


def _trusted() -> bool:
    if platform.system() != "Darwin":
        return False
    try:
        return macos_ax.accessibility_trusted()
    except Exception:  # noqa: BLE001
        return False


needs_grant = pytest.mark.skipif(
    not _trusted(),
    reason="Accessibility is not granted to the test process",
)


@live_macos
@needs_grant
def test_check_permission_reports_the_live_grant():
    result = run("check_permission")
    assert result["success"] is True
    assert result["data"]["accessibility_trusted"] is True
    assert isinstance(result["data"]["running_apps"], list)


@live_macos
@needs_grant
def test_snapshotting_finder_produces_refs_roles_labels_and_bounds():
    result = run("snapshot", app="Finder", max_nodes=40)
    if not result["success"]:
        pytest.skip(f"Finder is not readable here: {result['error']}")
    data = result["data"]
    assert data["app"] == "Finder"
    assert data["nodes"], "Finder always exposes something"
    assert data["tree"].startswith("Finder (pid ")
    for node in data["nodes"]:
        assert node["ref"].startswith("ax")
        assert node["role"].startswith("AX")
    assert any(n["bounds"] for n in data["nodes"]), (
        "AXPosition/AXSize must unpack to real numbers, not opaque AXValue "
        "boxes"
    )


@live_macos
@needs_grant
def test_the_filter_actually_filters():
    everything = run("snapshot", app="Finder", filter="all", max_nodes=1)
    interactive = run("snapshot", app="Finder", filter="interactive", max_nodes=1)
    if not (everything["success"] and interactive["success"]):
        pytest.skip("Finder is not readable here")
    assert (
        interactive["data"]["total_matched"] < everything["data"]["total_matched"]
    ), "'interactive' that keeps everything is not a filter"


@live_macos
@needs_grant
def test_pagination_covers_the_tree_without_repeating_itself():
    """The browser lane's hard 500-node cap with no offset is the defect
    this replaces: a big app silently loses everything past node 500."""
    first = run("snapshot", app="Finder", filter="all", max_nodes=15, offset=0)
    if not first["success"]:
        pytest.skip("Finder is not readable here")
    total = first["data"]["total_matched"]
    if total <= 15:
        pytest.skip("Finder's tree is too small to page here")
    assert first["data"]["truncated"] is True
    assert first["data"]["next_offset"] == 15
    assert "TRUNCATED" in first["data"]["tree"]
    second = run(
        "snapshot", app="Finder", filter="all", max_nodes=15,
        offset=first["data"]["next_offset"],
    )
    assert second["success"] is True
    assert second["data"]["offset"] == 15
    assert {n["ref"] for n in first["data"]["nodes"]}.isdisjoint(
        {n["ref"] for n in second["data"]["nodes"]}
    )


@live_macos
@needs_grant
def test_a_walk_stops_at_its_wall_clock_budget():
    result = run(
        "snapshot", app="Finder", filter="all", include_menus=True,
        timeout_s=0.5, max_nodes=5,
    )
    if not result["success"]:
        pytest.skip("Finder is not readable here")
    assert result["data"]["elapsed_ms"] < 3000, (
        "a 0.5s budget must not turn into a multi-second stall"
    )


@live_macos
@needs_grant
def test_a_depth_limit_is_reported_not_hidden():
    """A truncated walk must say so rather than look like a complete one.

    The depth is derived from the live tree instead of hardcoded. A fixed
    ``max_depth=2`` assumed Finder's window is deeper than two levels,
    which depends on which Finder window happens to be open: with a
    shallow one the walk completes, ``limits_hit`` is correctly empty,
    and the test failed while the code was behaving perfectly.

    Note ``max_nodes`` is pagination (``returned`` / ``next_offset``), not
    a walk limit, so it never appears in ``limits_hit``. Only bounds that
    stop the walk early do.
    """
    full = run("snapshot", app="Finder", filter="all")
    if not full["success"]:
        pytest.skip("Finder is not readable here")
    assert full["data"]["limits_hit"] == [], (
        "an unbounded walk reported a limit: " f"{full['data']['limits_hit']}"
    )

    depth = _max_depth_in_tree(full["data"]["tree"])
    if depth < 2:
        pytest.skip(f"Finder's tree is only {depth} level(s) deep; nothing to truncate")

    result = run("snapshot", app="Finder", filter="all", max_depth=depth - 1)
    assert result["success"] is True
    assert "max_depth" in result["data"]["limits_hit"], (
        f"walk was cut at depth {depth - 1} of {depth} and did not report it"
    )
    assert "WALK STOPPED EARLY" in result["data"]["tree"]


def _max_depth_in_tree(tree: str) -> int:
    """Deepest indent level among the element lines of a snapshot."""
    depths = [
        (len(line) - len(line.lstrip(" "))) // 2
        for line in tree.splitlines()
        if "[ax" in line
    ]
    return max(depths) if depths else 0


@live_macos
@needs_grant
def test_find_locates_a_named_control_and_hands_back_a_usable_ref():
    listed = run("check_permission")
    apps = listed["data"]["running_apps"]
    target = next((a for a in ("Finder", "Google Chrome") if a in apps), None)
    if target is None:
        pytest.skip("neither Finder nor Chrome is running")
    query = "Recents" if target == "Finder" else "Reload"
    result = run("find", app=target, query=query)
    assert result["success"] is True
    if not result["data"]["matches"]:
        pytest.skip(f"{target} is not showing {query!r} right now")
    match = result["data"]["matches"][0]
    assert query.lower() in match["label"].lower()
    described = run("describe", ref=match["ref"])
    assert described["success"] is True
    assert described["data"]["role"] == match["role"]


@live_macos
@needs_grant
def test_a_query_that_matches_nothing_is_a_real_answer_not_a_fake_success():
    """Distinct from an error: the search ran, and it found nothing.

    This is the exact seam the ``window_list`` regression got wrong in
    the other direction, so both halves are pinned: a *failed read* is
    ``success: False`` (below), and an *empty result* is ``success: True``
    with the count that proves the search happened.
    """
    result = run("find", app="Finder", query="zzz_no_such_control_zzz")
    assert result["success"] is True
    assert result["data"]["match_count"] == 0
    assert result["data"]["elements_visited"] > 0, (
        "an empty result must prove a tree was actually walked"
    )


@live_macos
@needs_grant
def test_a_missing_app_is_a_404_that_lists_what_is_running():
    result = run("snapshot", app="No Such Application 9000")
    assert result["success"] is False
    assert result["status_code"] == 404
    assert "Running apps:" in result["error"]


@live_macos
@needs_grant
def test_list_windows_reports_a_count_it_actually_read():
    result = run("list_windows", app="Finder")
    assert result["success"] is True
    assert result["data"]["window_count"] == len(result["data"]["windows"])
    for window in result["data"]["windows"]:
        assert isinstance(window["index"], int)


@live_macos
@needs_grant
def test_a_bad_window_index_is_404_rather_than_an_empty_tree():
    result = run("snapshot", app="Finder", window_index=987)
    assert result["success"] is False
    assert result["status_code"] == 404


@live_macos
@needs_grant
def test_snapshot_then_act_on_a_ref_from_that_snapshot():
    """The whole point of the module: read a tree, name a target, act.

    The action is deliberately the most inert one available, an
    ``AXShowMenu`` is not used and nothing is pressed here; this asserts
    the ref resolves to the same live element the snapshot described,
    which is the property every click depends on.
    """
    snapshot = run("snapshot", app="Finder", max_nodes=30)
    if not snapshot["success"] or not snapshot["data"]["nodes"]:
        pytest.skip("Finder is not readable here")
    node = snapshot["data"]["nodes"][0]
    described = run("describe", ref=node["ref"])
    assert described["success"] is True
    assert described["data"]["role"] == node["role"]
    assert described["data"]["app"] == "Finder"
    assert isinstance(described["data"]["actions"], list)


@live_macos
@needs_grant
def test_setting_a_value_on_something_unwritable_is_refused():
    result = run("snapshot", app="Finder", filter="all", max_nodes=200)
    if not result["success"]:
        pytest.skip("Finder is not readable here")
    static = next(
        (n for n in result["data"]["nodes"] if n["role"] == "AXStaticText"), None,
    )
    if static is None:
        pytest.skip("no static text in this Finder window")
    written = run("set_value", ref=static["ref"], value="nope")
    assert written["success"] is False
    assert written["status_code"] == 422
    assert "not settable" in written["error"]


@live_macos
@needs_grant
def test_an_unsupported_action_is_refused_with_the_supported_list():
    snapshot = run("snapshot", app="Finder", max_nodes=10)
    if not snapshot["success"] or not snapshot["data"]["nodes"]:
        pytest.skip("Finder is not readable here")
    ref = snapshot["data"]["nodes"][0]["ref"]
    result = run("perform_action", ref=ref, action="AXNotAnAction")
    assert result["success"] is False
    assert result["status_code"] == 422
    assert "supports" in result["error"]
