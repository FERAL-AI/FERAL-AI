"""Per-domain browser knowledge — the store that makes the browser get better at each site.

Why this exists
---------------
FERAL's browser controller (``skills/impl/browser_use.py``) is stateless with
respect to *which site* it is driving. Every session re-discovers the same
site-specific traps: this site's "dropdown" is a div overlay so
``page.select_option`` raises ``Element is not a <select> element``; that
site's Save button sits under a sticky cookie banner so Playwright reports
``<div id="cookie"> intercepts pointer events``. The failure is thrown away
with the tool result and the next session pays for it again.

Meanwhile ``agents/tool_genesis.py`` exists to let the brain author its own
tools. On this machine its DB holds 346 ``tool_sequences`` and 0
``generated_tools`` — verified by copying ``~/.feral/tool_genesis.db`` and
counting. It is dormant because its only input is a degenerate n-gram of
repeated tool names (the top sequence is literally
``["web_search__web_search", "web_search__web_search"]``), which carries no
information about *why* anything failed. This module supplies the missing
input: structured, per-domain observations of concrete failures.

Hard safety line
----------------
Everything persisted here is DATA. There is no code path in this module, or
in the skill that wraps it, that compiles, ``exec``s, ``eval``s or otherwise
executes any stored text. Notes may *contain* code snippets as prose (the
seeded browser-harness material does), and they are returned to the agent as
strings for it to read. Turning any of it into a running tool has to go
through ``ToolGenesisEngine``'s existing propose -> AST check -> human
approve -> promote pipeline. See ``genesis_candidates()``, which emits
*proposals* and deliberately does not call the engine.

Storage
-------
SQLite at ``feral_data_home() / "browser_domain_memory.db"``. ``feral_data_home()``
honours ``FERAL_HOME`` / ``XDG_DATA_HOME``, which is why we use it instead of
hardcoding ``~/.feral`` — the test suite's autouse ``isolate_feral_home``
fixture relies on that, and an operator who relocated their install would
otherwise get a split brain.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("feral.browser_domain_memory")

SCHEMA_VERSION = 1

# How many times we must see the same failure signature on the same scope
# before it is worth proposing to Tool Genesis. Matches
# ``agents.tool_genesis.SEQUENCE_THRESHOLD`` on purpose: a one-off failure is
# usually a bad selector from the model, a thrice-repeated one is the site.
GENESIS_CANDIDATE_THRESHOLD = 3

# Evidence is bounded so a site that fails in a loop cannot grow one row
# without limit.
MAX_EVIDENCE_SAMPLES = 5
MAX_BODY_CHARS = 8000


# ---------------------------------------------------------------------------
# Domain scoping
# ---------------------------------------------------------------------------

# A *small* bundled slice of the public suffix list. We do not ship the real
# PSL and we do not fetch it: this store must work offline, and a wrong
# registrable domain here costs us a slightly mis-scoped note, not a security
# boundary (nothing authenticates or authorises on these values). Entries are
# the multi-label suffixes that actually show up in browser automation.
_MULTI_LABEL_SUFFIXES: frozenset[str] = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk", "sch.uk",
        "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "lg.jp",
        "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
        "com.br", "net.br", "org.br", "gov.br",
        "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
        "com.hk", "org.hk", "net.hk", "edu.hk", "gov.hk",
        "com.tw", "org.tw", "net.tw", "edu.tw", "gov.tw",
        "com.sg", "com.my", "com.ph", "com.vn", "com.tr", "com.mx",
        "com.ar", "com.co", "com.pe", "com.ua", "com.pk", "com.eg",
        "com.sa", "com.ng", "com.gh", "com.kw", "com.qa",
        "co.in", "net.in", "org.in", "gov.in", "ac.in", "edu.in",
        "co.kr", "or.kr", "ne.kr", "go.kr", "re.kr",
        "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz",
        "co.za", "org.za", "net.za", "gov.za", "ac.za",
        "co.il", "org.il", "net.il", "ac.il", "gov.il",
        "co.id", "or.id", "ac.id", "go.id",
        "co.th", "in.th", "ac.th", "go.th",
        # Vendor-ish suffixes where each label really is a different owner.
        "github.io", "gitlab.io", "netlify.app", "vercel.app", "pages.dev",
        "workers.dev", "herokuapp.com", "azurewebsites.net", "appspot.com",
        "s3.amazonaws.com", "cloudfront.net", "myshopify.com", "web.app",
        "firebaseapp.com", "wordpress.com", "blogspot.com", "notion.site",
        "sharepoint.com", "salesforce.com", "force.com", "zendesk.com",
        "atlassian.net", "myjetbrains.com", "readthedocs.io", "surge.sh",
    }
)

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def normalize_host(url_or_host: str) -> str:
    """Lowercase bare hostname from a URL or a hostname, ``www.`` stripped.

    ``www.`` is dropped because ``www.example.com`` and ``example.com`` are
    the same site in every case we have hit; ``admin.shopify.com`` and other
    real subdomains are NOT dropped, see :func:`scope_chain`.
    """
    if not url_or_host:
        return ""
    raw = str(url_or_host).strip()
    if "://" in raw:
        host = urlsplit(raw).hostname or ""
    else:
        # Accept "example.com/path" and "example.com:8080" too.
        host = urlsplit("//" + raw).hostname or ""
    host = (host or "").lower().strip(".")
    if host.startswith("www.") and host.count(".") >= 2:
        host = host[4:]
    return host


def registrable_domain(url_or_host: str) -> str:
    """Best-effort eTLD+1 ("registrable domain") for a URL or hostname.

    ``https://admin.shopify.com/store/x`` -> ``shopify.com``
    ``https://shop.example.co.uk`` -> ``example.co.uk``
    ``http://localhost:3000`` -> ``localhost``
    ``http://127.0.0.1:8080`` -> ``127.0.0.1``

    Uses the bundled suffix slice above, then falls back to the last two
    labels. Not a substitute for the real public suffix list.
    """
    host = normalize_host(url_or_host)
    if not host:
        return ""
    if _IPV4_RE.match(host) or ":" in host:
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    # Longest known multi-label suffix wins ("s3.amazonaws.com" before "com").
    for depth in range(min(4, len(labels) - 1), 1, -1):
        candidate = ".".join(labels[-depth:])
        if candidate in _MULTI_LABEL_SUFFIXES:
            return ".".join(labels[-(depth + 1):])
    return ".".join(labels[-2:])


def host_scope(url_or_host: str) -> str:
    """``host:<normalized host>`` scope key, or ``""`` when there is no host."""
    host = normalize_host(url_or_host)
    return f"host:{host}" if host else ""


def domain_scope(url_or_host: str) -> str:
    """``domain:<eTLD+1>`` scope key, or ``""`` when there is no host."""
    dom = registrable_domain(url_or_host)
    return f"domain:{dom}" if dom else ""


GLOBAL_SCOPE = "global"


def scope_chain(url_or_host: str) -> list[str]:
    """Scope keys for a URL, most specific first.

    ``https://admin.shopify.com/x`` ->
        ``["host:admin.shopify.com", "domain:shopify.com", "global"]``

    Why both a host scope and a domain scope, rather than picking one:
    ``admin.shopify.com`` is a Polaris single-page admin app whose text
    inputs reject synthetic value setters, while ``shopify.com`` is a
    marketing site that shares none of that. Filing everything under the
    eTLD+1 would let admin-only quirks fire on the marketing site. Filing
    everything under the full host would fragment knowledge that genuinely is
    site-wide (login walls, cookie banners, bot gates) across every
    subdomain. So: writes from automatic capture go to the HOST scope,
    because that is the only thing we actually observed; reads walk
    host -> registrable domain -> global, so anything deliberately filed at
    the domain level still reaches every subdomain.
    """
    host = normalize_host(url_or_host)
    if not host:
        return [GLOBAL_SCOPE]
    chain = [f"host:{host}"]
    dom = registrable_domain(host)
    if dom and dom != host:
        chain.append(f"domain:{dom}")
    chain.append(GLOBAL_SCOPE)
    return chain


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureSignature:
    """A browser failure we know how to say something useful about."""

    code: str
    title: str
    # Prose the agent reads on the next visit. Never executed.
    guidance: str


# Match strings below are the literal text Playwright emits, confirmed by
# grepping the installed driver bundle
# (site-packages/playwright/driver/package/lib) rather than from memory:
#   "intercepts pointer events", "element is not visible",
#   "element is outside of the viewport", "Element is not a <select> element",
#   "Element is not an <input>, <textarea> or [contenteditable] element",
#   "Element is not attached to the DOM", "strict mode violation".
# Plus "Element not found: <sel>", which is raised by
# BrowserController._cdp_get_element_center on the CDP-only path.
_SIGNATURES: tuple[tuple[tuple[str, ...], FailureSignature], ...] = (
    (
        ("intercepts pointer events",),
        FailureSignature(
            code="click_intercepted",
            title="Click intercepted by an overlay",
            guidance=(
                "Playwright reported another element on top of the target. Something "
                "is covering it: cookie banner, sticky header, modal backdrop, or a "
                "toast. Dismiss or scroll past the overlay first, or re-read the ARIA "
                "snapshot after dismissing it. The interceptor's description is in the "
                "evidence below — it names the element that is actually on top."
            ),
        ),
    ),
    (
        ("element is not a <select> element", "not a <select>"),
        FailureSignature(
            code="not_native_select",
            title="Dropdown is a custom widget, not a native <select>",
            guidance=(
                "select_option only drives real <select> elements. This control is a "
                "div/listbox overlay. Drive it as a menu instead: click the trigger, "
                "take a fresh ARIA snapshot (the options usually do not exist in the "
                "DOM until the menu opens), then click the option by its accessible "
                "name. Re-measure after opening; option geometry appears late."
            ),
        ),
    ),
    (
        (
            "element is not an <input>, <textarea> or [contenteditable] element",
            "not an <input>",
        ),
        FailureSignature(
            code="not_editable",
            title="Target is not a fillable input",
            guidance=(
                "fill/type resolved to a wrapper element rather than the editable "
                "node. Common on design-system components that wrap the real input in "
                "a styled div. Snapshot again and target the inner input/textarea, or "
                "click to focus the control first and then type."
            ),
        ),
    ),
    (
        ("strict mode violation",),
        FailureSignature(
            code="ambiguous_selector",
            title="Selector matched more than one element",
            guidance=(
                "The selector is ambiguous on this page. Prefer the ARIA ref from a "
                "fresh snapshot, or narrow with a container. Repeated hits here mean "
                "the page renders several instances of the same control (mobile + "
                "desktop variants are the usual cause)."
            ),
        ),
    ),
    (
        ("element is not attached to the dom", "frame was detached", "detached"),
        FailureSignature(
            code="detached_node",
            title="Element or frame detached mid-interaction",
            guidance=(
                "The page re-rendered between snapshot and action. This site's DOM is "
                "not stable after load. Wait for a settled condition (wait_for_selector "
                "on the target) and re-snapshot immediately before acting, rather than "
                "reusing an ARIA ref captured earlier in the turn."
            ),
        ),
    ),
    (
        ("element is not visible", "element is outside of the viewport"),
        FailureSignature(
            code="not_visible",
            title="Element present but not visible / off-viewport",
            guidance=(
                "The node exists but is hidden or off-screen. Scroll it into view, open "
                "the collapsed section that contains it, or check whether the visible "
                "copy of this control is a different node (responsive duplicates)."
            ),
        ),
    ),
    (
        ("element is not enabled",),
        FailureSignature(
            code="not_enabled",
            title="Control disabled at interaction time",
            guidance=(
                "The control was disabled when we tried to use it. Usually a form "
                "gate: some other required field has not been filled in a way the "
                "site's framework accepts. If the field looks filled, the site may be "
                "reading framework state rather than the DOM value."
            ),
        ),
    ),
    (
        (
            "cross-origin",
            "cross origin",
            "securityerror",
            "blocked a frame with origin",
        ),
        FailureSignature(
            code="cross_origin_iframe",
            title="Content lives in a cross-origin iframe",
            guidance=(
                "Same-page JS cannot reach this content. Use list_iframes + "
                "execute_in_iframe to work inside the frame's own context, or drive it "
                "with coordinate-level input, which crosses the origin boundary because "
                "it goes through the compositor rather than the DOM. Checkout, payment "
                "and auth steps are the usual offenders."
            ),
        ),
    ),
    (
        ("element not found:",),
        FailureSignature(
            code="selector_not_found",
            title="Selector did not resolve",
            guidance=(
                "Nothing matched on the CDP path. Take an ARIA snapshot and use the "
                "returned ref instead of a hand-written CSS selector. If it repeats on "
                "this site, the control is probably inside shadow DOM or an iframe, "
                "neither of which document.querySelector reaches."
            ),
        ),
    ),
    (
        ("timeout", "waiting for"),
        FailureSignature(
            code="selector_timeout",
            title="Timed out waiting for the element",
            guidance=(
                "The element never appeared within the budget. Either the page loads it "
                "lazily (wait longer / wait for a network-settled state), or the "
                "selector is wrong. Repeated timeouts on the same target on this site "
                "mean the control is rendered behind an interaction (menu, tab, "
                "accordion) that has to happen first."
            ),
        ),
    ),
)

# Failures that tell us nothing about the *site* and must not be recorded as
# site knowledge. Recording these would fill the store with noise from a
# browser that simply is not running.
_NON_DIAGNOSTIC_MARKERS: tuple[str, ...] = (
    "browser not available",
    "cannot connect to chrome",
    "unknown browser action",
    "playwright not connected",
    "sandbox runtime not available",
    "net::err_internet_disconnected",
    "net::err_connection_refused",
    "net::err_name_not_resolved",
)

# Endpoints whose failures carry site information. ``evaluate`` is excluded on
# purpose: a JS error is usually the model's snippet being wrong, not the site.
_CAPTURED_ENDPOINTS: frozenset[str] = frozenset(
    {
        "click", "type_text", "fill", "fill_form", "hover", "select",
        "wait_for_selector", "execute_in_iframe", "navigate", "snapshot",
    }
)


def is_capturable_endpoint(endpoint_id: str) -> bool:
    """Whether a failure on this endpoint can teach us about the site.

    Exported so the hook in ``api.state`` can bail out before it does any
    work at all. Tracing, HAR, screencast/recording, screenshot and download
    endpoints are all excluded: their failures are about the harness, and the
    recording work owns those code paths.
    """
    return endpoint_id in _CAPTURED_ENDPOINTS


def classify_failure(error_text: str) -> Optional[FailureSignature]:
    """Map a raw browser error string to a known signature, or ``None``.

    ``None`` means "not diagnostic of this site" and the caller must not
    persist anything. Being stingy here is the point: a store full of
    "Browser not available" teaches the agent nothing.
    """
    if not error_text:
        return None
    low = str(error_text).lower()
    for marker in _NON_DIAGNOSTIC_MARKERS:
        if marker in low:
            return None
    for needles, sig in _SIGNATURES:
        for needle in needles:
            if needle in low:
                # "timeout"/"waiting for" is the weakest signal and sits last,
                # so a timeout that also names an interceptor is classified as
                # click_intercepted rather than a bare timeout.
                if sig.code == "selector_timeout" and "timeout" not in low:
                    continue
                return sig
    return None


def _extract_error(result: Any) -> str:
    """Pull the error string out of a browser tool result, if it failed."""
    if not isinstance(result, dict):
        return ""
    if result.get("success") is True:
        # fill_form reports partial failure while success may still be False;
        # a True success is unambiguous.
        return ""
    err = result.get("error")
    if isinstance(err, str) and err:
        return err
    failed = result.get("failed")
    if isinstance(failed, dict) and failed:
        return " | ".join(f"{k}: {v}" for k, v in list(failed.items())[:3])
    return ""


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@dataclass
class DomainNote:
    note_id: str
    scope_key: str
    topic: str
    title: str
    body: str
    kind: str = "observation"
    source: str = "feral-core"
    license: str = ""
    attribution: str = ""
    upstream_excerpt: str = ""
    confidence: float = 0.5
    observed_count: int = 1
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    evidence: list[dict] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "note_id": self.note_id,
            "scope_key": self.scope_key,
            "topic": self.topic,
            "title": self.title,
            "body": self.body,
            "kind": self.kind,
            "source": self.source,
            "confidence": self.confidence,
            "observed_count": self.observed_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "evidence": self.evidence,
            "tags": self.tags,
        }
        # Attribution fields ride along with every copy of the data. MIT
        # requires the notice to travel with substantial portions of the work,
        # and these notes ARE the work when they are seeded from
        # browser-use/browser-harness.
        if self.license:
            d["license"] = self.license
        if self.attribution:
            d["attribution"] = self.attribution
        if self.upstream_excerpt:
            d["upstream_excerpt"] = self.upstream_excerpt
        return d


def _fingerprint(scope_key: str, topic: str, title: str) -> str:
    raw = f"{scope_key}\x00{topic}\x00{title.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class DomainKnowledgeStore:
    """SQLite-backed per-domain browser knowledge.

    Thread-safe by a coarse lock plus per-call connections. The brain touches
    this from the asyncio loop and the test suite from plain threads; a shared
    connection across both was not worth the sqlite threading rules.
    """

    def __init__(self, db_path: Optional[str | Path] = None):
        if db_path is None:
            from config.loader import feral_data_home

            root = feral_data_home()
            root.mkdir(parents=True, exist_ok=True)
            db_path = root / "browser_domain_memory.db"
        self.db_path = str(db_path)
        # Create the parent unconditionally, including for an explicitly
        # passed path: get_store() hands us a path under a FERAL_HOME that
        # may not exist yet, and sqlite3.connect on a missing directory fails
        # with a bare "unable to open database file".
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._lock = threading.RLock()
        self._seeded = False
        self._init_db()

    # ── schema ────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10.0)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS domain_notes (
                        note_id           TEXT PRIMARY KEY,
                        scope_key         TEXT NOT NULL,
                        scope_kind        TEXT NOT NULL,
                        scope_value       TEXT NOT NULL,
                        topic             TEXT NOT NULL,
                        title             TEXT NOT NULL,
                        body              TEXT NOT NULL,
                        kind              TEXT NOT NULL DEFAULT 'observation',
                        source            TEXT NOT NULL DEFAULT 'feral-core',
                        license           TEXT NOT NULL DEFAULT '',
                        attribution       TEXT NOT NULL DEFAULT '',
                        upstream_excerpt  TEXT NOT NULL DEFAULT '',
                        confidence        REAL NOT NULL DEFAULT 0.5,
                        observed_count    INTEGER NOT NULL DEFAULT 1,
                        first_seen        REAL NOT NULL,
                        last_seen         REAL NOT NULL,
                        evidence_json     TEXT NOT NULL DEFAULT '[]',
                        tags_json         TEXT NOT NULL DEFAULT '[]'
                    )
                    """
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_notes_scope ON domain_notes(scope_key)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_notes_topic ON domain_notes(topic)"
                )
                con.execute(
                    "CREATE TABLE IF NOT EXISTS store_meta ("
                    "  key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                con.execute(
                    "INSERT OR IGNORE INTO store_meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                con.commit()
            finally:
                con.close()

    # ── writes ────────────────────────────────────────────────────────

    def add_note(
        self,
        *,
        scope: str,
        topic: str,
        title: str,
        body: str,
        kind: str = "note",
        source: str = "feral-core",
        license: str = "",
        attribution: str = "",
        upstream_excerpt: str = "",
        confidence: float = 0.5,
        evidence: Optional[dict] = None,
        tags: Optional[Iterable[str]] = None,
    ) -> dict:
        """Insert or reinforce a note.

        ``scope`` may be a full scope key (``host:x``/``domain:x``/``global``)
        or a bare URL/host, in which case it is normalised to a HOST scope —
        see :func:`scope_chain` for why capture defaults to host.

        Re-adding the same (scope, topic, title) bumps ``observed_count`` and
        appends bounded evidence instead of duplicating the row. That counter
        is what turns a one-off into a Tool Genesis candidate.
        """
        scope_key = self._normalize_scope(scope)
        kind_part, _, value_part = scope_key.partition(":")
        if not value_part:
            kind_part, value_part = "global", ""

        title = (title or "").strip()[:300]
        body = (body or "").strip()[:MAX_BODY_CHARS]
        if not title:
            raise ValueError("title is required")

        note_id = _fingerprint(scope_key, topic, title)
        now = time.time()
        ev = dict(evidence) if isinstance(evidence, dict) else None
        tag_list = sorted({str(t) for t in (tags or [])})

        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT observed_count, evidence_json, first_seen, tags_json"
                    " FROM domain_notes WHERE note_id = ?",
                    (note_id,),
                ).fetchone()
                if row is None:
                    con.execute(
                        "INSERT INTO domain_notes (note_id, scope_key, scope_kind,"
                        " scope_value, topic, title, body, kind, source, license,"
                        " attribution, upstream_excerpt, confidence, observed_count,"
                        " first_seen, last_seen, evidence_json, tags_json)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            note_id, scope_key, kind_part, value_part, topic, title,
                            body, kind, source, license, attribution, upstream_excerpt,
                            float(confidence), 1, now, now,
                            json.dumps([ev] if ev else []), json.dumps(tag_list),
                        ),
                    )
                    created = True
                    count = 1
                else:
                    count = int(row["observed_count"]) + 1
                    try:
                        existing_ev = json.loads(row["evidence_json"]) or []
                    except Exception:
                        existing_ev = []
                    if ev:
                        existing_ev.append(ev)
                    existing_ev = existing_ev[-MAX_EVIDENCE_SAMPLES:]
                    try:
                        existing_tags = set(json.loads(row["tags_json"]) or [])
                    except Exception:
                        existing_tags = set()
                    existing_tags.update(tag_list)
                    # Confidence rises with repetition but is capped: seeing a
                    # thing 50 times does not make our *explanation* of it
                    # certain, only the observation.
                    new_conf = min(0.95, float(confidence) + 0.1 * (count - 1))
                    con.execute(
                        "UPDATE domain_notes SET body = ?, observed_count = ?,"
                        " last_seen = ?, evidence_json = ?, tags_json = ?,"
                        " confidence = ? WHERE note_id = ?",
                        (
                            body, count, now, json.dumps(existing_ev),
                            json.dumps(sorted(existing_tags)), new_conf, note_id,
                        ),
                    )
                    created = False
                con.commit()
            finally:
                con.close()

        return {
            "note_id": note_id,
            "scope_key": scope_key,
            "topic": topic,
            "title": title,
            "created": created,
            "observed_count": count,
        }

    @staticmethod
    def _normalize_scope(scope: str) -> str:
        scope = (scope or "").strip()
        if not scope or scope == GLOBAL_SCOPE:
            return GLOBAL_SCOPE
        if scope.startswith("host:") or scope.startswith("domain:"):
            prefix, _, value = scope.partition(":")
            value = normalize_host(value) if prefix == "host" else registrable_domain(value)
            return f"{prefix}:{value}" if value else GLOBAL_SCOPE
        return host_scope(scope) or GLOBAL_SCOPE

    def forget(self, note_id: str) -> bool:
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute("DELETE FROM domain_notes WHERE note_id = ?", (note_id,))
                con.commit()
                return cur.rowcount > 0
            finally:
                con.close()

    # ── reads ─────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_note(row: sqlite3.Row) -> DomainNote:
        try:
            evidence = json.loads(row["evidence_json"]) or []
        except Exception:
            evidence = []
        try:
            tags = json.loads(row["tags_json"]) or []
        except Exception:
            tags = []
        return DomainNote(
            note_id=row["note_id"],
            scope_key=row["scope_key"],
            topic=row["topic"],
            title=row["title"],
            body=row["body"],
            kind=row["kind"],
            source=row["source"],
            license=row["license"],
            attribution=row["attribution"],
            upstream_excerpt=row["upstream_excerpt"],
            confidence=row["confidence"],
            observed_count=row["observed_count"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            evidence=evidence,
            tags=tags,
        )

    def recall(
        self,
        url_or_host: str,
        *,
        limit: int = 12,
        topic: Optional[str] = None,
        include_global: bool = True,
    ) -> dict:
        """Everything FERAL knows that applies to this URL, most specific first.

        Returns ``{domain, host, scope_chain, notes, briefing}``. ``briefing``
        is a compact markdown block meant to be dropped straight into a tool
        result so the model reads it before it starts guessing selectors.
        """
        chain = scope_chain(url_or_host)
        if not include_global:
            chain = [s for s in chain if s != GLOBAL_SCOPE]
        if not chain:
            return {
                "host": "", "domain": "", "scope_chain": [],
                "notes": [], "briefing": "",
            }

        placeholders = ",".join("?" for _ in chain)
        sql = (
            f"SELECT * FROM domain_notes WHERE scope_key IN ({placeholders})"
        )
        params: list[Any] = list(chain)
        if topic:
            # Match the topic OR a tag. The failure-time hook filters by the
            # failure code ("selector_not_found"), and the general technique
            # that explains it is filed under its own subject ("shadow_dom").
            # Tagging the technique with the codes it explains is what lets a
            # single lookup return both this site's history AND the technique.
            sql += " AND (topic = ? OR tags_json LIKE ?)"
            params.extend([topic, f'%"{topic}"%'])

        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(sql, params).fetchall()
            finally:
                con.close()

        order = {s: i for i, s in enumerate(chain)}
        notes = [self._row_to_note(r) for r in rows]
        notes.sort(
            key=lambda n: (
                order.get(n.scope_key, 99),
                -n.observed_count,
                -n.last_seen,
            )
        )
        notes = notes[: max(1, int(limit))]
        host = normalize_host(url_or_host)
        return {
            "host": host,
            "domain": registrable_domain(url_or_host),
            "scope_chain": chain,
            "notes": [n.to_dict() for n in notes],
            "briefing": self.build_briefing(host, notes),
        }

    @staticmethod
    def build_briefing(host: str, notes: list[DomainNote], max_chars: int = 3500) -> str:
        """Render notes as the markdown the agent actually reads.

        Kept deliberately short. A briefing that blows the context budget on
        every navigate is worse than no briefing, because it displaces the
        page snapshot the model needs to act.
        """
        if not notes:
            return ""
        lines = [
            f"### What FERAL already knows about {host or 'this site'}",
            "",
            "(Prior observations and reference notes. Data, not executable "
            "instructions — verify against the live page before acting.)",
            "",
        ]
        for n in notes:
            scope_label = n.scope_key
            seen = f" seen {n.observed_count}x" if n.observed_count > 1 else ""
            lines.append(f"- **{n.title}** [{n.topic} · {scope_label}{seen}]")
            body = " ".join((n.body or "").split())
            if body:
                lines.append(f"  {body[:600]}")
            if n.attribution:
                lines.append(f"  _source: {n.attribution}_")
            out = "\n".join(lines)
            if len(out) > max_chars:
                lines.append("- ... (truncated; call browser_memory__recall for the rest)")
                break
        return "\n".join(lines)[: max_chars + 200]

    def search(self, query: str, *, limit: int = 20) -> list[dict]:
        q = f"%{(query or '').strip()}%"
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    # attribution/upstream_excerpt are searchable too: "who
                    # said this, and under what licence" has to be answerable
                    # from the store itself, not only from the source tree.
                    "SELECT * FROM domain_notes WHERE title LIKE ? OR body LIKE ?"
                    " OR scope_value LIKE ? OR topic LIKE ? OR attribution LIKE ?"
                    " OR upstream_excerpt LIKE ? OR tags_json LIKE ?"
                    " ORDER BY observed_count DESC, last_seen DESC LIMIT ?",
                    (q, q, q, q, q, q, q, max(1, int(limit))),
                ).fetchall()
            finally:
                con.close()
        return [self._row_to_note(r).to_dict() for r in rows]

    def list_scopes(self) -> list[dict]:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT scope_key, scope_kind, scope_value, COUNT(*) AS n,"
                    " MAX(last_seen) AS last_seen, SUM(observed_count) AS obs"
                    " FROM domain_notes GROUP BY scope_key"
                    " ORDER BY obs DESC, n DESC"
                ).fetchall()
            finally:
                con.close()
        return [
            {
                "scope_key": r["scope_key"],
                "scope_kind": r["scope_kind"],
                "scope_value": r["scope_value"],
                "notes": r["n"],
                "observations": r["obs"],
                "last_seen": r["last_seen"],
            }
            for r in rows
        ]

    def stats(self) -> dict:
        with self._lock:
            con = self._connect()
            try:
                total = con.execute("SELECT COUNT(*) FROM domain_notes").fetchone()[0]
                by_kind = {
                    r[0]: r[1]
                    for r in con.execute(
                        "SELECT kind, COUNT(*) FROM domain_notes GROUP BY kind"
                    )
                }
                scopes = con.execute(
                    "SELECT COUNT(DISTINCT scope_key) FROM domain_notes"
                ).fetchone()[0]
                obs = con.execute(
                    "SELECT COALESCE(SUM(observed_count), 0) FROM domain_notes"
                ).fetchone()[0]
            finally:
                con.close()
        return {
            "db_path": self.db_path,
            "notes": total,
            "scopes": scopes,
            "total_observations": obs,
            "by_kind": by_kind,
            "genesis_candidates": len(self.genesis_candidates()),
        }

    # ── Tool Genesis bridge (proposal only — never auto-executes) ──────

    def genesis_candidates(
        self, *, min_observations: int = GENESIS_CANDIDATE_THRESHOLD
    ) -> list[dict]:
        """Recurring site failures worth proposing as a real helper.

        This returns PROPOSAL TEXT ONLY. It does not call
        ``ToolGenesisEngine``, does not generate code, and nothing downstream
        of it may execute anything. Wiring it to generation would have to go
        through the engine's existing propose -> AST safety check -> explicit
        human approve -> promote path, and the approval step is the whole
        point. The codebase already closed a hole where a scheduled routine
        could waive a safety DENY using its own payload; an auto-generated
        browser helper that runs itself would be the same mistake with a
        different name.
        """
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT * FROM domain_notes WHERE kind = 'observation'"
                    " AND observed_count >= ? ORDER BY observed_count DESC",
                    (max(1, int(min_observations)),),
                ).fetchall()
            finally:
                con.close()
        out = []
        for r in rows:
            n = self._row_to_note(r)
            out.append(
                {
                    "note_id": n.note_id,
                    "scope_key": n.scope_key,
                    "topic": n.topic,
                    "title": n.title,
                    "observed_count": n.observed_count,
                    "intent_text": (
                        f"On {n.scope_key}, browser automation has hit "
                        f"'{n.title}' {n.observed_count} times ({n.topic}). "
                        f"Known workaround: {' '.join((n.body or '').split())[:400]}"
                    ),
                    "requires_human_approval": True,
                    "auto_execute": False,
                }
            )
        return out

    # ── seeding ───────────────────────────────────────────────────────

    def ensure_seeded(self, force: bool = False) -> dict:
        """Load the bundled reference notes exactly once per database."""
        from memory.browser_domain_seeds import SEED_NOTES, SEED_VERSION

        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT value FROM store_meta WHERE key = 'seed_version'"
                ).fetchone()
                current = row["value"] if row else ""
            finally:
                con.close()
        if current == str(SEED_VERSION) and not force:
            return {"seeded": False, "reason": "already seeded", "version": current}

        added = 0
        for seed in SEED_NOTES:
            self.add_note(**seed)
            added += 1

        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "INSERT OR REPLACE INTO store_meta (key, value) VALUES ('seed_version', ?)",
                    (str(SEED_VERSION),),
                )
                con.commit()
            finally:
                con.close()
        logger.info("browser domain memory: seeded %d reference notes (v%s)", added, SEED_VERSION)
        return {"seeded": True, "notes": added, "version": str(SEED_VERSION)}


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture_failure(
    store: DomainKnowledgeStore,
    *,
    url: str,
    endpoint_id: str,
    args: Optional[dict],
    result: Any,
) -> Optional[dict]:
    """Record a diagnostic browser failure as a per-domain observation.

    Returns the stored note summary, or ``None`` when nothing was worth
    recording (success, non-diagnostic endpoint, or an error that says
    something about the *browser* rather than the *site*).
    """
    if endpoint_id not in _CAPTURED_ENDPOINTS:
        return None
    error_text = _extract_error(result)
    if not error_text:
        return None
    sig = classify_failure(error_text)
    if sig is None:
        return None
    host = normalize_host(url)
    if not host:
        return None

    args = args or {}
    target = str(
        args.get("ref_or_selector")
        or args.get("url")
        or (list(args.get("fields", {}).keys())[0] if isinstance(args.get("fields"), dict) and args.get("fields") else "")
        or ""
    )[:200]

    try:
        return store.add_note(
            scope=host_scope(host),
            topic=sig.code,
            title=sig.title,
            body=sig.guidance,
            kind="observation",
            source="feral-browser-capture",
            confidence=0.5,
            evidence={
                "endpoint": endpoint_id,
                "target": target,
                "error": str(error_text)[:600],
                "url": str(url)[:400],
                "ts": time.time(),
            },
            tags=[endpoint_id, sig.code],
        )
    except Exception as exc:  # never let bookkeeping break a browser action
        logger.debug("capture_failure failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_STORE: Optional[DomainKnowledgeStore] = None
_STORE_PATH: Optional[str] = None
_STORE_LOCK = threading.Lock()


def get_store() -> DomainKnowledgeStore:
    """Shared store for the running brain.

    Re-resolves when ``feral_data_home()`` changes, so the test suite's
    per-test ``FERAL_HOME`` isolation actually isolates instead of handing
    every test the first test's database.
    """
    global _STORE, _STORE_PATH
    from config.loader import feral_data_home

    want = str(feral_data_home() / "browser_domain_memory.db")
    with _STORE_LOCK:
        if _STORE is None or _STORE_PATH != want:
            _STORE = DomainKnowledgeStore(want)
            _STORE_PATH = want
            try:
                _STORE.ensure_seeded()
            except Exception as exc:
                logger.warning("browser domain memory seeding failed: %s", exc)
        return _STORE
