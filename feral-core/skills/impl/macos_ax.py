"""FERAL macOS Accessibility tree: a text snapshot of the desktop.

Why this module exists
======================
Desktop control on this machine was pure pixels. ``screen_capture`` and
``gui_computer_use`` hand back a PNG and a pair of coordinates, and the
model that has to act on them cannot see images at all in the text-only
lane. So "click the back button" became "guess an (x, y)", which is the
definition of operating blind.

The browser lane solved this years ago and does not use vision either:
``skills/impl/browser_use.py`` calls ``Accessibility.getFullAXTree`` over
CDP, prints a text tree with ``[axN]`` refs, and then acts on a ref. The
model reads structure, names a target, and the runtime resolves it.

macOS ships the same thing for native apps: the Accessibility (AX) API.
This module is the desktop equivalent of ``browser__snapshot`` /
``browser__click``:

    macos_ax__snapshot  -> [ax7] AXButton "Back" (24,105 28x28)
    macos_ax__click     -> AXPress on the element behind ax7

No screenshot, no vision model, no coordinates unless AXPress is
genuinely unavailable.

What this deliberately does differently from the browser lane
============================================================
``browser_use._build_aria_text`` truncates at a hard ``nodes[:500]`` with
no filter and no pagination. Chrome's own menu bar is 417 nodes before a
single page element is reached, and Mail or Xcode are far larger, so a
fixed cap silently drops the part the user asked about and says nothing.
Here:

* ``filter`` selects ``interactive`` (default: things with an AXPress or
  a settable value) or ``all``.
* ``max_nodes`` + ``offset`` paginate over the *full* match list, and the
  envelope reports ``total_matched`` / ``truncated`` / ``next_offset``,
  so a truncation is a fact the model can act on rather than a silent
  cut.
* The menu bar is excluded by default (``include_menus``), because it is
  the single largest and least relevant subtree in nearly every app.
* Every walk is bounded three ways: wall clock (``timeout_s``), visited
  element count (``MAX_VISITED_ELEMENTS``), and depth (``max_depth``).
  A hung app cannot stall an orchestrator turn, and an AX tree with a
  parent/child cycle cannot loop forever (identity set, see ``_walk``).

Permission story
================
The whole API is gated on one TCC grant: Accessibility. When it is not
granted, ``AXUIElementCopyAttributeValue`` does not fail loudly, it
returns ``kAXErrorAPIDisabled`` (-25211) on every call, which without
special handling looks exactly like "this app has no UI".

So every endpoint pre-checks ``AXIsProcessTrusted()`` and returns

    {"success": false, "status_code": 403, "error": "tcc_denied:accessibility"}

which is the exact contract ``agents/tcc_card.parse_tcc_error`` reads to
mint the SDUI permission card, and the same token
``skills/desktop_control/applescript.py`` returns for an Accessibility
denial coming back through the AppleScript lane. A -25211 seen mid-walk
is mapped to the same token, because the grant can be revoked between the
pre-check and the call.

Safety
======
* ``AXSecureTextField`` (password fields) are never pressed and never
  written to, and their values are never read into a snapshot.
* Refs are validated before they are acted on: role is re-read from the
  live element and compared to what the snapshot recorded. A recycled or
  dead element returns 409 telling the model to re-snapshot, rather than
  pressing whatever now occupies that slot.
* Nothing here reads pixels, so this module needs no Screen Recording
  grant.

Untrusted content
=================
An AX tree contains whatever the app renders, including a web page inside
Chrome's ``AXWebArea``. Labels are third-party text: they are data to
summarise, never instructions to follow. The manifest says so in the text
the model reads.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.skill.macos_ax")

# ── tuning constants ──────────────────────────────────────────────

# Attribute order for resolving a human label. AXTitle is empty on most
# real controls: Chrome's back button carries its name in AXDescription,
# a Finder scrollbar arrow only in AXRoleDescription. Probed live on
# macOS 15; see the module docstring's rationale.
LABEL_ATTRIBUTES: tuple[str, ...] = (
    "AXTitle",
    "AXDescription",
    "AXValue",
    "AXHelp",
    "AXRoleDescription",
)

# Roles treated as interactive even when the app exposes no action for
# them. Text fields are the important case: AppKit text fields often
# publish no AXPress, but their AXValue is settable, which is how
# ``set_value`` types without synthetic keystrokes.
INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton",
    "AXMenuButton", "AXMenuItem", "AXMenuBarItem", "AXLink",
    "AXTextField", "AXTextArea", "AXComboBox", "AXSlider",
    "AXIncrementor", "AXStepper", "AXDisclosureTriangle", "AXTabGroup",
    "AXSearchField", "AXColorWell", "AXSegmentedControl",
})

# Actions that mean "a person could operate this". Deliberately not
# "has any action at all": 547 of Finder's 677 elements report some
# action, because AXCancel and AXShowMenu are published almost
# universally, so that predicate filters nothing. Measured on this
# machine: Finder 677 elements -> 324 with AXPress, 312 with AXCancel,
# 146 with AXShowMenu.
#
# AXShowMenu is excluded on purpose even though it is a real action:
# every layout AXGroup in a Chrome page publishes it, so including it
# turns the interactive filter back into "everything". It is still
# reported per element and still callable via perform_action.
ACTIONABLE_ACTIONS: frozenset[str] = frozenset({
    "AXPress", "AXConfirm", "AXPick", "AXOpen", "AXIncrement",
    "AXDecrement",
})

# Actions worth printing next to an element even though they do not by
# themselves make it interactive.
NOTEWORTHY_ACTIONS: frozenset[str] = ACTIONABLE_ACTIONS | {"AXShowMenu"}

# Actions that ACTIVATE an element, i.e. a caller asking to "click" it
# could plausibly have meant one of these.
#
# Narrower than ACTIONABLE_ACTIONS on purpose. AXIncrement and
# AXDecrement make a stepper interactive and are worth reporting, but
# nobody asking to click something means "nudge it up by one", so
# offering them as a click substitute would be misleading.
#
# Used by ``_click`` to decide whether a cursor-free route exists before
# falling back to a real mouse event. It never invokes them itself: a
# coordinate click on a Finder row selects it and AXOpen opens it, and
# this module cannot know which the caller meant.
ACTIVATING_ACTIONS: frozenset[str] = frozenset({
    "AXPress", "AXOpen", "AXConfirm", "AXPick",
})

# Roles whose AXRoleDescription ("cell", "group", "list") is a category
# name rather than a label. When one of these resolves no better
# attribute, the real name is usually in a child AXStaticText: Finder's
# sidebar rows are AXCell with roleDescription "cell" and a child
# AXStaticText "Applications", which is the only text a person could
# possibly mean by "click Applications".
GENERIC_LABEL_ROLES: frozenset[str] = frozenset({
    "AXCell", "AXRow", "AXGroup", "AXList", "AXOutline", "AXTable",
    "AXScrollArea", "AXSplitGroup", "AXToolbar", "AXTabGroup",
    "AXLayoutArea", "AXUnknown",
})

# How deep to look for that child text, and how many pieces to join.
DESCENDANT_LABEL_DEPTH = 2
DESCENDANT_LABEL_PARTS = 3

# Password fields. Never read, never pressed, never written.
SECURE_ROLES: frozenset[str] = frozenset({"AXSecureTextField"})
SECURE_SUBROLES: frozenset[str] = frozenset({"AXSecureTextField"})

DEFAULT_MAX_NODES = 200
MAX_MAX_NODES = 1000
DEFAULT_MAX_DEPTH = 30
HARD_MAX_DEPTH = 60
DEFAULT_TIMEOUT_S = 8.0
MAX_TIMEOUT_S = 30.0
DEFAULT_FIND_LIMIT = 20

# Hard ceiling on elements touched in one walk, independent of the clock.
# A malformed tree that answers instantly could otherwise produce an
# unbounded node list inside the time budget.
MAX_VISITED_ELEMENTS = 20000

# Per-message AX timeout. Without it a beachballed app blocks the calling
# thread until the 6s system default expires, once per attribute read.
AX_MESSAGING_TIMEOUT_S = 2.0

# How many refs stay resolvable. Refs are handed to the model in a
# snapshot and used in a later turn, so they have to outlive the call
# that made them; they must not grow without bound either.
REF_STORE_LIMIT = 4000

_REF_RE = re.compile(r"^\[?\s*(ax\d+)\s*\]?$", re.IGNORECASE)


# ── envelopes ─────────────────────────────────────────────────────

def _ok(data: Any) -> dict:
    return {"success": True, "status_code": 200, "data": data, "error": None}


def _fail(reason: str, status: int = 400, data: Any = None) -> dict:
    """A failure is always success=False. There is no partial success here.

    The defect this repo just fixed elsewhere was ``window_list``
    returning ``success: True`` with an empty list when the query had
    actually errored, so the model reported "no windows are open" for
    "the query failed". Every failure path in this module goes through
    here.
    """
    return {"success": False, "status_code": status, "data": data, "error": reason}


def _tcc_denied(detail: str = "") -> dict:
    """The 403 ``agents/tcc_card`` turns into a permission card.

    ``parse_tcc_error`` matches the literal prefix ``tcc_denied:`` and
    looks the remainder up in ``TCC_CATALOG``, where ``accessibility``
    deeplinks to System Settings -> Privacy & Security -> Accessibility.
    """
    return {
        "success": False,
        "status_code": 403,
        "data": {
            "permission": "accessibility",
            "settings_path": (
                "System Settings -> Privacy & Security -> Accessibility"
            ),
            "detail": detail,
        },
        "error": "tcc_denied:accessibility",
    }


# ── lazy platform binding ─────────────────────────────────────────
#
# Imported inside functions rather than at module scope so the module
# still imports (and the skill still registers, and still refuses
# honestly) on a non-macOS host. A skill that vanishes from the process
# on import error is the failure ``skills/impl/__init__`` was rewritten
# to make visible; there is no reason to create another instance of it.

class _Unsupported(RuntimeError):
    """Raised when the AX API cannot be reached on this host."""


def _ax():
    """Return the ApplicationServices module, or raise ``_Unsupported``."""
    import platform

    if platform.system() != "Darwin":
        raise _Unsupported(
            f"macos_ax is macOS-only; this host reports {platform.system()!r}."
        )
    try:
        import ApplicationServices  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on install
        raise _Unsupported(
            "pyobjc's ApplicationServices bindings are not importable "
            f"({exc}). Install the darwin extra: pip install 'pyobjc-framework-"
            "ApplicationServices'."
        ) from exc
    return ApplicationServices


def _workspace():
    import platform

    if platform.system() != "Darwin":
        raise _Unsupported(
            f"macos_ax is macOS-only; this host reports {platform.system()!r}."
        )
    try:
        from AppKit import NSWorkspace  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on install
        raise _Unsupported(
            f"pyobjc's AppKit bindings are not importable ({exc})."
        ) from exc
    return NSWorkspace.sharedWorkspace()


def accessibility_trusted() -> bool:
    """``AXIsProcessTrusted()`` for the process hosting the brain.

    Never prompts. ``AXIsProcessTrustedWithOptions`` with
    ``kAXTrustedCheckOptionPrompt`` would raise a system dialog on the
    operator's Mac from inside a tool call, which is not something a
    background turn gets to do.
    """
    return bool(_ax().AXIsProcessTrusted())


# ── low level attribute access ────────────────────────────────────

def _attr(element, name: str) -> Tuple[int, Any]:
    """``(ax_error, value)``. Never raises for an ordinary AX failure."""
    try:
        err, value = _ax().AXUIElementCopyAttributeValue(element, name, None)
    except Exception as exc:  # noqa: BLE001 - pyobjc can raise on odd types
        logger.debug("AX read %s failed: %s", name, exc)
        return -25200, None
    return int(err), value


def _str_attr(element, name: str) -> str:
    err, value = _attr(element, name)
    if err != 0 or value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    # AXValue on a slider is a number; on a checkbox an int 0/1.
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _is_api_disabled(err: int) -> bool:
    return int(err) == -25211  # kAXErrorAPIDisabled


def _role(element) -> str:
    return _str_attr(element, "AXRole")


def _subrole(element) -> str:
    return _str_attr(element, "AXSubrole")


def _is_secure(role: str, subrole: str) -> bool:
    return role in SECURE_ROLES or subrole in SECURE_SUBROLES


def _label(element, role: str, subrole: str) -> Tuple[str, str]:
    """``(label, which_attribute_supplied_it)``.

    A password field's AXValue is never consulted: on some toolkits it is
    the plaintext. Its AXTitle/AXDescription still identify it.
    """
    secure = _is_secure(role, subrole)
    for name in LABEL_ATTRIBUTES:
        if secure and name == "AXValue":
            continue
        text = _str_attr(element, name)
        if text:
            # AXRoleDescription is a last resort: "button" is not a label,
            # it is the role spelled out. Keep it only when nothing else
            # exists, which is what the ordering already guarantees.
            return text, name
    return "", ""


def _descendant_label(element, depth: int = DESCENDANT_LABEL_DEPTH) -> str:
    """Text drawn inside a container, for containers with no name of their own.

    Only called for :data:`GENERIC_LABEL_ROLES` that resolved nothing
    better than their role description, so the extra AX round-trips are
    bounded to the elements that would otherwise print as "cell".
    """
    parts: List[str] = []

    def visit(node, remaining: int) -> None:
        if remaining < 0 or len(parts) >= DESCENDANT_LABEL_PARTS:
            return
        for child in _children(node):
            if len(parts) >= DESCENDANT_LABEL_PARTS:
                return
            role = _role(child)
            subrole = _subrole(child)
            if _is_secure(role, subrole):
                continue
            for attr_name in ("AXTitle", "AXValue", "AXDescription"):
                text = _str_attr(child, attr_name)
                if text and text not in parts:
                    parts.append(text)
                    break
            visit(child, remaining - 1)

    visit(element, depth)
    return " / ".join(parts)


def _actions(element) -> List[str]:
    try:
        err, names = _ax().AXUIElementCopyActionNames(element, None)
    except Exception:  # noqa: BLE001
        return []
    if int(err) != 0 or not names:
        return []
    # Apps do publish the same action twice (Music's sidebar reports
    # AXShowMenu, AXShowMenu). Dedupe, keeping the app's ordering.
    seen: set = set()
    ordered: List[str] = []
    for name in names:
        text = str(name)
        if text not in seen:
            seen.add(text)
            ordered.append(text)
    return ordered


def _bounds(element) -> Optional[Dict[str, float]]:
    """Screen bounds as plain numbers, or None.

    AXPosition/AXSize come back as opaque ``AXValueRef`` boxes, not
    tuples. ``AXValueGetValue(box, kAXValueCGPointType, None)`` returns
    ``(ok, CGPoint)`` in pyobjc; verified live against Finder's window,
    which unpacked to x=380 y=30 w=913 h=965.
    """
    ax = _ax()
    err_p, pos = _attr(element, "AXPosition")
    err_s, size = _attr(element, "AXSize")
    if err_p != 0 or err_s != 0 or pos is None or size is None:
        return None
    try:
        ok_p, point = ax.AXValueGetValue(pos, ax.kAXValueCGPointType, None)
        ok_s, dims = ax.AXValueGetValue(size, ax.kAXValueCGSizeType, None)
    except Exception:  # noqa: BLE001
        return None
    if not ok_p or not ok_s:
        return None
    return {
        "x": float(point.x), "y": float(point.y),
        "width": float(dims.width), "height": float(dims.height),
    }


def _children(element) -> List[Any]:
    err, kids = _attr(element, "AXChildren")
    if err != 0 or not kids:
        return []
    try:
        return list(kids)
    except TypeError:
        return []


def _enabled(element) -> Optional[bool]:
    err, value = _attr(element, "AXEnabled")
    if err != 0 or value is None:
        return None
    return bool(value)


# ── the node record and the ref store ─────────────────────────────

@dataclass
class AXNode:
    ref: str
    role: str
    subrole: str
    label: str
    label_source: str
    depth: int
    actions: List[str] = field(default_factory=list)
    bounds: Optional[Dict[str, float]] = None
    enabled: Optional[bool] = None
    secure: bool = False
    window_index: Optional[int] = None
    window_title: str = ""

    def is_interactive(self) -> bool:
        if self.role in INTERACTIVE_ROLES:
            return True
        return any(a in ACTIONABLE_ACTIONS for a in self.actions)

    def to_dict(self) -> dict:
        out = {
            "ref": self.ref,
            "role": self.role,
            "label": self.label,
            "depth": self.depth,
            "actions": list(self.actions),
            "bounds": self.bounds,
        }
        if self.subrole:
            out["subrole"] = self.subrole
        if self.label_source:
            out["label_source"] = self.label_source
        if self.enabled is False:
            out["enabled"] = False
        if self.secure:
            out["secure"] = True
        if self.window_index is not None:
            out["window_index"] = self.window_index
        return out

    def to_line(self) -> str:
        indent = "  " * min(self.depth, 12)
        parts = [f"{indent}[{self.ref}] {self.role}"]
        if self.subrole and self.subrole != self.role:
            parts.append(f"<{self.subrole}>")
        if self.secure:
            parts.append('"(secure text field, value withheld)"')
        elif self.label:
            shown = self.label if len(self.label) <= 120 else self.label[:117] + "..."
            parts.append(f'"{shown}"')
        if self.bounds:
            b = self.bounds
            parts.append(
                f"({int(b['x'])},{int(b['y'])} {int(b['width'])}x{int(b['height'])})"
            )
        if self.enabled is False:
            parts.append("(disabled)")
        press = [a for a in self.actions if a in NOTEWORTHY_ACTIONS]
        if press and press != ["AXPress"]:
            parts.append("actions=" + ",".join(press))
        return " ".join(parts)


@dataclass
class _RefEntry:
    element: Any
    role: str
    label: str
    app_name: str
    pid: int
    bounds: Optional[Dict[str, float]]
    secure: bool
    created_at: float


class _RefStore:
    """Monotonic ``axN`` refs bound to live AXUIElement handles.

    Refs are never reused. A snapshot taken now and acted on two turns
    later must either resolve to the same element or say it cannot; if
    the counter reset per snapshot, ``ax7`` from an old snapshot would
    quietly press whatever ``ax7`` means in the new one, which is the
    worst possible failure for a tool that clicks things.
    """

    def __init__(self, limit: int = REF_STORE_LIMIT):
        self._entries: "OrderedDict[str, _RefEntry]" = OrderedDict()
        self._counter = 0
        self._limit = limit

    def add(self, element, node: AXNode, app_name: str, pid: int) -> str:
        self._counter += 1
        ref = f"ax{self._counter}"
        self._entries[ref] = _RefEntry(
            element=element,
            role=node.role,
            label=node.label,
            app_name=app_name,
            pid=pid,
            bounds=node.bounds,
            secure=node.secure,
            created_at=time.time(),
        )
        while len(self._entries) > self._limit:
            self._entries.popitem(last=False)
        return ref

    def get(self, ref: str) -> Optional[_RefEntry]:
        return self._entries.get(ref)

    def __len__(self) -> int:  # pragma: no cover - diagnostics only
        return len(self._entries)


_REFS = _RefStore()


def normalise_ref(raw: Any) -> str:
    """Accept ``ax7``, ``[ax7]``, ``AX7`` and ``  ax7 ``; reject the rest.

    The model copies refs straight out of the snapshot text, where they
    are printed inside brackets. Refusing ``[ax7]`` because of two
    characters it was shown would be a self-inflicted failure.
    """
    match = _REF_RE.match(str(raw or "").strip())
    return match.group(1).lower() if match else ""


# ── app + window resolution ───────────────────────────────────────

@dataclass
class _App:
    name: str
    pid: int
    bundle_id: str


def _running_apps() -> List[_App]:
    """Regular (Dock-visible) applications, in NSWorkspace order."""
    apps: List[_App] = []
    for running in _workspace().runningApplications():
        try:
            if int(running.activationPolicy()) != 0:
                continue  # agents and daemons have no user-facing UI
            apps.append(_App(
                name=str(running.localizedName() or ""),
                pid=int(running.processIdentifier()),
                bundle_id=str(running.bundleIdentifier() or ""),
            ))
        except Exception:  # noqa: BLE001
            continue
    return apps


def _frontmost_app() -> Optional[_App]:
    running = _workspace().frontmostApplication()
    if running is None:
        return None
    return _App(
        name=str(running.localizedName() or ""),
        pid=int(running.processIdentifier()),
        bundle_id=str(running.bundleIdentifier() or ""),
    )


def _resolve_app(name: Optional[str]) -> Tuple[Optional[_App], str]:
    """``(app, "")`` or ``(None, reason)``. Never guesses."""
    apps = _running_apps()
    if not name or not str(name).strip():
        front = _frontmost_app()
        if front is None:
            return None, (
                "No frontmost application. Pass `app` with the name of a "
                "running app, e.g. \"Finder\"."
            )
        return front, ""

    wanted = str(name).strip().lower()
    for app in apps:
        if app.name.lower() == wanted:
            return app, ""
    for app in apps:
        if app.bundle_id.lower() == wanted:
            return app, ""
    partial = [a for a in apps if wanted in a.name.lower()]
    if len(partial) == 1:
        return partial[0], ""
    if len(partial) > 1:
        return None, (
            f"{name!r} matches several running apps "
            f"({', '.join(sorted(a.name for a in partial))}). Use the exact name."
        )
    return None, (
        f"No running application named {name!r}. Running apps: "
        f"{', '.join(sorted(a.name for a in apps)) or '(none)'}. "
        f"Launch it first with desktop_control__open_app."
    )


def _app_element(app: _App):
    ax = _ax()
    element = ax.AXUIElementCreateApplication(app.pid)
    if element is None:
        return None
    try:
        ax.AXUIElementSetMessagingTimeout(element, AX_MESSAGING_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - advisory only
        pass
    return element


def _windows(app_element) -> Tuple[int, List[Any]]:
    """``(ax_error, windows)``. An error is NOT an empty window list."""
    err, windows = _attr(app_element, "AXWindows")
    if err != 0:
        return err, []
    if not windows:
        return 0, []
    try:
        return 0, list(windows)
    except TypeError:
        return 0, []


# ── the walk ──────────────────────────────────────────────────────

@dataclass
class _WalkBudget:
    deadline: float
    max_depth: int
    visited: set = field(default_factory=set)
    visits: int = 0
    hit_timeout: bool = False
    hit_visit_cap: bool = False
    hit_depth: bool = False
    api_disabled: bool = False

    def exhausted(self) -> bool:
        if self.visits >= MAX_VISITED_ELEMENTS:
            self.hit_visit_cap = True
            return True
        if time.monotonic() >= self.deadline:
            self.hit_timeout = True
            return True
        return False


def _walk(root, budget: _WalkBudget, depth: int = 0) -> Iterator[Tuple[Any, int]]:
    """Depth-first element walk, bounded on every axis that can run away.

    Cycle safety: AX trees are supposed to be trees, and are not. A
    ``AXChildren`` list that contains an ancestor (seen in the wild on
    toolbars that publish their overflow menu as a child of both the
    toolbar and the window) would recurse forever. ``budget.visited``
    holds element identity hashes; AXUIElementRef hashes via CFHash under
    pyobjc, verified against Finder (677 nodes, 677 unique hashes).
    """
    if depth > budget.max_depth:
        budget.hit_depth = True
        return
    if budget.exhausted():
        return

    try:
        key = hash(root)
    except Exception:  # noqa: BLE001 - unhashable handle: fall back to id()
        key = id(root)
    if key in budget.visited:
        return
    budget.visited.add(key)
    budget.visits += 1

    yield root, depth

    err, kids = _attr(root, "AXChildren")
    if _is_api_disabled(err):
        budget.api_disabled = True
        return
    if err != 0 or not kids:
        return
    try:
        child_list = list(kids)
    except TypeError:
        return
    for child in child_list:
        if budget.exhausted():
            return
        yield from _walk(child, budget, depth + 1)


def _describe_node(element, depth: int) -> AXNode:
    """Cheap description: role, label, actions, enabled.

    Bounds are deliberately NOT read here. AXPosition and AXSize are two
    more Mach round-trips per element, and on a 700-element app that is
    1 400 messages spent on elements the filter is about to discard.
    :func:`_fill_bounds` reads them for the page that is actually
    returned, which is at most ``max_nodes``.
    """
    role = _role(element) or "AXUnknown"
    subrole = _subrole(element)
    secure = _is_secure(role, subrole)
    label, source = _label(element, role, subrole)
    node = AXNode(
        ref="",
        role=role,
        subrole=subrole,
        label=label,
        label_source=source,
        depth=depth,
        actions=_actions(element),
        bounds=None,
        enabled=_enabled(element),
        secure=secure,
    )
    if (
        role in GENERIC_LABEL_ROLES
        and source in ("", "AXRoleDescription")
        and node.is_interactive()
    ):
        borrowed = _descendant_label(element)
        if borrowed:
            node.label = borrowed
            node.label_source = "AXStaticText(descendant)"
    return node


def _fill_bounds(node: AXNode, element) -> AXNode:
    node.bounds = _bounds(element)
    return node


@dataclass
class _Collected:
    nodes: List[AXNode]
    elements: List[Any]
    budget: _WalkBudget
    windows_seen: int
    window_titles: List[str]


def _collect(
    app: _App,
    *,
    include_menus: bool,
    max_depth: int,
    timeout_s: float,
    window_index: Optional[int],
) -> Tuple[Optional[_Collected], Optional[dict]]:
    """Walk the app and describe every element. ``(collected, error)``."""
    app_element = _app_element(app)
    if app_element is None:
        return None, _fail(
            f"Could not open an accessibility handle for {app.name} "
            f"(pid {app.pid}).", status=502,
        )

    budget = _WalkBudget(
        deadline=time.monotonic() + timeout_s,
        max_depth=max_depth,
    )

    err, windows = _windows(app_element)
    if _is_api_disabled(err):
        return None, _tcc_denied(
            f"Reading windows of {app.name} returned kAXErrorAPIDisabled."
        )
    if err != 0:
        return None, _fail(
            f"Could not read the window list of {app.name}: AX error {err}. "
            f"The app may still be launching, or may publish no accessibility "
            f"information at all.",
            status=502,
            data={"app": app.name, "pid": app.pid, "ax_error": err},
        )

    roots: List[Tuple[Any, Optional[int], str]] = []
    window_titles: List[str] = []
    for index, window in enumerate(windows):
        title = _str_attr(window, "AXTitle")
        window_titles.append(title)
        if window_index is not None and index != window_index:
            continue
        roots.append((window, index, title))

    if window_index is not None and not roots:
        if windows:
            reason = (
                f"{app.name} has {len(windows)} window(s); window_index "
                f"{window_index} does not exist. Valid indexes are "
                f"0..{len(windows) - 1}."
            )
        else:
            reason = (
                f"{app.name} has no open windows, so window_index "
                f"{window_index} cannot be selected."
            )
        return None, _fail(
            reason, status=404,
            data={"app": app.name, "window_count": len(windows)},
        )

    if include_menus:
        err_m, menubar = _attr(app_element, "AXMenuBar")
        if err_m == 0 and menubar is not None:
            roots.append((menubar, None, "(menu bar)"))

    if not roots:
        # No windows and menus not requested. This is a real answer, not
        # an error, but it must not read as "the app has no UI" when the
        # menu bar was simply excluded.
        return _Collected([], [], budget, len(windows), window_titles), None

    nodes: List[AXNode] = []
    elements: List[Any] = []
    for root, w_index, w_title in roots:
        for element, depth in _walk(root, budget):
            node = _describe_node(element, depth)
            node.window_index = w_index
            node.window_title = w_title
            nodes.append(node)
            elements.append(element)
        if budget.exhausted():
            break

    if budget.api_disabled:
        return None, _tcc_denied(
            f"Walking {app.name} hit kAXErrorAPIDisabled part-way through; "
            f"the Accessibility grant was revoked mid-call."
        )

    return _Collected(nodes, elements, budget, len(windows), window_titles), None


# ── coercion helpers (manifest defaults are never applied for us) ──
#
# VERIFIED in this tree: ``EndpointParam.default`` is read in exactly one
# place, ``skills/registry.py`` line ~506, and only to build the JSON
# schema shown to the model. Nothing merges defaults into ``args`` before
# dispatch. An impl that relies on a manifest default receives ``None``.

def _s(args: dict, key: str, default: str = "") -> str:
    value = args.get(key)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _i(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    value = args.get(key, None)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, parsed))


def _f(args: dict, key: str, default: float, lo: float, hi: float) -> float:
    value = args.get(key, None)
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, parsed))


def _b(args: dict, key: str, default: bool) -> bool:
    value = args.get(key, None)
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _opt_i(args: dict, key: str) -> Optional[int]:
    value = args.get(key, None)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── the skill ─────────────────────────────────────────────────────

@register_skill
class MacOSAccessibilitySkill(BaseSkill):
    """Backing implementation for the ``macos_ax`` manifest."""

    def __init__(self):
        super().__init__(skill_id="macos_ax")

    async def execute(
        self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str],
    ) -> Dict[str, Any]:
        dispatch = {
            "snapshot": self._snapshot,
            "find": self._find,
            "list_windows": self._list_windows,
            "describe": self._describe,
            "click": self._click,
            "activate": self._activate,
            "set_value": self._set_value,
            "perform_action": self._perform_action,
            "check_permission": self._check_permission,
        }
        handler = dispatch.get(str(endpoint_id))
        if handler is None:
            return _fail(
                f"Unknown macos_ax endpoint {endpoint_id!r}. Known endpoints: "
                + ", ".join(sorted(dispatch)),
                status=404,
            )
        call_args = dict(args or {})
        try:
            if str(endpoint_id) != "check_permission":
                gate = self._require_accessibility()
                if gate is not None:
                    return gate
            return await self._run(handler, call_args)
        except _Unsupported as exc:
            return _fail(str(exc), status=501)
        except Exception as exc:  # noqa: BLE001 - never crash the orchestrator
            logger.exception("macos_ax.%s failed", endpoint_id)
            return _fail(f"{endpoint_id} failed: {exc}", status=500)

    @staticmethod
    async def _run(handler, call_args: dict) -> dict:
        """Run a handler off the event loop.

        Every AX call is a synchronous Mach message to another process.
        A 700-element walk is ~700 of them, and doing that on the event
        loop thread stalls every other orchestrator task for the duration
        (0.25s for Finder, worse for a busy app).
        """
        import asyncio

        return await asyncio.to_thread(handler, call_args)

    @staticmethod
    def _require_accessibility() -> Optional[dict]:
        if accessibility_trusted():
            return None
        return _tcc_denied(
            "AXIsProcessTrusted() is False for the process hosting FERAL."
        )

    # ── reading ───────────────────────────────────────────────────

    def _snapshot(self, args: dict) -> dict:
        # Argument validation runs BEFORE the app lookup, because it is
        # the only part of this that does not depend on the host. The
        # lookup reaches the AX API, which raises _Unsupported anywhere
        # that is not macOS with pyobjc, and that surfaces as 501.
        #
        # With the old order a bad `filter` was reported as "macos_ax is
        # macOS-only" on any non-Mac, which tells the caller nothing
        # about the thing they actually got wrong, and made every
        # validation test in this surface unrunnable off a Mac: CI is
        # Linux, so `assert 501 == 400` is what it reported.
        #
        # It is also better on a Mac. When the filter and the app are
        # both wrong, "filter must be 'interactive' or 'all'" is the
        # more useful of the two answers.
        filter_mode = _s(args, "filter", "interactive").lower()
        if filter_mode not in ("interactive", "all"):
            return _fail(
                f"`filter` must be 'interactive' or 'all', got {filter_mode!r}.",
                status=400,
            )

        app_name = _s(args, "app")
        app, reason = _resolve_app(app_name or None)
        if app is None:
            return _fail(reason, status=404)
        max_nodes = _i(args, "max_nodes", DEFAULT_MAX_NODES, 1, MAX_MAX_NODES)
        offset = max(0, _i(args, "offset", 0, 0, 10 ** 6))
        max_depth = _i(args, "max_depth", DEFAULT_MAX_DEPTH, 1, HARD_MAX_DEPTH)
        timeout_s = _f(args, "timeout_s", DEFAULT_TIMEOUT_S, 0.5, MAX_TIMEOUT_S)
        include_menus = _b(args, "include_menus", False)
        window_index = _opt_i(args, "window_index")

        started = time.monotonic()
        collected, error = _collect(
            app,
            include_menus=include_menus,
            max_depth=max_depth,
            timeout_s=timeout_s,
            window_index=window_index,
        )
        if error is not None:
            return error
        assert collected is not None

        pairs = [
            (node, element)
            for node, element in zip(collected.nodes, collected.elements)
            if filter_mode == "all" or node.is_interactive()
        ]
        total = len(pairs)
        page = pairs[offset:offset + max_nodes]
        for node, element in page:
            _fill_bounds(node, element)
            node.ref = _REFS.add(element, node, app.name, app.pid)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        header = self._header(
            app, collected, filter_mode, total, offset, len(page), elapsed_ms,
            include_menus, window_index,
        )
        body = "\n".join(node.to_line() for node, _ in page)
        tree_text = header + ("\n" + body if body else "\n(no matching elements)")

        truncated = offset + len(page) < total
        return _ok({
            "app": app.name,
            "pid": app.pid,
            "bundle_id": app.bundle_id,
            "filter": filter_mode,
            "tree": tree_text,
            "nodes": [node.to_dict() for node, _ in page],
            "returned": len(page),
            "total_matched": total,
            "offset": offset,
            "next_offset": offset + len(page) if truncated else None,
            "truncated": truncated,
            "elements_visited": collected.budget.visits,
            "window_count": collected.windows_seen,
            "window_titles": collected.window_titles,
            "elapsed_ms": elapsed_ms,
            "limits_hit": self._limits_hit(collected.budget),
        })

    @staticmethod
    def _limits_hit(budget: _WalkBudget) -> List[str]:
        hit = []
        if budget.hit_timeout:
            hit.append("timeout")
        if budget.hit_visit_cap:
            hit.append("visit_cap")
        if budget.hit_depth:
            hit.append("max_depth")
        return hit

    def _header(
        self, app: _App, collected: _Collected, filter_mode: str, total: int,
        offset: int, shown: int, elapsed_ms: int, include_menus: bool,
        window_index: Optional[int],
    ) -> str:
        scope = "all windows" if window_index is None else f"window {window_index}"
        if include_menus:
            scope += " + menu bar"
        lines = [
            f"{app.name} (pid {app.pid}): {scope}, "
            f"{collected.windows_seen} window(s), filter={filter_mode}",
        ]
        if collected.window_titles:
            titled = ", ".join(
                f"[{i}] {t or '(untitled)'}"
                for i, t in enumerate(collected.window_titles)
            )
            lines.append(f"windows: {titled}")
        lines.append(
            f"showing {offset}-{offset + shown} of {total} matching elements "
            f"({collected.budget.visits} visited, {elapsed_ms}ms)"
        )
        if offset + shown < total:
            lines.append(
                f"TRUNCATED: call snapshot again with offset={offset + shown} "
                f"for the next page, or narrow with find."
            )
        limits = self._limits_hit(collected.budget)
        if limits:
            lines.append(
                f"WALK STOPPED EARLY ({', '.join(limits)}): this tree is "
                f"incomplete. Raise timeout_s/max_depth, or target one window "
                f"with window_index."
            )
        if not include_menus:
            lines.append(
                "(menu bar excluded; pass include_menus=true to see menu items)"
            )
        return "\n".join(lines)

    def _find(self, args: dict) -> dict:
        query = _s(args, "query")
        if not query:
            return _fail("`query` is required and must be non-empty.", status=400)

        app_name = _s(args, "app")
        app, reason = _resolve_app(app_name or None)
        if app is None:
            return _fail(reason, status=404)

        limit = _i(args, "limit", DEFAULT_FIND_LIMIT, 1, 200)
        max_depth = _i(args, "max_depth", DEFAULT_MAX_DEPTH, 1, HARD_MAX_DEPTH)
        timeout_s = _f(args, "timeout_s", DEFAULT_TIMEOUT_S, 0.5, MAX_TIMEOUT_S)
        include_menus = _b(args, "include_menus", True)
        filter_mode = _s(args, "filter", "interactive").lower()
        if filter_mode not in ("interactive", "all"):
            return _fail(
                f"`filter` must be 'interactive' or 'all', got {filter_mode!r}.",
                status=400,
            )

        started = time.monotonic()
        collected, error = _collect(
            app,
            include_menus=include_menus,
            max_depth=max_depth,
            timeout_s=timeout_s,
            window_index=_opt_i(args, "window_index"),
        )
        if error is not None:
            return error
        assert collected is not None

        needle = query.lower()
        scored: List[Tuple[int, AXNode, Any]] = []
        for node, element in zip(collected.nodes, collected.elements):
            if filter_mode == "interactive" and not node.is_interactive():
                continue
            haystack = node.label.lower()
            if haystack == needle:
                rank = 0
            elif haystack.startswith(needle):
                rank = 1
            elif needle in haystack:
                rank = 2
            elif needle in node.role.lower():
                rank = 3
            else:
                continue
            scored.append((rank, node, element))

        scored.sort(key=lambda item: (item[0], len(item[1].label)))
        page = scored[:limit]
        for _rank, node, element in page:
            _fill_bounds(node, element)
            node.ref = _REFS.add(element, node, app.name, app.pid)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if not page:
            # Not an error: the query ran and matched nothing. Say which
            # tree was searched so the model can widen instead of
            # concluding the control does not exist.
            return _ok({
                "app": app.name,
                "query": query,
                "matches": [],
                "match_count": 0,
                "total_matched": 0,
                "text": (
                    f"No element in {app.name} matches {query!r} "
                    f"(searched {collected.budget.visits} elements, "
                    f"filter={filter_mode}, include_menus={include_menus}). "
                    f"Try filter='all', include_menus=true, or "
                    f"macos_ax__snapshot to see what is there."
                ),
                "elements_visited": collected.budget.visits,
                "elapsed_ms": elapsed_ms,
                "limits_hit": self._limits_hit(collected.budget),
            })

        text = "\n".join(node.to_line().lstrip() for _r, node, _e in page)
        return _ok({
            "app": app.name,
            "query": query,
            "matches": [node.to_dict() for _r, node, _e in page],
            "match_count": len(page),
            "total_matched": len(scored),
            "truncated": len(scored) > len(page),
            "text": text,
            "elements_visited": collected.budget.visits,
            "elapsed_ms": elapsed_ms,
            "limits_hit": self._limits_hit(collected.budget),
        })

    def _list_windows(self, args: dict) -> dict:
        app_name = _s(args, "app")
        app, reason = _resolve_app(app_name or None)
        if app is None:
            return _fail(reason, status=404)

        app_element = _app_element(app)
        if app_element is None:
            return _fail(
                f"Could not open an accessibility handle for {app.name} "
                f"(pid {app.pid}).", status=502,
            )
        err, windows = _windows(app_element)
        if _is_api_disabled(err):
            return _tcc_denied(f"Reading windows of {app.name} was refused.")
        if err != 0:
            # The bug class this whole module is careful about: an AX
            # error is NOT "zero windows".
            return _fail(
                f"Could not read the window list of {app.name}: AX error {err}.",
                status=502,
                data={"app": app.name, "pid": app.pid, "ax_error": err},
            )

        # AXFocusedWindow is an element, not a string: reading it with
        # _str_attr would silently yield "" and report "no focused
        # window" for every app on the machine.
        err_f, focused = _attr(app_element, "AXFocusedWindow")
        focused_title = (
            _str_attr(focused, "AXTitle") if err_f == 0 and focused is not None
            else ""
        )
        rows = []
        for index, window in enumerate(windows):
            rows.append({
                "index": index,
                "title": _str_attr(window, "AXTitle"),
                "subrole": _str_attr(window, "AXSubrole"),
                "bounds": _bounds(window),
                "minimized": bool(_attr(window, "AXMinimized")[1]),
                "main": bool(_attr(window, "AXMain")[1]),
                "child_count": len(_children(window)),
            })

        lines = [f"{app.name} (pid {app.pid}): {len(rows)} window(s)"]
        for row in rows:
            bounds = row["bounds"]
            geo = (
                f"({int(bounds['x'])},{int(bounds['y'])} "
                f"{int(bounds['width'])}x{int(bounds['height'])})"
                if bounds else "(no bounds)"
            )
            flags = []
            if row["main"]:
                flags.append("main")
            if row["minimized"]:
                flags.append("minimized")
            lines.append(
                f"  [{row['index']}] {row['title'] or '(untitled)'} {geo} "
                f"{row['child_count']} direct children"
                + (f" [{', '.join(flags)}]" if flags else "")
            )
        return _ok({
            "app": app.name,
            "pid": app.pid,
            "window_count": len(rows),
            "windows": rows,
            "focused_window": focused_title,
            "text": "\n".join(lines),
        })

    def _describe(self, args: dict) -> dict:
        entry, error = self._resolve_ref(args, purpose="describe")
        if entry is None:
            return error or _fail("Unresolvable ref.", status=404)
        element = entry.element
        role = _role(element)
        subrole = _subrole(element)
        label, source = _label(element, role, subrole)
        err_names, names = (0, None)
        try:
            err_names, names = _ax().AXUIElementCopyAttributeNames(element, None)
        except Exception:  # noqa: BLE001
            names = None
        return _ok({
            "ref": normalise_ref(args.get("ref")),
            "app": entry.app_name,
            "role": role,
            "subrole": subrole,
            "label": label,
            "label_source": source,
            "actions": _actions(element),
            "bounds": _bounds(element),
            "enabled": _enabled(element),
            "focused": bool(_attr(element, "AXFocused")[1]),
            "secure": _is_secure(role, subrole),
            "value_settable": self._value_settable(element),
            "attributes": [str(n) for n in (names or [])] if err_names == 0 else [],
            "children": len(_children(element)),
        })

    def _check_permission(self, args: dict) -> dict:
        try:
            trusted = accessibility_trusted()
        except _Unsupported as exc:
            return _fail(str(exc), status=501)
        if not trusted:
            return _tcc_denied(
                "AXIsProcessTrusted() is False. Nothing in macos_ax can read "
                "or act until the grant is given."
            )
        apps = _running_apps()
        return _ok({
            "accessibility_trusted": True,
            "running_apps": sorted(a.name for a in apps),
            "frontmost_app": (_frontmost_app() or _App("", 0, "")).name,
            "text": (
                "Accessibility is granted. macos_ax can read and act on "
                f"{len(apps)} running app(s)."
            ),
        })

    # ── acting ────────────────────────────────────────────────────

    def _resolve_ref(
        self, args: dict, *, purpose: str,
    ) -> Tuple[Optional[_RefEntry], Optional[dict]]:
        """Look a ref up and prove it still points at what it pointed at.

        The check is role identity, not label: a text field's label is
        its own value and legitimately changes between the snapshot and
        the act. A role change means the element handle was recycled by
        the app, and pressing it would press something the model never
        saw.
        """
        raw = args.get("ref")
        ref = normalise_ref(raw)
        if not ref:
            return None, _fail(
                f"`ref` must be an element ref from a macos_ax snapshot or "
                f"find, like 'ax12' (got {raw!r}). Call macos_ax__snapshot "
                f"first; refs are only valid after one.",
                status=400,
            )
        entry = _REFS.get(ref)
        if entry is None:
            return None, _fail(
                f"Ref {ref!r} is not known. It was never issued, or it aged "
                f"out of the ref store (last {REF_STORE_LIMIT} refs are kept). "
                f"Take a fresh macos_ax__snapshot and use a ref from it.",
                status=404,
            )

        live_role = _role(entry.element)
        if not live_role:
            return None, _fail(
                f"Ref {ref!r} ({entry.role} \"{entry.label}\" in "
                f"{entry.app_name}) no longer resolves: the element is gone, "
                f"or its window closed. Re-snapshot and retry.",
                status=409,
                data={"ref": ref, "expected_role": entry.role},
            )
        if live_role != entry.role:
            return None, _fail(
                f"Ref {ref!r} now points at a {live_role}, but the snapshot "
                f"recorded a {entry.role} (\"{entry.label}\"). The UI changed "
                f"under the ref; {purpose} was NOT performed. Re-snapshot.",
                status=409,
                data={"ref": ref, "expected_role": entry.role,
                      "actual_role": live_role},
            )
        if entry.secure or _is_secure(live_role, _subrole(entry.element)):
            return None, _fail(
                f"Ref {ref!r} is a secure text field (a password box). "
                f"macos_ax never reads, presses or writes one. Ask the user "
                f"to type it themselves.",
                status=403,
                data={"ref": ref, "role": live_role},
            )
        return entry, None

    #: Order in which ``activate`` tries an element's actions.
    #
    #: AXPress first because it is the most specific activation and the
    #: one almost every control publishes. The rest are the activations
    #: that mean "do this element's primary thing" for the roles that do
    #: not publish AXPress at all, chiefly Finder's AXCell rows, which
    #: publish AXOpen.
    _ACTIVATION_ORDER = ("AXPress", "AXOpen", "AXConfirm", "AXPick")

    def _activate(self, args: dict) -> dict:
        """Do this element's primary thing, whatever that is.

        The capable counterpart to :meth:`_click`. ``click`` means the
        click gesture and performs AXPress, which covers 44 of Finder's
        261 interactive elements; the other 217 are AXCell rows
        publishing AXOpen and ``click`` can only refuse them with a
        pointer at ``perform_action``. Honest, but two calls where one
        would do, and only if the model knows to take the round trip.

        ``activate`` asks for the OUTCOME rather than the gesture, so
        AXOpen is a correct answer here rather than a substitution. That
        distinction is the whole reason this is a separate endpoint
        instead of a looser ``click``: a caller that means "select this
        row" still has ``click``, and a caller that means "open it" now
        has one call that does not touch the cursor.

        There is deliberately no coordinate fallback. An element with no
        activating action has no primary thing to do, and saying so is
        more useful than moving the operator's mouse and hoping.
        """
        entry, error = self._resolve_ref(args, purpose="the activation")
        if entry is None:
            return error or _fail("Unresolvable ref.", status=404)

        ref = normalise_ref(args.get("ref"))
        element = entry.element
        actions = _actions(element)

        if _enabled(element) is False:
            return _fail(
                f"{entry.role} \"{entry.label}\" is disabled, so activating it "
                f"would do nothing. Nothing was activated.",
                status=409,
                data={"ref": ref, "enabled": False},
            )

        chosen = next((a for a in self._ACTIVATION_ORDER if a in actions), None)
        if chosen is None:
            return _fail(
                f"{entry.role} \"{entry.label}\" publishes no activating action "
                f"(it has: {', '.join(actions) or 'none'}), so there is nothing "
                f"to activate. Use macos_ax__perform_action for a specific "
                f"action, or macos_ax__click if you want a real mouse click.",
                status=422,
                data={"ref": ref, "actions": actions},
            )

        err = self._perform(element, chosen)
        if err == 0:
            return _ok({
                "ref": ref,
                "method": chosen,
                "role": entry.role,
                "label": entry.label,
                "app": entry.app_name,
                "text": (
                    f"Activated {entry.role} \"{entry.label}\" in "
                    f"{entry.app_name} via {chosen}."
                ),
            })
        if _is_api_disabled(err):
            return _tcc_denied(f"{chosen} on {ref} was refused by TCC.")
        return _fail(
            f"{chosen} on {entry.role} \"{entry.label}\" failed with AX error "
            f"{err}; nothing was activated.",
            status=502,
            data={"ref": ref, "ax_error": err, "attempted": chosen},
        )

    def _click(self, args: dict) -> dict:
        entry, error = self._resolve_ref(args, purpose="the click")
        if entry is None:
            return error or _fail("Unresolvable ref.", status=404)

        ref = normalise_ref(args.get("ref"))
        allow_fallback = _b(args, "allow_coordinate_fallback", True)
        element = entry.element
        actions = _actions(element)

        if _enabled(element) is False:
            return _fail(
                f"{entry.role} \"{entry.label}\" is disabled, so pressing it "
                f"would do nothing. Nothing was clicked.",
                status=409,
                data={"ref": ref, "enabled": False},
            )

        if "AXPress" in actions:
            err = self._perform(element, "AXPress")
            if err == 0:
                return _ok({
                    "ref": ref,
                    "method": "AXPress",
                    "role": entry.role,
                    "label": entry.label,
                    "app": entry.app_name,
                    "text": f"Pressed {entry.role} \"{entry.label}\" in {entry.app_name}.",
                })
            if _is_api_disabled(err):
                return _tcc_denied(f"AXPress on {ref} was refused by TCC.")
            if not allow_fallback:
                return _fail(
                    f"AXPress on {entry.role} \"{entry.label}\" failed with AX "
                    f"error {err} and allow_coordinate_fallback is false, so "
                    f"nothing was clicked.",
                    status=502,
                    data={"ref": ref, "ax_error": err},
                )
        else:
            # No AXPress. Before reaching for the mouse, check whether
            # the element publishes some OTHER action that activates it.
            #
            # Measured during the capability audit: of Finder's 261
            # interactive elements only 44 publish AXPress. The other
            # 217 are AXCell rows publishing AXOpen, so "click
            # Applications in Finder" became a real mouse click in a
            # module whose whole premise is that it does not touch the
            # cursor.
            #
            # We deliberately do NOT press AXOpen on the caller's
            # behalf. A coordinate click on a Finder row *selects* it
            # and AXOpen *opens* it; those are different gestures and
            # this function cannot know which was meant. Guessing would
            # trade a cursor problem for a correctness problem.
            #
            # So: name the route and let the caller choose. This refusal
            # already existed and was already well worded, it was just
            # unreachable unless the caller had explicitly disabled the
            # fallback, which is not the default.
            usable = [a for a in actions if a in ACTIVATING_ACTIONS]
            if usable or not allow_fallback:
                if usable:
                    detail = (
                        f"publishes no AXPress but does publish "
                        f"{', '.join(usable)}, which can be invoked without "
                        f"moving the cursor"
                    )
                else:
                    detail = (
                        f"publishes no AXPress action "
                        f"(it has: {', '.join(actions) or 'none'})"
                    )
                return _fail(
                    f"{entry.role} \"{entry.label}\" {detail}, so nothing was "
                    f"clicked. Use macos_ax__perform_action for a different "
                    f"action.",
                    status=422,
                    data={"ref": ref, "actions": actions, "usable_actions": usable},
                )

        return self._coordinate_click(entry, ref, actions)

    def _coordinate_click(self, entry: _RefEntry, ref: str, actions: List[str]) -> dict:
        """Last resort: a real mouse click at the element's centre.

        Only reached when AXPress is absent or failed. It moves the
        operator's actual cursor and lands wherever those screen
        coordinates now are, so the envelope always reports
        ``method: "coordinate"`` rather than letting a synthetic click
        masquerade as a semantic press.
        """
        bounds = _bounds(entry.element)
        if not bounds or bounds["width"] <= 0 or bounds["height"] <= 0:
            return _fail(
                f"{entry.role} \"{entry.label}\" has no AXPress action and no "
                f"usable on-screen bounds, so there is nothing to click. "
                f"Available actions: {', '.join(actions) or 'none'}.",
                status=422,
                data={"ref": ref, "actions": actions},
            )
        x = bounds["x"] + bounds["width"] / 2.0
        y = bounds["y"] + bounds["height"] / 2.0
        try:
            import Quartz  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on install
            return _fail(
                f"Coordinate fallback needs pyobjc's Quartz bindings ({exc}).",
                status=501,
            )
        try:
            point = (x, y)
            for event_type in (
                Quartz.kCGEventMouseMoved,
                Quartz.kCGEventLeftMouseDown,
                Quartz.kCGEventLeftMouseUp,
            ):
                event = Quartz.CGEventCreateMouseEvent(
                    None, event_type, point, Quartz.kCGMouseButtonLeft,
                )
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        except Exception as exc:  # noqa: BLE001
            return _fail(
                f"Coordinate click at ({int(x)},{int(y)}) failed: {exc}",
                status=502, data={"ref": ref},
            )
        return _ok({
            "ref": ref,
            "method": "coordinate",
            "x": x, "y": y,
            "role": entry.role,
            "label": entry.label,
            "app": entry.app_name,
            "text": (
                f"{entry.role} \"{entry.label}\" exposes no working AXPress, "
                f"so a real mouse click was sent to ({int(x)},{int(y)}). "
                f"This lands on whatever is on top at that point."
            ),
        })

    @staticmethod
    def _perform(element, action: str) -> int:
        try:
            return int(_ax().AXUIElementPerformAction(element, action))
        except Exception as exc:  # noqa: BLE001
            logger.debug("AXUIElementPerformAction(%s) raised: %s", action, exc)
            return -25200

    @staticmethod
    def _value_settable(element) -> bool:
        try:
            err, settable = _ax().AXUIElementIsAttributeSettable(
                element, "AXValue", None,
            )
        except Exception:  # noqa: BLE001
            return False
        return int(err) == 0 and bool(settable)

    def _set_value(self, args: dict) -> dict:
        entry, error = self._resolve_ref(args, purpose="the value change")
        if entry is None:
            return error or _fail("Unresolvable ref.", status=404)

        if "value" not in args or args.get("value") is None:
            return _fail(
                "`value` is required. Pass the exact text to put in the field.",
                status=400,
            )
        value = str(args.get("value"))
        ref = normalise_ref(args.get("ref"))
        element = entry.element

        if not self._value_settable(element):
            return _fail(
                f"AXValue is not settable on {entry.role} \"{entry.label}\", so "
                f"nothing was typed. Read-only fields, labels and static text "
                f"cannot be written this way; click the field first, or use "
                f"gui_computer_use for synthetic keystrokes.",
                status=422,
                data={"ref": ref, "role": entry.role},
            )
        try:
            err = int(_ax().AXUIElementSetAttributeValue(element, "AXValue", value))
        except Exception as exc:  # noqa: BLE001
            return _fail(
                f"Setting AXValue on {ref} raised: {exc}", status=502,
                data={"ref": ref},
            )
        if _is_api_disabled(err):
            return _tcc_denied(f"Setting AXValue on {ref} was refused by TCC.")
        if err != 0:
            return _fail(
                f"Setting AXValue on {entry.role} \"{entry.label}\" returned AX "
                f"error {err}; the field was not changed.",
                status=502, data={"ref": ref, "ax_error": err},
            )
        readback = self._verified_readback(element, value)
        return _ok({
            "ref": ref,
            "role": entry.role,
            "label": entry.label,
            "app": entry.app_name,
            "value": value,
            "readback": readback,
            "verified": readback == value,
            "text": (
                f"Set {entry.role} \"{entry.label}\" to {value!r}"
                + ("." if readback == value else
                   f", but it now reads {readback!r}. The app rewrote or "
                   f"rejected the value.")
            ),
        })

    @staticmethod
    def _verified_readback(element, expected: str) -> str:
        """Read AXValue back, allowing for an app that updates it late.

        Chrome's web content answers the first read after a write with
        the value it held *before* the write. Reporting that as
        ``verified: false`` would tell the model "the app rejected your
        text" for a write that in fact landed, which is worse than
        useless. Two extra reads, 120ms apart, cover the observed lag;
        a value that still disagrees after that really did not take.
        """
        for attempt in range(3):
            readback = _str_attr(element, "AXValue")
            if readback == expected:
                return readback
            if attempt < 2:
                time.sleep(0.12)
        return readback

    def _perform_action(self, args: dict) -> dict:
        entry, error = self._resolve_ref(args, purpose="the action")
        if entry is None:
            return error or _fail("Unresolvable ref.", status=404)

        action = _s(args, "action")
        if not action:
            return _fail(
                "`action` is required, e.g. AXPress, AXShowMenu, AXIncrement. "
                "Call macos_ax__describe on the ref to list what it supports.",
                status=400,
            )
        ref = normalise_ref(args.get("ref"))
        available = _actions(entry.element)
        if action not in available:
            return _fail(
                f"{entry.role} \"{entry.label}\" does not support {action!r}. "
                f"It supports: {', '.join(available) or 'nothing'}.",
                status=422,
                data={"ref": ref, "actions": available},
            )
        err = self._perform(entry.element, action)
        if _is_api_disabled(err):
            return _tcc_denied(f"{action} on {ref} was refused by TCC.")
        if err != 0:
            return _fail(
                f"{action} on {entry.role} \"{entry.label}\" returned AX error "
                f"{err}; nothing happened.",
                status=502, data={"ref": ref, "ax_error": err},
            )
        return _ok({
            "ref": ref,
            "action": action,
            "role": entry.role,
            "label": entry.label,
            "app": entry.app_name,
            "text": (
                f"Performed {action} on {entry.role} \"{entry.label}\" in "
                f"{entry.app_name}."
            ),
        })


# Endpoint ids, as data, so the manifest parity test has one thing to
# compare against without importing an event loop or a Mac.
MACOS_AX_ENDPOINTS: frozenset[str] = frozenset({
    "snapshot", "find", "list_windows", "describe",
    "click", "activate", "set_value", "perform_action", "check_permission",
})


__all__ = [
    "ACTIONABLE_ACTIONS",
    "AXNode",
    "INTERACTIVE_ROLES",
    "LABEL_ATTRIBUTES",
    "MACOS_AX_ENDPOINTS",
    "MacOSAccessibilitySkill",
    "SECURE_ROLES",
    "accessibility_trusted",
    "normalise_ref",
]
