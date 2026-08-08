"""Bundled reference notes for the per-domain browser knowledge store.

These are the NON-site-specific interaction techniques: the things that are
true of shadow DOM, cross-origin iframes, dialogs, dropdowns, uploads and
drag-and-drop everywhere, so FERAL does not have to rediscover them on the
first site that uses them. They are seeded at ``global`` scope and therefore
surface on every domain, underneath anything host- or domain-specific.

Attribution
-----------
The topic set and the ``upstream_excerpt`` fields come from
**browser-use/browser-harness** (``interaction-skills/*.md``), MIT licensed,
Copyright (c) 2026 Browser Use. Excerpts are quoted verbatim and carry the
notice in :data:`BROWSER_HARNESS_MIT_NOTICE`; the same notice is written into
the database as its own row (``ensure_seeded``) so it travels with any copy
of the data, and is reproduced in ``THIRD_PARTY_NOTICES.md`` at the repo root.

The ``body`` of each note is FERAL-authored: it maps the technique onto
FERAL's actual browser endpoints (``snapshot``, ``list_iframes``,
``execute_in_iframe``, ``wait_for_selector``, ``evaluate`` ...), which do not
exist upstream. Those bodies are marked ``source="feral-core"`` and are NOT
attributed to Browser Use, because putting our words in their mouth would be
its own kind of dishonesty.

Honest scope note: several upstream files
(``shadow-dom.md``, ``dropdowns.md``, ``drag-and-drop.md``, ``iframes.md``,
``cross-origin-iframes.md``, ``scrolling.md``) are one-sentence topic stubs in
the cloned repo, not full write-ups. Their excerpts are correspondingly short.
We did not pad them out and then credit the padding upstream.
"""

from __future__ import annotations

# Bump when SEED_NOTES changes so existing databases pick up the new set.
SEED_VERSION = 3

BROWSER_HARNESS_MIT_NOTICE = """MIT License

Copyright (c) 2026 Browser Use

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


def _bh(path: str) -> str:
    return f"browser-use/browser-harness — {path} (MIT, Copyright (c) 2026 Browser Use)"


_MIT = "MIT"


SEED_NOTES: list[dict] = [
    # -- The licence notice itself, stored as data so it travels with the DB.
    {
        "scope": "global",
        "topic": "attribution",
        "title": "Third-party notice: browser-use/browser-harness (MIT)",
        "kind": "attribution",
        "source": "browser-use/browser-harness",
        "license": _MIT,
        "attribution": _bh("LICENSE"),
        "confidence": 1.0,
        "body": (
            "Reference notes in this store tagged source='browser-use/browser-harness' "
            "are derived from the browser-harness project's interaction-skills "
            "documentation, used under the MIT License. The full notice is preserved "
            "in the upstream_excerpt field of this note and in THIRD_PARTY_NOTICES.md "
            "at the root of feral-core."
        ),
        "upstream_excerpt": BROWSER_HARNESS_MIT_NOTICE,
        "tags": ["license", "attribution"],
    },
    # -- Shadow DOM -----------------------------------------------------
    {
        "scope": "global",
        "topic": "shadow_dom",
        "title": "Shadow DOM: querySelector does not pierce shadow roots",
        "kind": "technique",
        "source": "feral-core",
        "license": _MIT,
        "attribution": _bh("interaction-skills/shadow-dom.md"),
        "confidence": 0.8,
        "body": (
            "document.querySelector stops at a shadow boundary, so a control inside a "
            "web component looks like it does not exist. Symptoms in FERAL: 'Element "
            "not found:' from the CDP path, or a snapshot that shows the component but "
            "no children. Two ways through. (1) Recursive traversal via the evaluate "
            "endpoint: walk elements, and where el.shadowRoot exists, recurse into it, "
            "collecting matches. (2) Give up on DOM piercing and click by coordinate: "
            "the ARIA snapshot still reports accessible nodes inside open shadow roots "
            "with a backend node id, so hover/click by ARIA ref often works where a CSS "
            "selector cannot. Deeply nested component trees are where coordinate "
            "clicking wins on effort. Closed shadow roots are not reachable from page "
            "JS at all; coordinates are the only option there."
        ),
        "upstream_excerpt": (
            "# Shadow DOM\n\n"
            "Focus on recursive `shadowRoot` traversal, and note when coordinate "
            "clicking is simpler than piercing deeply nested component trees."
        ),
        # Tagged with the failure codes this technique explains, so the
        # failure-time lookup in api.state finds it. A selector that never
        # resolves is very often a shadow root, not a typo.
        "tags": ["shadow-dom", "selectors", "selector_not_found", "selector_timeout"],
    },
    # -- Iframes (same-origin) ------------------------------------------
    {
        "scope": "global",
        "topic": "iframes",
        "title": "Same-origin iframes: traverse via contentDocument, mind the coordinate space",
        "kind": "technique",
        "source": "feral-core",
        "license": _MIT,
        "attribution": _bh("interaction-skills/iframes.md"),
        "confidence": 0.8,
        "body": (
            "For a same-origin frame you can reach the inner document through the "
            "frame element's contentDocument / contentWindow from the evaluate "
            "endpoint, or use list_iframes + execute_in_iframe, which is the cleaner "
            "path because it does not depend on the parent's origin. The trap is "
            "coordinates: getBoundingClientRect() inside the frame returns "
            "frame-local coordinates, while mouse input dispatched at page level uses "
            "page coordinates. Add the frame's own offset before clicking by "
            "coordinate, or the click lands somewhere else entirely."
        ),
        "upstream_excerpt": (
            "# Iframes\n\n"
            "Cover same-origin iframe traversal through `contentDocument` / "
            "`contentWindow`, and keep the frame-local versus page-coordinate warning "
            "explicit for clicks."
        ),
        "tags": ["iframes", "coordinates", "selector_not_found"],
    },
    # -- Cross-origin iframes -------------------------------------------
    {
        "scope": "global",
        "topic": "cross_origin_iframe",
        "title": "Cross-origin iframes: use the frame's own target, or go through the compositor",
        "kind": "technique",
        "source": "feral-core",
        "license": _MIT,
        "attribution": _bh("interaction-skills/cross-origin-iframes.md"),
        "confidence": 0.85,
        "body": (
            "A cross-origin frame is a separate CDP target. Page JS cannot read or "
            "write it (you get a SecurityError / 'Blocked a frame with origin'), and no "
            "amount of selector cleverness changes that. In FERAL: call list_iframes, "
            "then execute_in_iframe with the frame index so the script runs in the "
            "frame's own context. When even that is awkward (deeply nested payment "
            "widgets, captcha frames), coordinate-level mouse input is lower friction, "
            "because it is delivered by the compositor and does not care about origin "
            "boundaries at all. Checkout, payment card entry and third-party auth are "
            "where this bites most often, and it is usually the hidden step that makes "
            "a checkout flow look impossible."
        ),
        "upstream_excerpt": (
            "# Cross-Origin Iframes\n\n"
            "Focus on `iframe_target(...)`, target attachment, and when "
            "compositor-level coordinate clicks are lower-friction than cross-target "
            "DOM work."
        ),
        "tags": ["iframes", "cross-origin", "checkout", "cross_origin_iframe"],
    },
    # -- Dialogs (upstream doc has real content) ------------------------
    {
        "scope": "global",
        "topic": "dialogs",
        "title": "Native dialogs freeze the page: dismiss at CDP level, or stub before",
        "kind": "technique",
        "source": "feral-core",
        "license": _MIT,
        "attribution": _bh("interaction-skills/dialogs.md"),
        "confidence": 0.9,
        "body": (
            "alert / confirm / prompt / beforeunload block the JS thread, so every "
            "subsequent evaluate or snapshot hangs or times out. If browser actions "
            "start timing out for no visible reason right after a click or a "
            "navigation, suspect a dialog. Reactive fix, and the preferred one: "
            "Page.handleJavaScriptDialog with accept true or false — it works while JS "
            "is frozen, handles beforeunload, and injects nothing into the page so it "
            "is invisible to bot detection. Proactive fix: overwrite window.alert / "
            "confirm / prompt before triggering the action. That is easier for a burst "
            "of dialogs but has three costs: the stubs are lost on navigation, "
            "confirm() then always returns true, and window.alert.toString() reveals "
            "non-native code to antibot. It also does not handle beforeunload at all."
        ),
        "upstream_excerpt": (
            "# Dialogs\n\n"
            "Browser dialogs (`alert`, `confirm`, `prompt`, `beforeunload`) freeze the "
            "JS thread. Two approaches depending on timing.\n\n"
            "## Reactive: dismiss via CDP (preferred)\n\n"
            "Works even when JS is frozen. Handles all dialog types including "
            "`beforeunload`.\n\n"
            "```python\n"
            "cdp(\"Page.handleJavaScriptDialog\", accept=True)   # accept / click OK\n"
            "cdp(\"Page.handleJavaScriptDialog\", accept=False)  # cancel / click Cancel\n"
            "```\n\n"
            "Undetectable by antibot — no JS injected into the page.\n\n"
            "## Proactive: stub via JS\n\n"
            "Tradeoffs:\n"
            "- Stubs are lost on page navigation -- must re-run the snippet\n"
            "- `confirm()` always returns `true` (auto-approves)\n"
            "- Detectable by antibot (`window.alert.toString()` reveals non-native code)\n"
            "- Does NOT handle `beforeunload`"
        ),
        # A native dialog freezes the JS thread, so the symptom the agent
        # actually sees is every following action timing out.
        "tags": ["dialogs", "cdp", "beforeunload", "selector_timeout"],
    },
    # -- Dropdowns ------------------------------------------------------
    {
        "scope": "global",
        "topic": "not_native_select",
        "title": "Dropdowns come in four kinds; only one is a real <select>",
        "kind": "technique",
        "source": "feral-core",
        "license": _MIT,
        "attribution": _bh("interaction-skills/dropdowns.md"),
        "confidence": 0.9,
        "body": (
            "Classify before acting. (1) Native <select>: select_option works. "
            "(2) Custom overlay (div/ul with role=listbox): select_option raises "
            "'Element is not a <select> element'. Click the trigger, take a FRESH "
            "snapshot because the options usually are not in the DOM until it opens, "
            "then click the option by accessible name. (3) Searchable combobox: click, "
            "type to filter, wait for the filtered list, then pick — typing the full "
            "value and pressing Enter often selects nothing. (4) Virtualized menu: only "
            "the visible window of options exists; scroll the menu container, not the "
            "page, and re-snapshot between scrolls. In all non-native cases re-measure "
            "geometry after opening: option rects appear late, and a coordinate "
            "captured before the menu opened is stale."
        ),
        "upstream_excerpt": (
            "# Dropdowns\n\n"
            "Split dropdowns into native selects, custom overlays, searchable "
            "comboboxes, and virtualized menus, and always re-measure after opening "
            "because option geometry often appears late."
        ),
        "tags": ["dropdowns", "select", "combobox", "not_native_select", "not_visible"],
    },
    # -- Uploads --------------------------------------------------------
    {
        "scope": "global",
        "topic": "uploads",
        "title": "File uploads: set the input's files, never try to drive the OS picker",
        "kind": "technique",
        "source": "feral-core",
        "license": _MIT,
        "attribution": _bh("interaction-skills/uploads.md"),
        "confidence": 0.7,
        "body": (
            "Clicking a styled 'Upload' button opens the operating system's file "
            "picker, which is outside the browser and outside CDP — the automation "
            "stalls there with no error. Do not click it. Find the underlying "
            "input[type=file] (it is usually present but hidden behind the styled "
            "label) and set its files programmatically: Playwright's set_input_files, "
            "or CDP DOM.setFileInputFiles against the node. Two follow-ups that catch "
            "people out: some sites only enable Submit after the input fires a change "
            "event, and drag-and-drop upload zones frequently have no file input at "
            "all, in which case you must synthesise a DataTransfer and dispatch a drop "
            "event at the zone. Upstream's uploads.md is a title-only stub in the "
            "cloned repo, so this body is FERAL-authored; only the topic comes from "
            "browser-harness."
        ),
        "upstream_excerpt": "# Uploads",
        "tags": ["uploads", "file-input", "not_editable"],
    },
    # -- Drag and drop --------------------------------------------------
    {
        "scope": "global",
        "topic": "drag_and_drop",
        "title": "Drag-and-drop: decide first whether it is pointer drag, HTML5 drag, or an upload",
        "kind": "technique",
        "source": "feral-core",
        "license": _MIT,
        "attribution": _bh("interaction-skills/drag-and-drop.md"),
        "confidence": 0.75,
        "body": (
            "Three different mechanisms wear the same UI. (1) Pointer-based sortables "
            "(most kanban and list reorder libraries) listen to mousedown / mousemove / "
            "mouseup and are driven by low-level input events — dispatch a press, "
            "several intermediate moves (a single jump is often ignored as it never "
            "crosses the drag threshold), then a release. (2) Native HTML5 drag-and-drop "
            "listens for dragstart/dragover/drop with a DataTransfer; synthetic mouse "
            "moves alone do nothing, you have to dispatch the drag events. (3) A 'drop "
            "files here' zone is really an upload: build a DataTransfer with the file "
            "and dispatch drop, see the uploads note. Getting this wrong looks like the "
            "drag 'not working' with no error at all, which is why classification comes "
            "before action."
        ),
        "upstream_excerpt": (
            "# Drag And Drop\n\n"
            "Focus on when drag-and-drop can be driven with low-level input events "
            "versus when the site really expects a file upload or DOM-specific drag "
            "sequence."
        ),
        "tags": ["drag-and-drop", "input-events"],
    },
    # -- Scrolling ------------------------------------------------------
    {
        "scope": "global",
        "topic": "scrolling",
        "title": "Scrolling: find which element consumes the wheel before scrolling",
        "kind": "technique",
        "source": "feral-core",
        "license": _MIT,
        "attribution": _bh("interaction-skills/scrolling.md"),
        "confidence": 0.75,
        "body": (
            "window.scrollBy moves the document, which does nothing when the content "
            "you want lives in a nested overflow container, a virtualized list, or an "
            "open dropdown menu. Identify the actual scroll owner first (walk up from "
            "the target looking for an element whose scrollHeight exceeds its "
            "clientHeight) and scroll that element. Virtualized lists additionally need "
            "a re-snapshot after every scroll step, because rows outside the window are "
            "removed from the DOM entirely — an ARIA ref captured before the scroll may "
            "no longer be attached, which surfaces as 'Element is not attached to the "
            "DOM'."
        ),
        "upstream_excerpt": (
            "# Scrolling\n\n"
            "Separate page scroll, nested containers, virtualized lists, and dropdown "
            "menus, and identify which element is actually consuming wheel events "
            "before scrolling."
        ),
        "tags": ["scrolling", "virtualized", "detached_node", "not_visible"],
    },
    # -- Screenshots / coordinates (upstream doc has real content) ------
    {
        "scope": "global",
        "topic": "coordinates",
        "title": "Screenshots are device pixels; click coordinates are CSS pixels",
        "kind": "technique",
        "source": "feral-core",
        "license": _MIT,
        "attribution": _bh("interaction-skills/screenshots.md"),
        "confidence": 0.85,
        "body": (
            "A screenshot on a 2x display is twice the size of the CSS viewport, so a "
            "coordinate read off the image is double what a click expects. Divide by "
            "window.devicePixelRatio before dispatching mouse input. Separately, some "
            "vision models reject images larger than about 2000px on a side, so long "
            "sessions on retina displays start failing on the image rather than the "
            "page. Full-page captures are much larger and slower than viewport "
            "captures — use them only when you actually need content below the fold."
        ),
        "upstream_excerpt": (
            "# Screenshots\n\n"
            "`capture_screenshot()` writes a PNG of the current viewport. The file is "
            "in **device pixels** — on a 2\u00d7 display a 2296\u00d71143 CSS viewport produces "
            "a 4592\u00d72286 PNG.\n\n"
            "1. **Click coordinates are CSS pixels.** Don't read a target off the image "
            "and pass it to `click_at_xy()` directly without dividing by "
            "`devicePixelRatio`.\n"
            "2. **Some LLMs reject images > 2000 px per side.**"
        ),
        "tags": ["screenshots", "coordinates", "devicePixelRatio"],
    },
    # -- Tabs (upstream doc has real content) ---------------------------
    {
        "scope": "global",
        "topic": "tabs",
        "title": "Attaching to a tab is not the same as showing it to the user",
        "kind": "technique",
        "source": "feral-core",
        "license": _MIT,
        "attribution": _bh("interaction-skills/tabs.md"),
        "confidence": 0.8,
        "body": (
            "switch_tab attaches the automation to a target; it does not necessarily "
            "bring that tab to the front of the user's window. If the user is watching "
            "and expects Chrome to visibly change, send Target.activateTarget as well. "
            "CDP also cannot report the left-to-right order of the tab strip, and "
            "list_tabs includes internal chrome:// targets — including an omnibox popup "
            "target that is not a page at all. Filter those out of anything shown to a "
            "human. A target reporting a 0x0 viewport usually means you attached to a "
            "non-window surface rather than a real page."
        ),
        "upstream_excerpt": (
            "# Tabs\n\n"
            "Use **CDP for control**, **UI automation for user-visible order**.\n\n"
            "## Rules that held up in practice\n\n"
            "- `switch_tab()` is **not enough** if the user expects Chrome to visibly "
            "change.\n"
            "- `Target.activateTarget` is the CDP-side \"show this tab\".\n"
            "- `list_tabs()` includes `chrome://newtab/` by default; ask for "
            "`include_chrome=False` when you want only real pages.\n"
            "- `chrome://omnibox-popup.top-chrome/` can appear as a fake page target; "
            "ignore it for user-facing tab lists.\n"
            "- If a page has `w=0 h=0`, you may be attached to the wrong target or a "
            "non-window surface.\n"
            "- For dynamic UIs, re-read element rects after opening dropdowns / modals "
            "before coordinate-clicking."
        ),
        "tags": ["tabs", "cdp"],
    },
    # -- Network as ground truth ----------------------------------------
    {
        "scope": "global",
        "topic": "network",
        "title": "When the DOM is ambiguous, the network log is the ground truth",
        "kind": "technique",
        "source": "feral-core",
        "license": _MIT,
        "attribution": _bh("interaction-skills/network-requests.md"),
        "confidence": 0.75,
        "body": (
            "Single-page apps routinely succeed without any obvious DOM change: a form "
            "submits, a row saves, a download starts, and the page looks identical. "
            "Rather than guessing from a screenshot, turn on network_monitor_start "
            "before the action and read network_log after it. A 2xx on the site's own "
            "API is a far more reliable success signal than a toast that may already "
            "have faded. This is also the cheapest way to tell 'the click did nothing' "
            "apart from 'the click worked and the UI did not update'."
        ),
        "upstream_excerpt": (
            "# Network Requests\n\n"
            "Document how to watch or infer network activity when page state is "
            "ambiguous, especially for submit flows, downloads, and SPA actions that "
            "succeed without obvious DOM changes."
        ),
        "tags": ["network", "verification", "ambiguous_selector"],
    },
]
