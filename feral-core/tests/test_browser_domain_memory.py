"""Tests for per-domain browser knowledge: scoping, capture, recall, safety.

These exercise real SQLite writes against a tmp_path database. The autouse
``isolate_feral_home`` fixture in conftest already points FERAL_HOME at a
per-test tmp dir, so nothing here can touch the operator's ~/.feral.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from memory.browser_domain_memory import (
    GENESIS_CANDIDATE_THRESHOLD,
    DomainKnowledgeStore,
    capture_failure,
    classify_failure,
    domain_scope,
    get_store,
    host_scope,
    normalize_host,
    registrable_domain,
    scope_chain,
)


@pytest.fixture()
def store(tmp_path: Path) -> DomainKnowledgeStore:
    s = DomainKnowledgeStore(tmp_path / "sub" / "dm.db")
    s.ensure_seeded()
    return s


# ---------------------------------------------------------------------------
# Domain scoping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected_host,expected_domain",
    [
        ("https://admin.shopify.com/store/acme", "admin.shopify.com", "shopify.com"),
        ("https://www.shopify.com/pricing", "shopify.com", "shopify.com"),
        ("https://shop.example.co.uk/cart", "shop.example.co.uk", "example.co.uk"),
        ("https://example.co.uk", "example.co.uk", "example.co.uk"),
        ("http://localhost:3000/x", "localhost", "localhost"),
        ("http://127.0.0.1:8080/", "127.0.0.1", "127.0.0.1"),
        # myshopify.com is a public suffix: every store is its own owner, so
        # the registrable domain must NOT collapse to myshopify.com.
        ("https://acme.myshopify.com/admin", "acme.myshopify.com", "acme.myshopify.com"),
        ("https://a.b.pages.dev/x", "a.b.pages.dev", "b.pages.dev"),
        ("letterboxd.com/film/heat", "letterboxd.com", "letterboxd.com"),
        ("", "", ""),
    ],
)
def test_registrable_domain(url, expected_host, expected_domain):
    assert normalize_host(url) == expected_host
    assert registrable_domain(url) == expected_domain


def test_scope_chain_is_most_specific_first():
    assert scope_chain("https://admin.shopify.com/x") == [
        "host:admin.shopify.com",
        "domain:shopify.com",
        "global",
    ]
    # No redundant domain scope when host already is the registrable domain.
    assert scope_chain("https://shopify.com/x") == ["host:shopify.com", "global"]
    assert scope_chain("") == ["global"]


def test_scope_helpers():
    assert host_scope("https://admin.shopify.com/a") == "host:admin.shopify.com"
    assert domain_scope("https://admin.shopify.com/a") == "domain:shopify.com"


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


# The left-hand strings are the literal messages Playwright emits (verified by
# grepping the installed driver bundle) plus BrowserController's own CDP-path
# "Element not found:" raise.
@pytest.mark.parametrize(
    "error,code",
    [
        (
            'Timeout 5000ms exceeded.\nCall log:\n  - waiting for locator("#save")\n'
            '  - <div id="cookie-banner">…</div> intercepts pointer events',
            "click_intercepted",
        ),
        ("Element is not a <select> element", "not_native_select"),
        (
            "Element is not an <input>, <textarea> or [contenteditable] element",
            "not_editable",
        ),
        ('strict mode violation: locator("button") resolved to 3 elements', "ambiguous_selector"),
        ("Element is not attached to the DOM", "detached_node"),
        ("element is not visible", "not_visible"),
        ("element is outside of the viewport", "not_visible"),
        ("element is not enabled", "not_enabled"),
        (
            'SecurityError: Blocked a frame with origin "https://a.com" from '
            "accessing a cross-origin frame.",
            "cross_origin_iframe",
        ),
        ("Element not found: .cta", "selector_not_found"),
        (
            'Timeout 5000ms exceeded.\nCall log:\n  - waiting for locator("#late")',
            "selector_timeout",
        ),
    ],
)
def test_classify_real_playwright_errors(error, code):
    sig = classify_failure(error)
    assert sig is not None, error
    assert sig.code == code
    assert sig.guidance.strip()


@pytest.mark.parametrize(
    "error",
    [
        "",
        "Browser not available",
        "Cannot connect to Chrome. Start it with --remote-debugging-port=9222",
        "Unknown browser action: frobnicate",
        "net::ERR_CONNECTION_REFUSED",
        "some unrecognised thing happened",
    ],
)
def test_non_diagnostic_errors_are_not_classified(error):
    """A store full of 'Chrome is not running' teaches the agent nothing."""
    assert classify_failure(error) is None


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def test_capture_writes_host_scoped_observation(store):
    result = capture_failure(
        store,
        url="https://admin.shopify.com/store/acme/products",
        endpoint_id="select",
        args={"ref_or_selector": "#country"},
        result={"success": False, "error": "Element is not a <select> element"},
    )
    assert result is not None
    assert result["scope_key"] == "host:admin.shopify.com"
    assert result["topic"] == "not_native_select"
    assert result["created"] is True

    notes = store.recall("https://admin.shopify.com/store/acme/products")["notes"]
    observation = [n for n in notes if n["kind"] == "observation"][0]
    assert observation["evidence"][0]["endpoint"] == "select"
    assert observation["evidence"][0]["target"] == "#country"


def test_capture_is_idempotent_and_counts(store):
    for _ in range(4):
        out = capture_failure(
            store,
            url="https://checkout.example.com/pay",
            endpoint_id="click",
            args={"ref_or_selector": "#submit"},
            result={"success": False, "error": '<div id="banner"> intercepts pointer events'},
        )
    assert out["observed_count"] == 4
    assert out["created"] is False
    matching = [
        n
        for n in store.recall("https://checkout.example.com/pay")["notes"]
        if n["topic"] == "click_intercepted" and n["kind"] == "observation"
    ]
    assert len(matching) == 1, "repeat failures must reinforce one row, not duplicate"
    # Evidence stays bounded.
    assert len(matching[0]["evidence"]) <= 5


def test_capture_skips_success_and_non_diagnostic(store):
    before = store.stats()["notes"]
    assert capture_failure(
        store, url="https://x.com/", endpoint_id="click", args={},
        result={"success": True, "clicked": "#a"},
    ) is None
    assert capture_failure(
        store, url="https://x.com/", endpoint_id="click", args={},
        result={"success": False, "error": "Cannot connect to Chrome"},
    ) is None
    # evaluate is excluded: a broken JS snippet is the model's fault, not the site's
    assert capture_failure(
        store, url="https://x.com/", endpoint_id="evaluate", args={},
        result={"success": False, "error": "Element is not attached to the DOM"},
    ) is None
    assert capture_failure(
        store, url="", endpoint_id="click", args={},
        result={"success": False, "error": "Element is not a <select> element"},
    ) is None
    assert store.stats()["notes"] == before


def test_capture_reads_fill_form_partial_failures(store):
    out = capture_failure(
        store,
        url="https://forms.example.org/apply",
        endpoint_id="fill_form",
        args={"fields": {"#email": "a@b.c"}},
        result={
            "success": False,
            "filled": [],
            "failed": {"#email": "Element is not an <input>, <textarea> or [contenteditable] element"},
            "total": 1,
        },
    )
    assert out is not None and out["topic"] == "not_editable"


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------


def test_recall_orders_host_then_domain_then_global(store):
    store.add_note(
        scope="host:admin.shopify.com", topic="polaris",
        title="Polaris inputs need real keystrokes", body="host level",
    )
    store.add_note(
        scope="domain:shopify.com", topic="auth",
        title="Shopify login is behind an SSO redirect", body="domain level",
    )
    notes = store.recall("https://admin.shopify.com/store/acme")["notes"]
    scopes = [n["scope_key"] for n in notes]
    assert scopes[0] == "host:admin.shopify.com"
    assert scopes[1] == "domain:shopify.com"
    assert scopes[-1] == "global"


def test_host_scoped_note_does_not_leak_to_a_sibling_host(store):
    """admin.shopify.com is a Polaris SPA; shopify.com is a marketing site.

    Filing everything at the registrable domain would fire admin-only quirks
    on the marketing site, which is why capture writes at host scope.
    """
    store.add_note(
        scope="https://admin.shopify.com/x", topic="polaris",
        title="Polaris inputs reject synthetic setters", body="admin only",
    )
    marketing = store.recall("https://shopify.com/pricing")["notes"]
    assert all(n["title"] != "Polaris inputs reject synthetic setters" for n in marketing)

    # ... but a note deliberately filed at the domain DOES reach the subdomain.
    store.add_note(
        scope="domain:shopify.com", topic="auth",
        title="Site-wide cookie banner", body="everywhere",
    )
    admin = store.recall("https://admin.shopify.com/x")["notes"]
    assert any(n["title"] == "Site-wide cookie banner" for n in admin)


def test_recall_can_exclude_global_techniques(store):
    """The navigate hook uses this so a first visit costs zero tokens."""
    fresh = store.recall("https://never-seen-before.example/", include_global=False)
    assert fresh["notes"] == []
    assert fresh["briefing"] == ""
    assert store.recall("https://never-seen-before.example/")["notes"]


def test_recall_topic_filter_matches_topic_or_tag(store):
    """Failure code -> the general technique that explains it.

    The shadow DOM note is filed under topic 'shadow_dom' but tagged
    'selector_not_found', because a selector that never resolves is usually a
    shadow root. The failure-time hook looks up by failure code.
    """
    hits = store.recall("https://any.example/", topic="selector_not_found")["notes"]
    titles = [n["title"] for n in hits]
    assert any("Shadow DOM" in t for t in titles), titles


def test_briefing_is_bounded_and_readable(store):
    for i in range(30):
        store.add_note(
            scope="host:big.example", topic="noise", title=f"note {i}",
            body="x" * 900,
        )
    briefing = store.recall("https://big.example/")["briefing"]
    assert briefing.startswith("### What FERAL already knows about big.example")
    assert len(briefing) < 4000, "briefing must not displace the page snapshot"


# ---------------------------------------------------------------------------
# Seeding + attribution
# ---------------------------------------------------------------------------


def test_seeding_is_idempotent_and_attributed(store):
    first = store.stats()["notes"]
    again = store.ensure_seeded()
    assert again["seeded"] is False
    assert store.stats()["notes"] == first

    seeded = store.recall("https://anything.example/", limit=50)["notes"]
    upstream = [n for n in seeded if n.get("attribution")]
    assert len(upstream) >= 10, "expected the browser-harness technique set"
    for n in upstream:
        assert "browser-use/browser-harness" in n["attribution"]
        assert n["license"] == "MIT"
        assert n["upstream_excerpt"], f"{n['title']} claims attribution but quotes nothing"


def test_mit_notice_is_stored_as_data(store):
    hits = store.search("Browser Use")
    notice = [n for n in hits if n["topic"] == "attribution"]
    assert notice, "MIT notice must travel with the database"
    text = notice[0]["upstream_excerpt"]
    assert "MIT License" in text
    assert "Copyright (c) 2026 Browser Use" in text
    assert "THE SOFTWARE IS PROVIDED" in text


def test_seeded_topics_cover_the_requested_set(store):
    topics = {n["topic"] for n in store.recall("https://x.example/", limit=50)["notes"]}
    for required in (
        "shadow_dom", "cross_origin_iframe", "dialogs", "not_native_select",
        "uploads", "drag_and_drop",
    ):
        assert required in topics, f"missing seeded topic {required}"


def test_feral_authored_bodies_are_not_credited_upstream(store):
    """We attribute the source file, but never claim upstream wrote our text."""
    notes = store.recall("https://x.example/", limit=50)["notes"]
    techniques = [n for n in notes if n["kind"] == "technique"]
    assert techniques
    assert all(n["source"] == "feral-core" for n in techniques)


# ---------------------------------------------------------------------------
# Tool Genesis bridge
# ---------------------------------------------------------------------------


def test_genesis_candidates_respect_threshold_and_never_auto_execute(store):
    for _ in range(GENESIS_CANDIDATE_THRESHOLD - 1):
        capture_failure(
            store, url="https://slow.example/app", endpoint_id="click",
            args={"ref_or_selector": "#go"},
            result={"success": False, "error": "Element not found: #go"},
        )
    assert store.genesis_candidates() == []

    capture_failure(
        store, url="https://slow.example/app", endpoint_id="click",
        args={"ref_or_selector": "#go"},
        result={"success": False, "error": "Element not found: #go"},
    )
    candidates = store.genesis_candidates()
    assert len(candidates) == 1
    c = candidates[0]
    assert c["observed_count"] == GENESIS_CANDIDATE_THRESHOLD
    assert c["requires_human_approval"] is True
    assert c["auto_execute"] is False
    assert "slow.example" in c["intent_text"]
    # Seeded techniques are reference material, not proposals.
    assert all("browser-harness" not in c["intent_text"] for c in candidates)


def test_genesis_candidates_do_not_touch_the_genesis_engine(store, monkeypatch):
    """The bridge is proposal-only: it must not construct or drive the engine."""
    import agents.tool_genesis as tg

    called = []
    monkeypatch.setattr(
        tg.ToolGenesisEngine, "generate_tool",
        lambda *a, **k: called.append("generate") or None,
    )
    monkeypatch.setattr(
        tg.ToolGenesisEngine, "execute_tool",
        lambda *a, **k: called.append("execute") or None,
    )
    for _ in range(5):
        capture_failure(
            store, url="https://x.example/a", endpoint_id="click", args={},
            result={"success": False, "error": "Element not found: #z"},
        )
    store.genesis_candidates()
    assert called == []


# ---------------------------------------------------------------------------
# Hard safety constraint: persisted knowledge is data, never code
# ---------------------------------------------------------------------------


def test_no_dynamic_execution_anywhere_in_the_new_modules():
    """Nothing here may run model- or site-authored text.

    This is the constraint the whole feature hangs on: a note may *describe*
    a workaround and may quote JS, but no code path turns stored text into
    running code. Anything that should become a real tool goes through Tool
    Genesis review.
    """
    root = Path(__file__).resolve().parent.parent
    targets = [
        root / "memory" / "browser_domain_memory.py",
        root / "memory" / "browser_domain_seeds.py",
        root / "skills" / "impl" / "browser_memory.py",
    ]
    banned = re.compile(
        r"(?<![\w.])(exec|eval|compile|__import__)\s*\(|subprocess|os\.system|os\.popen"
    )
    for path in targets:
        src = path.read_text()
        # Strip string literals and comments so prose about eval() does not
        # trip the scan; we care about call sites, not documentation.
        stripped = re.sub(r'("""|\'\'\')(.|\n)*?\1', "", src)
        stripped = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', "", stripped)
        stripped = re.sub(r"#.*", "", stripped)
        found = banned.findall(stripped)
        assert not found, f"{path.name} contains dynamic execution: {found}"


def test_stored_bodies_are_returned_as_plain_strings(store):
    """A note containing code text stays a string all the way out."""
    store.add_note(
        scope="host:evil.example", topic="note", title="payload",
        body="__import__('os').system('touch /tmp/pwned')",
    )
    note = [
        n for n in store.recall("https://evil.example/")["notes"]
        if n["title"] == "payload"
    ][0]
    assert isinstance(note["body"], str)
    assert note["body"].startswith("__import__")
    assert not Path("/tmp/pwned").exists()


# ---------------------------------------------------------------------------
# Storage location + persistence
# ---------------------------------------------------------------------------


def test_store_lives_under_feral_data_home(monkeypatch, tmp_path):
    from config.loader import feral_data_home

    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    s = get_store()
    assert Path(s.db_path) == feral_data_home() / "browser_domain_memory.db"
    assert Path(s.db_path).exists()
    # get_store re-resolves when FERAL_HOME moves, so tests do not share a DB.
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "other"))
    assert get_store().db_path != s.db_path


def test_knowledge_survives_a_new_process(tmp_path):
    path = tmp_path / "persist.db"
    a = DomainKnowledgeStore(path)
    a.add_note(scope="host:keep.example", topic="x", title="remembered", body="body")
    del a
    b = DomainKnowledgeStore(path)
    assert any(
        n["title"] == "remembered" for n in b.recall("https://keep.example/")["notes"]
    )


def test_schema_columns_are_stable(tmp_path):
    s = DomainKnowledgeStore(tmp_path / "schema.db")
    con = sqlite3.connect(s.db_path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(domain_notes)")}
    con.close()
    assert {
        "note_id", "scope_key", "scope_kind", "scope_value", "topic", "title",
        "body", "kind", "source", "license", "attribution", "upstream_excerpt",
        "confidence", "observed_count", "first_seen", "last_seen",
        "evidence_json", "tags_json",
    } <= cols


def test_forget_and_search(store):
    added = store.add_note(
        scope="host:temp.example", topic="x", title="wrong now", body="stale",
    )
    assert store.search("wrong now")
    assert store.forget(added["note_id"]) is True
    assert store.forget(added["note_id"]) is False
    assert not [n for n in store.search("wrong now") if n["title"] == "wrong now"]


def test_list_scopes_and_stats(store):
    capture_failure(
        store, url="https://a.example/x", endpoint_id="click", args={},
        result={"success": False, "error": "Element not found: #a"},
    )
    scopes = {s["scope_key"] for s in store.list_scopes()}
    assert "global" in scopes and "host:a.example" in scopes
    stats = store.stats()
    assert stats["notes"] > 0
    assert stats["by_kind"]["observation"] == 1
    assert json.dumps(stats)  # serialisable for the skill result envelope
