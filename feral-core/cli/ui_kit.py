"""Shared CLI UI primitives — InquirerPy + Rich, with non-tty fallback.

Single source of truth for prompt UX across every ``feral`` subcommand
(``feral setup``, ``feral install``, ``feral key``, ``feral access``,
``feral doctor``). Call sites never touch InquirerPy or Rich directly,
so the brand chrome (raccoon emoji, brand colour, panels) stays
consistent and the non-tty fallback path is exercised in one place.

Truthfulness rules
------------------
* When ``InquirerPy`` is not installed OR ``stdin``/``stdout`` is not a
  TTY, every prompt falls back to plain ``input``/``getpass`` and the
  prompt label is annotated so the operator can see they're in the
  silent path. We never pretend to mask characters when we cannot.
* ``brand_panel`` and ``banner_line`` degrade to plain text when Rich
  is unavailable; they never raise.
* ``warn_non_interactive_setup_hint`` prints the exact ``ssh -t``
  invocation needed when the wizard is launched without a controlling
  TTY, instead of silently falling back to a degraded UX.

Asyncio nested-loop fix
-----------------------
The wizard runs inside ``asyncio.run(_run_async())`` so every prompt
call lands while an event loop is already running. ``prompt_toolkit``
(which InquirerPy wraps) detects the running loop and returns a
coroutine from ``Application.run()`` instead of blocking — that broke
v2026.5.22 where the prompts silently fell back to the typed numeric
fallback. ``_run_inquirer_safely`` detects this case and runs the
prompt in a worker thread that has no event loop of its own, so
prompt_toolkit's normal blocking path works. When called from a sync
context (no running loop) we bypass the thread entirely.

The mouse
---------
``pick`` is the one prompt built directly on ``prompt_toolkit`` rather
than on InquirerPy, because ``inquirer.select`` exposes no way to reach
``Application(mouse_support=...)`` and a wizard the operator cannot
click in is the thing this was asked to fix. See ``_MousePicker``.

Mouse capture is on by default and turned off with
``FERAL_SETUP_MOUSE=0``. It is opt-out rather than opt-in because most
operators want the click; it is opt-*able* because while a picker with
mouse reporting on is on screen the terminal sends drag gestures to the
application instead of selecting text, so the usual drag-to-select /
copy of a provider name or a model id does not work. That is a real
cost, and an operator who is copying rather than clicking should be
able to pay nothing for a feature they are not using.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import sys
import threading
from typing import Any, Callable, Optional, Sequence, Union

logger = logging.getLogger("feral.cli.ui_kit")

try:
    from rich.console import Console
    from rich.panel import Panel

    _RICH_AVAILABLE = True
except Exception:  # pragma: no cover - rich is a hard dep but guard anyway
    Console = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    _RICH_AVAILABLE = False

try:
    from InquirerPy import inquirer  # type: ignore
    from InquirerPy.base.control import Choice  # type: ignore

    _INQUIRER_AVAILABLE = True
except Exception:  # pragma: no cover - InquirerPy is the new dep; allow tests to run without it
    inquirer = None  # type: ignore[assignment]
    Choice = None  # type: ignore[assignment]
    _INQUIRER_AVAILABLE = False

try:
    from prompt_toolkit.application import Application  # type: ignore
    from prompt_toolkit.data_structures import Point  # type: ignore
    from prompt_toolkit.formatted_text import FormattedText  # type: ignore
    from prompt_toolkit.key_binding import KeyBindings  # type: ignore
    from prompt_toolkit.layout import HSplit, Layout, Window  # type: ignore
    from prompt_toolkit.layout.controls import FormattedTextControl  # type: ignore
    from prompt_toolkit.mouse_events import MouseEventType  # type: ignore
    from prompt_toolkit.shortcuts import print_formatted_text  # type: ignore
    from prompt_toolkit.styles import Style  # type: ignore

    _PROMPT_TOOLKIT_AVAILABLE = True
except Exception:  # pragma: no cover - prompt_toolkit ships with InquirerPy
    Application = None  # type: ignore[assignment]
    Point = None  # type: ignore[assignment]
    FormattedText = None  # type: ignore[assignment]
    KeyBindings = None  # type: ignore[assignment]
    HSplit = Layout = Window = None  # type: ignore[assignment]
    FormattedTextControl = None  # type: ignore[assignment]
    MouseEventType = None  # type: ignore[assignment]
    print_formatted_text = None  # type: ignore[assignment]
    Style = None  # type: ignore[assignment]
    _PROMPT_TOOLKIT_AVAILABLE = False


BRAND_EMOJI = "🦝"
BRAND_COLOR = "cyan"


# ---------------------------------------------------------------------------
# Console / TTY helpers
# ---------------------------------------------------------------------------


def _is_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def is_interactive() -> bool:
    """Public alias — used by callers that want to gate features by TTY."""
    return _is_interactive()


def is_inquirer_available() -> bool:
    return bool(_INQUIRER_AVAILABLE)


class _FallbackConsole:
    def print(self, *args, **kwargs) -> None:
        text_out = " ".join(str(a) for a in args)
        sys.stdout.write(text_out + "\n")
        sys.stdout.flush()


def get_console():
    if _RICH_AVAILABLE:
        return Console()
    return _FallbackConsole()


# ---------------------------------------------------------------------------
# Brand chrome
# ---------------------------------------------------------------------------


def brand_panel(
    title: str,
    body: str = "",
    *,
    console=None,
    border_style: str = BRAND_COLOR,
) -> None:
    """Render a Rich panel with the raccoon emoji prefix.

    Falls back to a plain hr-bracketed block when Rich is unavailable
    so callers can use this primitive everywhere without conditionals.
    """
    console = console or get_console()
    titled = f"{BRAND_EMOJI}  {title}"
    if _RICH_AVAILABLE and Panel is not None:
        console.print(Panel.fit(body or "", title=titled, border_style=border_style))
        return
    bar = "─" * max(20, len(titled) + 4)
    console.print(bar)
    console.print(titled)
    if body:
        console.print(bar)
        console.print(body)
    console.print(bar)


def banner_line(
    message: str,
    *,
    style: str = BRAND_COLOR,
    console=None,
) -> None:
    """Single-line raccoon-prefixed status message."""
    console = console or get_console()
    if _RICH_AVAILABLE:
        console.print(f"[{style}]{BRAND_EMOJI}[/]  {message}")
    else:
        console.print(f"{BRAND_EMOJI}  {message}")


# ---------------------------------------------------------------------------
# ``feral start`` chrome — shared with ``feral serve`` and the launchd
# foreground entrypoint so every boot path renders the same brand panel
# instead of the legacy ASCII box.
# ---------------------------------------------------------------------------


def print_start_banner(
    *,
    port: int,
    tls: bool,
    bind_host: Optional[str] = None,
    console=None,
) -> None:
    """Boot banner for ``feral start`` / ``feral serve``.

    Renders the same Rich ``Panel`` chrome as the setup wizard's
    Welcome screen so the brand styling stays consistent across every
    command, instead of the legacy ``╔══ F E R A L ══╗`` ASCII box.
    """
    console = console or get_console()
    scheme = "https" if tls else "http"
    host_label = bind_host or "127.0.0.1"
    lines = [
        f"Starting brain on [{BRAND_COLOR}]{scheme}://{host_label}:{port}[/]",
    ]
    if tls:
        lines.append("[dim]TLS enabled (self-signed cert in ~/.feral/tls)[/dim]")
    body = "\n".join(lines)

    if _RICH_AVAILABLE and Panel is not None:
        console.print(
            Panel.fit(
                body,
                title=f"{BRAND_EMOJI}  F E R A L",
                border_style=BRAND_COLOR,
                padding=(1, 2),
            )
        )
        return

    bar = "─" * 40
    console.print(bar)
    console.print(f"{BRAND_EMOJI}  F E R A L")
    console.print(f"   Starting brain on {scheme}://{host_label}:{port}")
    if tls:
        console.print("   TLS enabled")
    console.print(bar)


def print_ready_panel(
    *,
    port: int,
    llm_ok: bool,
    skills_count: object = "?",
    memory_notes: object = 0,
    public_url: Optional[str] = None,
    tls: bool = False,
    console=None,
) -> None:
    """Post-boot summary card for ``feral start`` / ``feral serve``.

    Mirrors the wizard's finish screen — same panel, same brand
    color, same bullet shape. Renders an ``http://`` or ``https://``
    URL based on the ``tls`` flag so the link is clickable in modern
    terminals without scheme drift.
    """
    console = console or get_console()
    scheme = "https" if tls else "http"
    url = public_url or f"{scheme}://localhost:{port}"
    llm_label = "ready" if llm_ok else "no key (run feral key)"
    body_lines = [
        f"[bold]Dashboard:[/bold] [{BRAND_COLOR}]{url}[/]",
        f"[bold]LLM:[/bold] {llm_label}",
        f"[bold]Skills:[/bold] {skills_count}",
        f"[bold]Memory:[/bold] {memory_notes} notes",
    ]
    body = "\n".join(body_lines)

    if _RICH_AVAILABLE and Panel is not None:
        console.print(
            Panel.fit(
                body,
                title=f"{BRAND_EMOJI}  Brain ready",
                border_style=BRAND_COLOR,
                padding=(1, 2),
            )
        )
        return

    console.print(f"{BRAND_EMOJI}  Brain ready")
    for line in body_lines:
        # Strip rich tags for the fallback path.
        clean = line.replace("[bold]", "").replace("[/bold]", "")
        clean = clean.replace(f"[{BRAND_COLOR}]", "").replace("[/]", "")
        console.print(f"   {clean}")


# ---------------------------------------------------------------------------
# Asyncio nested-loop shim
# ---------------------------------------------------------------------------


def _run_inquirer_safely(builder: Callable[[], Any]) -> Any:
    """Call an InquirerPy prompt's ``.execute()`` in a context where
    prompt_toolkit will actually block.

    The wizard runs inside ``asyncio.run(_run_async())``, so when a
    step calls ``inquirer.X(...).execute()`` prompt_toolkit detects the
    already-running loop and returns a coroutine instead of blocking
    (and emits ``RuntimeWarning: coroutine 'Application.run_async' was
    never awaited``). To get the normal blocking semantics back we run
    the builder in a worker thread that has no event loop bound to it.
    The main thread then ``done.wait()``s the worker — that intentionally
    blocks the asyncio loop, which is fine because the wizard step is
    the only thing happening at that point.
    """
    try:
        asyncio.get_running_loop()
        nested = True
    except RuntimeError:
        nested = False

    if not nested:
        return builder()

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            result["v"] = builder()
        except BaseException as exc:  # noqa: BLE001 — propagate every exception type
            error["e"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_worker, name="feral-ui-prompt", daemon=True)
    worker.start()
    done.wait()
    if "e" in error:
        raise error["e"]
    return result.get("v")


# ---------------------------------------------------------------------------
# Choice normalisation
# ---------------------------------------------------------------------------


ChoiceLike = Union[str, dict, Any]


def _normalise_choices(choices: Sequence[ChoiceLike]) -> list:
    """Map our loose choice shapes into either InquirerPy Choice objects
    (when available) or plain dicts the fallback path can read."""
    out: list = []
    for c in choices:
        if isinstance(c, str):
            if _INQUIRER_AVAILABLE:
                out.append(Choice(value=c, name=c))
            else:
                out.append({"name": c, "value": c})
        elif isinstance(c, dict):
            name = c.get("name") or str(c.get("value", ""))
            value = c.get("value", name)
            if _INQUIRER_AVAILABLE:
                out.append(
                    Choice(value=value, name=name, enabled=bool(c.get("enabled", False)))
                )
            else:
                out.append({"name": name, "value": value})
        else:
            # Already a Choice or arbitrary object — pass through.
            out.append(c)
    return out


def _fallback_pairs(choices: Sequence[ChoiceLike]) -> list[tuple[str, Any]]:
    pairs: list[tuple[str, Any]] = []
    for c in choices:
        if isinstance(c, str):
            pairs.append((c, c))
        elif isinstance(c, dict):
            name = c.get("name") or str(c.get("value", ""))
            value = c.get("value", name)
            pairs.append((str(name), value))
        else:
            name = getattr(c, "name", None) or str(getattr(c, "value", c))
            value = getattr(c, "value", c)
            pairs.append((str(name), value))
    return pairs


def _fallback_select(
    message: str,
    choices: Sequence[ChoiceLike],
    *,
    default: Any = None,
) -> Any:
    pairs = _fallback_pairs(choices)
    sys.stdout.write(message + "\n")
    for i, (name, _) in enumerate(pairs, start=1):
        sys.stdout.write(f"  {i}. {name}\n")
    default_label = ""
    default_idx = None
    if default is not None:
        for i, (_, value) in enumerate(pairs, start=1):
            if value == default:
                default_idx = i
                default_label = f" [{i}]"
                break
    while True:
        sys.stdout.write(f"  Choose{default_label}: ")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if line == "":
            raise EOFError("stdin closed during select")
        line = line.strip()
        if line == "" and default_idx is not None:
            return pairs[default_idx - 1][1]
        if line.isdigit():
            idx = int(line) - 1
            if 0 <= idx < len(pairs):
                return pairs[idx][1]
        for name, value in pairs:
            if line.lower() == str(name).lower() or line.lower() == str(value).lower():
                return value
        sys.stdout.write("  Invalid choice — try again.\n")


def _normalise_default_for_checkbox(default: Any, choices: Sequence[ChoiceLike]) -> list:
    """Mark the matching choice as enabled so the user lands on it pre-marked."""
    if default is None:
        return _normalise_choices(choices)
    out: list = []
    for c in choices:
        value = c if isinstance(c, str) else (c.get("value") if isinstance(c, dict) else c)
        name = (
            c
            if isinstance(c, str)
            else (c.get("name") or str(c.get("value", ""))) if isinstance(c, dict) else str(c)
        )
        is_default = value == default
        if _INQUIRER_AVAILABLE:
            out.append(Choice(value=value, name=name, enabled=is_default))
        else:
            out.append({"name": name, "value": value, "enabled": is_default})
    return out


# ---------------------------------------------------------------------------
# The clickable picker (``pick``'s engine)
# ---------------------------------------------------------------------------


MOUSE_ENV_VAR = "FERAL_SETUP_MOUSE"

# Values that mean "do not capture the mouse". Everything else, including
# an unset or empty variable, leaves the mouse on: the click is what most
# operators want, and the opt-out exists for the minority who are copying
# text out of the prompt rather than clicking in it.
_MOUSE_OFF_VALUES = frozenset({"0", "off", "false", "no", "n", "disable", "disabled"})


def mouse_enabled() -> bool:
    """Whether ``pick`` should ask the terminal to report mouse events.

    On (the default) the operator can click a row or scroll the wheel.
    The cost is that while the picker is on screen the terminal routes
    drags to the application, so the usual click-and-drag text selection
    does not select anything and there is nothing to copy. Set
    ``FERAL_SETUP_MOUSE=0`` to get that back; every keyboard gesture is
    identical either way.
    """
    raw = os.environ.get(MOUSE_ENV_VAR, "").strip().lower()
    return raw not in _MOUSE_OFF_VALUES


_MOUSE_HINT = "click to pick"

_PICK_STYLE_RULES = {
    "qmark": "ansicyan",
    "question": "bold",
    "instruction": "ansibrightblack",
    "pointer": "ansicyan bold",
    "selected": "ansicyan bold",
    "choice": "",
}


class _MousePicker:
    """Single-select list rendered on prompt_toolkit, mouse included.

    Exists because ``InquirerPy.inquirer.select`` accepts no mouse
    parameter, while the ``prompt_toolkit.Application`` it is built on
    does. Rather than reach around InquirerPy's internals, ``pick``
    drives prompt_toolkit directly; every other prompt in this module
    is still InquirerPy's.

    Rows come in already normalised as ``(label, value)`` pairs from
    :func:`_fallback_pairs`, the *same* function the typed fallback
    renders from, so the clickable list and the typed list cannot show
    different labels or a different order. Nothing here re-derives a
    label from a choice.

    The instance is the render state: ``index`` is the cursor, and both
    the key bindings and the per-row mouse handlers mutate it. Key
    handlers reach the application through ``event.app``; mouse
    handlers are called with only a ``MouseEvent``, so they use
    ``self.app``, which :meth:`build_application` sets.
    """

    def __init__(
        self,
        message: str,
        rows: Sequence[tuple[str, Any]],
        *,
        default: Any = None,
        instruction: str = "",
        mouse: Optional[bool] = None,
    ) -> None:
        self.message = message
        self.rows: list[tuple[str, Any]] = list(rows)
        self.instruction = instruction
        self.mouse = mouse_enabled() if mouse is None else bool(mouse)
        self.app: Any = None
        self.index = 0
        self._pressed: Optional[int] = None
        # One header line above the rows; the cursor position is what
        # lets a list longer than the terminal scroll itself.
        self._header_lines = 1
        if default is not None:
            for i, (_label, value) in enumerate(self.rows):
                if value == default:
                    self.index = i
                    break

    # -- content ---------------------------------------------------------

    def row_labels(self) -> list[str]:
        return [label for label, _value in self.rows]

    def header_text(self) -> str:
        parts = [f"{BRAND_EMOJI}  {self.message}"]
        hint = self.instruction
        if self.mouse:
            hint = f"{hint} · {_MOUSE_HINT}" if hint else _MOUSE_HINT
        if hint:
            parts.append(f"  {hint}")
        return "".join(parts)

    def cursor_position(self):
        return Point(x=0, y=self._header_lines + self.index)

    def fragments(self) -> list:
        """What prompt_toolkit draws, one fragment per row."""
        out: list = [
            ("class:qmark", f"{BRAND_EMOJI}  "),
            ("class:question", self.message),
        ]
        hint = self.instruction
        if self.mouse:
            hint = f"{hint} · {_MOUSE_HINT}" if hint else _MOUSE_HINT
        if hint:
            out.append(("class:instruction", f"  {hint}"))
        width = max((len(label) for label in self.row_labels()), default=0)
        for i, (label, _value) in enumerate(self.rows):
            out.append(("", "\n"))
            selected = i == self.index
            style = "class:selected" if selected else "class:choice"
            pointer = "❯ " if selected else "  "
            # The pointer, the label and the padding are one fragment so
            # the whole row is the click target, not just the glyphs.
            text = f"{pointer}{label}".ljust(width + 2)
            if self.mouse:
                out.append((style, text, self._row_mouse_handler(i)))
            else:
                out.append((style, text))
        return out

    # -- state -----------------------------------------------------------

    def move(self, delta: int) -> None:
        """Move the cursor without wrapping.

        InquirerPy ran this list with ``cycle=False``; an operator who
        holds down ↑ should land on the first row and stay there rather
        than being teleported to the bottom.
        """
        if not self.rows:
            return
        self.index = max(0, min(len(self.rows) - 1, self.index + delta))

    def accept(self, app: Any, index: Optional[int] = None) -> None:
        if index is not None:
            self.index = index
        if app is None:  # pragma: no cover - defensive
            return
        app.exit(result=self.index)

    def _invalidate(self) -> None:
        app = self.app
        if app is not None:
            try:
                app.invalidate()
            except Exception:  # pragma: no cover - redraw is best-effort
                logger.debug("picker redraw request failed", exc_info=True)

    # -- input -----------------------------------------------------------

    def _row_mouse_handler(self, index: int):
        def handler(mouse_event):
            event_type = mouse_event.event_type
            if event_type == MouseEventType.MOUSE_DOWN:
                self.index = index
                self._pressed = index
                self._invalidate()
            elif event_type == MouseEventType.MOUSE_UP:
                pressed, self._pressed = self._pressed, None
                # A release on a different row than the press is a drag
                # off the button: cancelled, the way a button behaves.
                if pressed == index:
                    self.accept(self.app, index)
            elif event_type == MouseEventType.SCROLL_DOWN:
                self.move(1)
                self._invalidate()
            elif event_type == MouseEventType.SCROLL_UP:
                self.move(-1)
                self._invalidate()
            else:
                return NotImplemented
            return None

        handler.row_index = index  # type: ignore[attr-defined]
        return handler

    def key_bindings(self):
        """Exactly the gestures the InquirerPy picker answered to.

        A picker that needs a mouse would be worse than the one it
        replaces, so the keyboard is bound first and the mouse is an
        addition to it.
        """
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("c-p")
        def _up(event):
            self.move(-1)

        @kb.add("down")
        @kb.add("c-n")
        def _down(event):
            self.move(1)

        @kb.add("enter")
        def _enter(event):
            self.accept(event.app)

        @kb.add("c-c")
        def _interrupt(event):
            # ``helpers.ask_choice`` turns this into ``QuitNavigation``,
            # which is how ctrl-c leaves the wizard cleanly.
            event.app.exit(exception=KeyboardInterrupt)

        return kb

    # -- wiring ----------------------------------------------------------

    def build_application(self, *, input=None, output=None):
        control = FormattedTextControl(
            self.fragments,
            focusable=True,
            show_cursor=False,
            get_cursor_position=self.cursor_position,
        )
        window = Window(control, always_hide_cursor=True, wrap_lines=False)
        extra: dict[str, Any] = {}
        if input is not None:
            extra["input"] = input
        if output is not None:
            extra["output"] = output
        app = Application(
            layout=Layout(HSplit([window])),
            key_bindings=self.key_bindings(),
            style=Style.from_dict(_PICK_STYLE_RULES),
            mouse_support=self.mouse,
            full_screen=False,
            erase_when_done=True,
            **extra,
        )
        self.app = app
        return app

    def echo_answer(self, index: int) -> None:
        """Leave one line behind, the way InquirerPy's ``amark`` does.

        ``erase_when_done`` wipes the list on exit, so without this the
        transcript would not record what the operator chose.
        """
        label = self.rows[index][0]
        try:
            print_formatted_text(
                FormattedText(
                    [
                        ("class:qmark", f"{BRAND_EMOJI}  "),
                        ("class:question", f"{self.message} "),
                        ("class:selected", label),
                    ]
                ),
                style=Style.from_dict(_PICK_STYLE_RULES),
                file=sys.stdout,
            )
        except Exception:  # pragma: no cover - never fail on an echo
            sys.stdout.write(f"{BRAND_EMOJI}  {self.message} {label}\n")
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# Public prompts
# ---------------------------------------------------------------------------


_SELECT_INSTRUCTION = "↑/↓ navigate · space to mark · enter to confirm"
_FUZZY_INSTRUCTION = "type to filter · ↑/↓ navigate · space to mark · enter to confirm"

# v2026.5.28 — direct-pick instructions (single press, no mark phase).
# Used by the new ``pick`` / ``fuzzy_pick`` callers below; the legacy
# ``select`` / ``fuzzy_select`` callers keep the mark-then-confirm UX
# because some flows (e.g. autonomy mode) genuinely want a confirm
# step before committing.
_PICK_INSTRUCTION = "↑/↓ navigate · enter to pick"
_FUZZY_PICK_INSTRUCTION = "type to filter · ↑/↓ navigate · enter to pick"


def _validate_single_selection(result) -> bool:
    return isinstance(result, list) and len(result) == 1


def select(
    message: str,
    choices: Sequence[ChoiceLike],
    *,
    default: Any = None,
    instruction: str = _SELECT_INSTRUCTION,
) -> Any:
    """Single-pick from a list using arrow keys + space + enter.

    Implemented on top of InquirerPy's ``checkbox`` with a
    ``len(result) == 1`` validator so the user marks exactly one item
    with space, then confirms with enter (the user's preferred UX —
    they want to *see* their pick before committing instead of
    enter-on-cursor-position semantics). Falls back to a numeric typed
    prompt off-tty.
    """
    if _INQUIRER_AVAILABLE and _is_interactive():
        try:
            normalised = _normalise_default_for_checkbox(default, choices)

            def _build():
                return inquirer.checkbox(  # type: ignore[union-attr]
                    message=message,
                    choices=normalised,
                    instruction=instruction,
                    qmark=BRAND_EMOJI,
                    amark=BRAND_EMOJI,
                    pointer="❯",
                    enabled_symbol="[*]",
                    disabled_symbol="[ ]",
                    validate=_validate_single_selection,
                    invalid_message="press space to mark exactly one option, then enter",
                    transformer=lambda r: r[0] if isinstance(r, list) and r else "",
                ).execute()

            picked = _run_inquirer_safely(_build)
            if isinstance(picked, list) and picked:
                return picked[0]
            return picked
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # pragma: no cover - last-ditch defensive log
            logger.debug("ui_kit.select InquirerPy path failed: %r", exc)
    return _fallback_select(message, choices, default=default)


_MULTI_INSTRUCTION = "↑/↓ navigate · space to toggle · enter to confirm"


def multi_select(
    message: str,
    choices: Sequence[ChoiceLike],
    *,
    instruction: str = _MULTI_INSTRUCTION,
) -> list:
    """Toggle any number of options; returns the marked values.

    ``select`` looks like a multi-select but validates down to exactly
    one pick. This is the real thing, for genuinely independent flags
    (the setup wizard's capability toggles). Pre-marked rows come from
    each choice's own ``enabled`` key, so callers express "currently
    on" per row rather than through a separate defaults argument.

    Off-TTY the fallback prints a numbered list and accepts a
    comma-separated set of indices, so a scripted run can still answer.
    """
    if _INQUIRER_AVAILABLE and _is_interactive():
        try:

            def _build():
                return inquirer.checkbox(  # type: ignore[union-attr]
                    message=message,
                    choices=list(choices),
                    instruction=instruction,
                    qmark=BRAND_EMOJI,
                    amark=BRAND_EMOJI,
                    pointer="❯",
                    enabled_symbol="[*]",
                    disabled_symbol="[ ]",
                ).execute()

            picked = _run_inquirer_safely(_build)
            return list(picked) if isinstance(picked, list) else []
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("ui_kit.multi_select InquirerPy path failed: %r", exc)

    return _fallback_multi_select(message, choices)


def _fallback_multi_select(message: str, choices: Sequence[ChoiceLike]) -> list:
    """Numbered typed multi-pick for non-TTY shells."""
    rows = [_choice_parts(c) for c in choices]
    sys.stdout.write(f"{message}\n")
    for i, (name, _value, enabled) in enumerate(rows, start=1):
        mark = "*" if enabled else " "
        sys.stdout.write(f"  [{mark}] {i}. {name}\n")
    sys.stdout.write(
        "  Enter numbers to toggle, comma-separated (blank keeps current): "
    )
    sys.stdout.flush()
    try:
        raw = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        raw = ""
    toggled = set()
    for token in (raw or "").strip().replace(" ", "").split(","):
        token = token.strip()
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(rows):
                toggled.add(idx)
    out = []
    for i, (_name, value, enabled) in enumerate(rows):
        on = (not enabled) if i in toggled else enabled
        if on:
            out.append(value)
    return out


def _choice_parts(choice: ChoiceLike) -> tuple[str, Any, bool]:
    """Normalise a choice into ``(name, value, enabled)``."""
    if isinstance(choice, dict):
        value = choice.get("value", choice.get("name"))
        return str(choice.get("name", value)), value, bool(choice.get("enabled"))
    name = getattr(choice, "name", None)
    value = getattr(choice, "value", choice)
    enabled = bool(getattr(choice, "enabled", False))
    return str(name if name is not None else choice), value, enabled


def fuzzy_select(
    message: str,
    choices: Sequence[ChoiceLike],
    *,
    default: Any = None,
    instruction: str = _FUZZY_INSTRUCTION,
) -> Any:
    """Type-to-filter single-pick (e.g. for hundreds of model ids).

    Same UX contract as ``select``: arrows navigate, space marks the
    choice, enter confirms. Implemented on top of ``inquirer.fuzzy``
    with ``multiselect=True`` + a single-selection validator.
    """
    if _INQUIRER_AVAILABLE and _is_interactive():
        try:
            normalised = _normalise_default_for_checkbox(default, choices)

            def _build():
                return inquirer.fuzzy(  # type: ignore[union-attr]
                    message=message,
                    choices=normalised,
                    instruction=instruction,
                    qmark=BRAND_EMOJI,
                    amark=BRAND_EMOJI,
                    border=True,
                    multiselect=True,
                    validate=_validate_single_selection,
                    invalid_message="press space to mark exactly one option, then enter",
                    transformer=lambda r: r[0] if isinstance(r, list) and r else "",
                ).execute()

            picked = _run_inquirer_safely(_build)
            if isinstance(picked, list) and picked:
                return picked[0]
            return picked
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # pragma: no cover
            logger.debug("ui_kit.fuzzy_select InquirerPy path failed: %r", exc)
    return _fallback_select(message, choices, default=default)


def pick(
    message: str,
    choices: Sequence[ChoiceLike],
    *,
    default: Any = None,
    instruction: str = _PICK_INSTRUCTION,
) -> Any:
    """Direct single-pick with enter-on-cursor-position semantics.

    v2026.5.28 — added because the legacy ``select`` (space-to-mark +
    enter-to-confirm) confused every first-time operator coming from
    the standard arrow-keys-then-enter UX. Use ``pick`` for any
    single-pick where the user's intent is "I want this one, get me
    out of this menu" — model pickers, provider pickers, yes/no/maybe
    triplets. Keep ``select`` for flows that genuinely want a mark
    step before commit.

    v2026.9.1. The only prompt in this module not built on InquirerPy.
    ``inquirer.select`` exposes no mouse parameter, so the operator
    could not click a row; the ``prompt_toolkit.Application`` under it
    accepts ``mouse_support``, so ``pick`` drives prompt_toolkit
    directly through :class:`_MousePicker` instead. Keyboard gestures
    are unchanged: ↑/↓ (and ctrl-p / ctrl-n) move without wrapping,
    enter takes the row under the cursor, ctrl-c raises
    ``KeyboardInterrupt``. Clicking a row picks it and the wheel
    scrolls, unless ``FERAL_SETUP_MOUSE=0`` (see :func:`mouse_enabled`
    for what mouse capture costs).

    Falls back to the same typed numeric prompt off-tty, and on any
    failure to build or run the application. A picker that raised in
    an unusual terminal would take the whole wizard down with it.
    """
    if _PROMPT_TOOLKIT_AVAILABLE and _is_interactive():
        try:
            # Rows come from the same helper the fallback renders from,
            # so the two lists cannot drift apart.
            picker = _MousePicker(
                message,
                _fallback_pairs(choices),
                default=default,
                instruction=instruction,
            )
            if picker.rows:

                def _build_and_run():
                    # Built inside the worker thread (when there is one)
                    # so the application and its event loop share a
                    # thread, exactly as the InquirerPy prompts do.
                    return picker.build_application().run()

                index = _run_inquirer_safely(_build_and_run)
                if isinstance(index, int) and 0 <= index < len(picker.rows):
                    picker.echo_answer(index)
                    return picker.rows[index][1]
                logger.debug("ui_kit.pick got no usable selection: %r", index)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # pragma: no cover
            logger.debug("ui_kit.pick prompt_toolkit path failed: %r", exc)
    return _fallback_select(message, choices, default=default)


def fuzzy_pick(
    message: str,
    choices: Sequence[ChoiceLike],
    *,
    default: Any = None,
    instruction: str = _FUZZY_PICK_INSTRUCTION,
) -> Any:
    """Type-to-filter direct single-pick.

    v2026.5.28 — companion to ``pick`` for choice lists too long to
    scroll (e.g. 100+ LLM model ids). One keystroke filters; enter
    commits the highlighted item. No space-to-mark phase.

    Mirrors ``inquirer.fuzzy(multiselect=False, ...)``; the legacy
    ``fuzzy_select`` runs ``multiselect=True`` with a
    single-selection validator, which is the UX that confused
    operators with the "press space to mark exactly one option, then
    enter" footer.
    """
    if _INQUIRER_AVAILABLE and _is_interactive():
        try:
            normalised = _normalise_choices(choices)
            default_value = default if default in [
                getattr(c, "value", c if isinstance(c, str) else c.get("value"))
                for c in choices
            ] else None

            def _build():
                return inquirer.fuzzy(  # type: ignore[union-attr]
                    message=message,
                    choices=normalised,
                    default=default_value,
                    instruction=instruction,
                    qmark=BRAND_EMOJI,
                    amark=BRAND_EMOJI,
                    border=True,
                    multiselect=False,
                    cycle=False,
                ).execute()

            result = _run_inquirer_safely(_build)
            # InquirerPy returns the raw Choice object when callers
            # pass Choice instances; ``_fallback_pairs`` already
            # unwraps via ``.value`` (see ``_fallback_pairs`` above),
            # so do the same here so sentinel comparisons and
            # downstream string equality checks behave identically
            # regardless of which path produced the value.
            return getattr(result, "value", result)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # pragma: no cover
            logger.debug("ui_kit.fuzzy_pick InquirerPy path failed: %r", exc)
    return _fallback_select(message, choices, default=default)


def password(
    message: str,
    *,
    mask: str = "*",
    validate: Optional[Callable[[str], bool]] = None,
    allow_empty: bool = False,
) -> str:
    """Masked password prompt.

    InquirerPy / prompt_toolkit show one ``mask`` character per typed
    character so the operator gets visible feedback that the paste
    landed. Falls back to ``getpass.getpass`` (silent, same as the
    legacy behaviour) when the library is unavailable or stdin is not
    a TTY. The fallback annotates the prompt label so the operator can
    see they're in the silent path.

    ``getpass`` is the documented non-TTY fallback, but it opens
    /dev/tty directly, so where there is no controlling terminal at all
    (a piped `feral setup`, CI, pytest with captured output) it does not
    degrade, it raises OSError. That is handled at the call below rather
    than by refusing to prompt up front, so the getpass path itself stays
    exercisable and testable.
    """

    def _final_validate(raw: str) -> bool:
        if not allow_empty and not raw:
            return False
        if validate is not None:
            try:
                return bool(validate(raw))
            except Exception:
                return False
        return True

    if _INQUIRER_AVAILABLE and _is_interactive():
        try:

            def _build():
                return inquirer.secret(  # type: ignore[union-attr]
                    message=message,
                    qmark=BRAND_EMOJI,
                    amark=BRAND_EMOJI,
                    transformer=lambda r: mask * len(r) if r else "",
                    validate=_final_validate,
                    invalid_message="value cannot be empty",
                ).execute()

            return _run_inquirer_safely(_build)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # pragma: no cover
            logger.debug("ui_kit.password InquirerPy path failed: %r", exc)

    label = f"{message} (silent — non-interactive shell)"
    while True:
        try:
            value = getpass.getpass(label + ": ")
        except (EOFError, KeyboardInterrupt):
            raise
        except OSError:
            # No controlling terminal: getpass could not open /dev/tty.
            # Under pytest this is "reading from stdin while output is
            # captured", which is how a CI run of the voice preflight
            # died rather than skipping an optional API key. An empty
            # answer is what this prompt already treats as "skip", so a
            # scripted run degrades instead of crashing.
            logger.debug("password prompt: no tty, treating as skipped")
            return ""
        if _final_validate(value):
            return value
        sys.stdout.write("  value cannot be empty — try again.\n")


def confirm(message: str, *, default: bool = False) -> bool:
    """Yes/no with a default."""
    if _INQUIRER_AVAILABLE and _is_interactive():
        try:

            def _build():
                return bool(
                    inquirer.confirm(  # type: ignore[union-attr]
                        message=message,
                        default=default,
                        qmark=BRAND_EMOJI,
                        amark=BRAND_EMOJI,
                    ).execute()
                )

            return _run_inquirer_safely(_build)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # pragma: no cover
            logger.debug("ui_kit.confirm InquirerPy path failed: %r", exc)
    suffix = "Y/n" if default else "y/N"
    while True:
        sys.stdout.write(f"{message} [{suffix}]: ")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if line == "":
            return default
        line = line.strip().lower()
        if line == "":
            return default
        if line in ("y", "yes", "true", "1"):
            return True
        if line in ("n", "no", "false", "0"):
            return False
        sys.stdout.write("  Please answer yes or no.\n")


def text(
    message: str,
    *,
    default: str = "",
    validate: Optional[Callable[[str], bool]] = None,
    instruction: str = "",
    allow_empty: bool = True,
) -> str:
    """Free-text input."""

    def _final_validate(raw: str) -> bool:
        if not allow_empty and not raw:
            return False
        if validate is not None:
            try:
                return bool(validate(raw))
            except Exception:
                return False
        return True

    if _INQUIRER_AVAILABLE and _is_interactive():
        try:

            def _build():
                return inquirer.text(  # type: ignore[union-attr]
                    message=message,
                    default=default,
                    qmark=BRAND_EMOJI,
                    amark=BRAND_EMOJI,
                    validate=_final_validate,
                    instruction=instruction,
                    invalid_message="value cannot be empty",
                ).execute()

            return _run_inquirer_safely(_build)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # pragma: no cover
            logger.debug("ui_kit.text InquirerPy path failed: %r", exc)
    suffix = f" [{default}]" if default else ""
    while True:
        sys.stdout.write(f"{message}{suffix}: ")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if line == "":
            raise EOFError("stdin closed during text input")
        stripped = line.strip()
        if not stripped and default:
            return default
        if _final_validate(stripped):
            return stripped
        sys.stdout.write("  value cannot be empty — try again.\n")


def warn_non_interactive_setup_hint(console=None) -> None:
    """Print a one-line hint when an interactive command is launched
    without a controlling TTY (e.g. ``ssh host feral setup`` instead of
    ``ssh -t host feral setup``).
    """
    if _is_interactive():
        return
    console = console or get_console()
    hint = (
        "Interactive setup needs a real terminal. "
        "If you're SSH'd in, re-run with `ssh -t <host> feral setup`. "
        "For headless setup use `feral config set …`."
    )
    if _RICH_AVAILABLE:
        console.print(f"[{BRAND_COLOR}]{BRAND_EMOJI}[/]  {hint}")
    else:
        console.print(f"{BRAND_EMOJI}  {hint}")


__all__ = [
    "BRAND_EMOJI",
    "BRAND_COLOR",
    "MOUSE_ENV_VAR",
    "mouse_enabled",
    "select",
    "fuzzy_select",
    "pick",
    "fuzzy_pick",
    "password",
    "confirm",
    "text",
    "brand_panel",
    "banner_line",
    "print_start_banner",
    "print_ready_panel",
    "get_console",
    "is_inquirer_available",
    "is_interactive",
    "warn_non_interactive_setup_hint",
]
