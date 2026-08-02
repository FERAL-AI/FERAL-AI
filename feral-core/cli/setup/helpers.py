"""Shared prompt + table helpers used by every setup step.

The real prompt UX lives in :mod:`cli.ui_kit` (InquirerPy + Rich); the
public ``ask_choice`` / ``ask_text`` / ``confirm`` / ``resolve_option``
/ ``render_provider_table`` API here is preserved verbatim so every
existing step (audio, identity, channels, home_assistant, etc.) keeps
working without edits, and the legacy tests in
``tests/test_cli_setup.py`` keep importing the same symbols.

Behaviour rules carried over from the old typed-text wizard:

* ``ask_choice`` accepts either an arrow-key pick (when InquirerPy is
  available + the shell is interactive) or a typed canonical id /
  alias / numeric index / unambiguous substring; ambiguous typed input
  re-prompts with the candidate list rather than picking silently.
* ``ask_text`` is free-text with a sane default. Empty input accepts
  the default.
* ``confirm`` is yes/no with a default.
* Typing ``back`` / ``quit`` at any prompt raises ``BackNavigation``
  / ``QuitNavigation`` so the state machine can navigate centrally.
* ``render_table`` uses Rich when available; falls back to a plain
  markdown-ish renderer so headless environments still see the data.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional, Sequence

from cli import ui_kit

try:
    from rich.table import Table

    _RICH_AVAILABLE = True
except Exception:  # pragma: no cover - rich is a hard dep but guard anyway
    Table = None  # type: ignore[assignment]
    _RICH_AVAILABLE = False


STATUS_READY = "ready"
STATUS_NEEDS_KEY = "needs_api_key"
STATUS_UNREACHABLE = "unreachable"
STATUS_UNAVAILABLE = "unavailable"


# Legacy entry-point preserved (every step imports from here).
def get_console():
    return ui_kit.get_console()


@dataclass(frozen=True)
class Option:
    """A picker entry — id + human label + optional aliases + status.

    Status is rendered in the side-by-side table but does *not* block
    selection. The wizard explicitly wants to allow users to pick
    providers that show unreachable so they can re-probe after
    entering a key or starting the local server.
    """

    id: str
    label: str
    aliases: tuple[str, ...] = ()
    status: str = ""  # "ready" / "needs_api_key" / "unreachable" / ""
    hint: str = ""


def _normalize(text: str) -> str:
    return (text or "").strip().lower()


def resolve_option(text: str, options: Sequence[Option]) -> Optional[Option]:
    """Map a user-typed string to an :class:`Option` using the same
    precedence the catalog uses: canonical id → label → alias →
    numeric index → unambiguous substring.

    Returns ``None`` on ambiguity or empty input so the caller can
    re-prompt.
    """
    norm = _normalize(text)
    if not norm:
        return None

    if norm.isdigit():
        idx = int(norm) - 1
        if 0 <= idx < len(options):
            return options[idx]
        return None

    for opt in options:
        if norm == _normalize(opt.id):
            return opt
        if norm == _normalize(opt.label):
            return opt
        if any(norm == _normalize(a) for a in opt.aliases):
            return opt

    hits = []
    for opt in options:
        needles = [opt.id, opt.label, *opt.aliases]
        if any(norm in _normalize(n) for n in needles):
            hits.append(opt)
    if len(hits) == 1:
        return hits[0]
    return None


# ----------------------------------------------------------------------
# Prompts (delegate to ui_kit)
# ----------------------------------------------------------------------


_BACK_SENTINEL = "__feral_back__"
_QUIT_SENTINEL = "__feral_quit__"
_MENU_SENTINEL = "__feral_menu__"


def ask_choice(
    prompt: str,
    options: Sequence[Option],
    *,
    default: Optional[str] = None,
    console=None,
) -> Option:
    """Prompt the user for one of the given options.

    Uses arrow-key selection when InquirerPy is available + the shell
    is a TTY; otherwise falls back to the legacy typed prompt that
    accepts canonical ids / aliases / numeric indices / unambiguous
    substrings. Both paths support ``back`` and ``quit`` navigation.
    """
    console = console or get_console()
    default_opt: Optional[Option] = None
    if default:
        default_opt = next((o for o in options if o.id == default), None)

    # Interactive arrow-key path — preferred.
    if ui_kit.is_inquirer_available() and ui_kit.is_interactive():
        choices: list = []
        for opt in options:
            badge = _option_badge(opt)
            label = f"{opt.label}{badge}".strip()
            choices.append({"name": label, "value": opt.id})
        # Pseudo-choices for navigation parity with the legacy prompt.
        # The "↺ jump to a previous step…" affordance lets the operator
        # leave any step and re-enter the provider/key step (or any
        # earlier step) without re-walking the whole wizard. The state
        # machine catches :class:`JumpToStep` and shows the step picker.
        choices.append({"name": "↺ jump to a previous step…", "value": _MENU_SENTINEL})
        choices.append({"name": "← back", "value": _BACK_SENTINEL})
        choices.append({"name": "✕ quit setup", "value": _QUIT_SENTINEL})
        try:
            # v2026.5.28 — pick (direct-pick) instead of select
            # (space-to-mark). Provider / network-profile / autonomy
            # pickers are all "choose one and move on" interactions;
            # the mark-then-confirm UX was confusing for every
            # first-time operator. See cli/ui_kit.py for the
            # rationale + the kept `select` for flows that really
            # want a confirm step.
            picked = ui_kit.pick(
                prompt,
                choices,
                default=default_opt.id if default_opt else None,
            )
        except KeyboardInterrupt:
            raise QuitNavigation()
        if picked == _BACK_SENTINEL:
            raise BackNavigation()
        if picked == _QUIT_SENTINEL:
            raise QuitNavigation()
        if picked == _MENU_SENTINEL:
            raise JumpToStep()
        match = next((o for o in options if o.id == picked), None)
        if match is not None:
            return match
        # Defensive — fall through to the typed path if the picker returned
        # something we don't recognise (shouldn't happen).

    # Typed fallback (and the path the existing tests drive).
    while True:
        default_display = f" [{default_opt.label}]" if default_opt else ""
        raw = _prompt_raw(f"{prompt}{default_display}", console)
        if raw == "":
            if default_opt is not None:
                return default_opt
            console.print("[yellow]Please type a choice.[/]" if _RICH_AVAILABLE else "Please type a choice.")
            continue
        if raw.lower() in ("back", "b"):
            raise BackNavigation()
        if raw.lower() in ("quit", "q", "exit"):
            raise QuitNavigation()
        if raw.lower() in ("menu", "m", ":menu", ":back"):
            raise JumpToStep()

        hit = resolve_option(raw, options)
        if hit is not None:
            return hit

        norm = _normalize(raw)
        candidates = [
            o
            for o in options
            if norm in _normalize(o.id)
            or norm in _normalize(o.label)
            or any(norm in _normalize(a) for a in o.aliases)
        ]
        if candidates:
            names = ", ".join(c.label for c in candidates)
            console.print(f"Ambiguous — did you mean: {names}? Please type one exactly.")
        else:
            console.print(f"'{raw}' isn't a valid choice. Try again (type 'back' to go back).")


# A wizard prompt must terminate even when nobody is there to answer.
#
# `feral setup` gets driven non-interactively more often than the prompt
# helpers assumed: a scripted or piped install, CI, and any test that
# walks a step rather than stubbing each prompt inside it. The
# interactive branches below already check ``ui_kit.is_interactive()``,
# but the FALLBACK path did a raw stdin read, so a non-TTY run did not
# degrade, it blocked, and under pytest it died with "reading from stdin
# while output is captured".
#
# That is what broke test_setup_wizard_preflights on CI: the voice step
# asks for an API key (voice_preflight.py, ask_text(secret=True)) only
# when no key is discoverable, which is CI's situation but not a
# developer's, so it passed locally and failed there.
#
# Returning the documented default is the honest answer: it is what the
# operator would get by pressing enter, and it never invents a secret.
def _can_prompt() -> bool:
    """True when a human can actually answer."""
    try:
        return bool(ui_kit.is_interactive())
    except Exception:
        return False


def ask_text(
    prompt: str,
    *,
    default: str = "",
    allow_empty: bool = True,
    secret: bool = False,
    console=None,
) -> str:
    """Free-text input with back/quit support.

    ``secret=True`` routes to :func:`cli.ui_kit.password`, which masks
    every typed character with ``*`` so the operator gets visible
    feedback that their paste landed.
    """
    console = console or get_console()
    if secret:
        return _ask_secret(prompt, allow_empty=allow_empty, console=console)

    while True:
        default_display = f" [{default}]" if default else ""
        raw = _prompt_raw(f"{prompt}{default_display}", console)
        if raw == "" and default:
            return default
        if raw.lower() in ("back", "b"):
            raise BackNavigation()
        if raw.lower() in ("quit", "q", "exit"):
            raise QuitNavigation()
        if raw.lower() in ("menu", ":menu", ":back"):
            raise JumpToStep()
        if raw == "" and not allow_empty:
            console.print("[yellow]This field can't be empty.[/]" if _RICH_AVAILABLE else "This field can't be empty.")
            continue
        return raw


def _ask_secret(prompt: str, *, allow_empty: bool, console) -> str:
    """Masked prompt that is not a one-way door.

    ``ui_kit.password`` re-raises a bare ``KeyboardInterrupt``, which
    unwinds past the state machine and kills the whole wizard. Every
    API key and channel token went through this path, so Ctrl+C on a
    mistyped key prompt threw away the run instead of stepping back.

    A typed sentinel can't be matched loosely here — "back" is a
    legitimate (if terrible) password — so the escape hatches are the
    two colon-prefixed literals plus the interrupt keys, and the
    prompt advertises them.
    """
    label = f"{prompt}  (:back / :quit)"
    while True:
        try:
            raw = ui_kit.password(label, allow_empty=allow_empty)
        except KeyboardInterrupt:
            # Ctrl+C on a masked field means "get me out of this
            # prompt", not "discard the run" — hand control back to the
            # previous step, which offers its own quit affordance.
            raise BackNavigation()
        except EOFError:
            raise QuitNavigation()
        if raw is None:
            # InquirerPy returns None when the prompt is skipped.
            raise BackNavigation()
        stripped = raw.strip().lower()
        if stripped == ":back":
            raise BackNavigation()
        if stripped == ":quit":
            raise QuitNavigation()
        if stripped == ":menu":
            raise JumpToStep()
        if not raw and not allow_empty:
            console.print(
                "[yellow]This field can't be empty.[/]"
                if _RICH_AVAILABLE else "This field can't be empty."
            )
            continue
        return raw


async def probe_and_report(
    provider_id: str,
    *,
    console=None,
    display_name: str = "",
) -> tuple[bool, str]:
    """Run ``security.probe`` for *provider_id* and print the verdict.

    Mirrors the ``feral key add`` UX (store → probe → print result →
    let the caller offer a retry). Returns ``(ok, detail)`` so callers
    can drive their own retry loop.

    The wizard used to persist Home Assistant tokens, Telegram bot
    tokens and voice keys with no verification at all: setup reported
    success and the operator only found out the credential was wrong
    when the integration silently did nothing at runtime. Every one of
    those providers already had a probe registered — nothing called it.

    A provider with no registered probe reports honestly ("no probe
    registered") rather than claiming success.
    """
    console = console or get_console()
    name = display_name or provider_id
    try:
        from security.probe import probe as _probe
    except Exception as exc:
        console.print(f"  [yellow]Could not load the credential probe: {exc}[/]"
                      if _RICH_AVAILABLE else
                      f"  Could not load the credential probe: {exc}")
        return False, str(exc)

    console.print(f"  Probing {name}…")
    result = await _probe(provider_id, force=True)

    if result.ok:
        console.print(f"  [green]✔[/] {name} verified (HTTP {result.status_code})"
                      if _RICH_AVAILABLE else
                      f"  ✔ {name} verified (HTTP {result.status_code})")
        return True, result.detail or "ok"

    if result.reason == "unknown_provider":
        console.print(f"  [dim]No probe registered for {provider_id} — "
                      f"stored without verification.[/]"
                      if _RICH_AVAILABLE else
                      f"  No probe registered for {provider_id} — "
                      f"stored without verification.")
        return False, result.reason

    detail = result.detail or result.reason or "unreachable"
    console.print(f"  [red]✘[/] {name} rejected the credential: {detail}"
                  if _RICH_AVAILABLE else
                  f"  ✘ {name} rejected the credential: {detail}")
    return False, detail


def confirm(prompt: str, *, default: bool = False, console=None) -> bool:
    """Yes/no prompt.

    The arrow-key path is delegated to :func:`cli.ui_kit.confirm`; the
    typed fallback supports ``back`` / ``quit`` for parity with the
    legacy wizard navigation.
    """
    console = console or get_console()
    if ui_kit.is_inquirer_available() and ui_kit.is_interactive():
        try:
            return ui_kit.confirm(prompt, default=default)
        except KeyboardInterrupt:
            raise QuitNavigation()

    suffix = "Y/n" if default else "y/N"
    while True:
        raw = _prompt_raw(f"{prompt} [{suffix}]", console)
        if raw == "":
            return default
        if raw.lower() in ("back", "b"):
            raise BackNavigation()
        if raw.lower() in ("quit", "q", "exit"):
            raise QuitNavigation()
        if raw.lower() in ("menu", ":menu", ":back"):
            raise JumpToStep()
        if raw.lower() in ("y", "yes", "true", "1"):
            return True
        if raw.lower() in ("n", "no", "false", "0"):
            return False
        console.print("Please answer yes or no.")


def _prompt_raw(prompt: str, console) -> str:
    """Plain-text input fallback — kept for the typed code path that
    still handles ``back`` / ``quit`` sentinels.
    """
    if not _can_prompt():
        # Nobody can answer, so do not read. See _can_prompt for why the
        # guard lives at the READER rather than at ask_text/confirm:
        # tests replace the reader, and a guard above their stub would
        # short-circuit the very behaviour they pin.
        raise QuitNavigation()
    try:
        sys.stdout.write(prompt + ": ")
        sys.stdout.flush()
        line = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        raise QuitNavigation()
    if line == "":
        # EOF — same semantics as the legacy Rich path.
        raise QuitNavigation()
    return line.strip()


def _option_badge(opt: Option) -> str:
    if not opt.status:
        return ""
    mapping = {
        STATUS_READY: "  · ready",
        STATUS_NEEDS_KEY: "  · needs API key",
        STATUS_UNREACHABLE: "  · unreachable",
        STATUS_UNAVAILABLE: "  · unavailable",
    }
    return mapping.get(opt.status, "")


# ----------------------------------------------------------------------
# Table rendering
# ----------------------------------------------------------------------


def existing_provider_key(provider_id: str, env_var: str, state) -> tuple[str, str, list]:
    """Resolve "is a key already stored for *provider_id*?" across the
    three credential surfaces the wizard cares about.

    Returns a ``(secret, source, labels)`` tuple:

    * ``secret``: the raw API key (so callers can probe / mask it).
      Empty when no key is stored anywhere.
    * ``source``: one of ``"vault_keys"`` (labeled-key system),
      ``"vault_legacy"`` (default-namespace ``OPENAI_API_KEY`` style
      entry that the wizard previously wrote), ``"env"`` (process env
      var), or ``""`` (no key).
    * ``labels``: list of :class:`ProviderKeyEntry` for every labeled
      key the operator has stored under this provider (empty when the
      labeled-key system has nothing for *provider_id*).

    Used by the LLM provider step + the voice preflight step to
    detect "OpenAI key already configured for chat — use it for
    realtime voice too?" and to render "key already exists" instead
    of "needs a key" when the operator opens an existing install.
    """
    secret = ""
    source = ""
    labels: list = []
    try:
        from security import vault_keys

        labels = list(vault_keys.list_provider_keys(provider_id))
        active = vault_keys.get_active_provider_key(provider_id)
        if active:
            secret = active
            source = "vault_keys"
    except Exception:
        labels = []

    if not secret and state is not None and env_var:
        legacy = state.credentials.get(env_var, "") if hasattr(state, "credentials") else ""
        if legacy:
            secret = legacy
            source = "vault_legacy"

    if not secret and env_var:
        import os as _os

        env_val = _os.environ.get(env_var, "")
        if env_val:
            secret = env_val
            source = "env"

    return secret, source, labels


def render_provider_table(
    title: str,
    options: Sequence[Option],
    *,
    console=None,
    extra_columns: Optional[dict[str, dict[str, str]]] = None,
) -> None:
    """Render the side-by-side provider table with ready/needs-key/unreachable."""
    console = console or get_console()
    extra_columns = extra_columns or {}
    if _RICH_AVAILABLE and Table is not None:
        table = Table(title=title, show_lines=False)
        table.add_column("#", style="dim", justify="right")
        table.add_column("Provider", style="bold")
        table.add_column("Status")
        if extra_columns:
            table.add_column("Notes")
        for i, opt in enumerate(options, start=1):
            status = _pretty_status(opt.status)
            row = [str(i), opt.label, status]
            if extra_columns:
                note = extra_columns.get(opt.id, {}).get("note", opt.hint or "")
                row.append(note)
            table.add_row(*row)
        console.print(table)
        return

    console.print(title)
    for i, opt in enumerate(options, start=1):
        console.print(f"  {i}. {opt.label} — {_pretty_status(opt.status)}  {opt.hint}")


def _pretty_status(status: str) -> str:
    mapping = {
        STATUS_READY: "[green]ready[/]" if _RICH_AVAILABLE else "ready",
        STATUS_NEEDS_KEY: "[yellow]needs API key[/]" if _RICH_AVAILABLE else "needs API key",
        STATUS_UNREACHABLE: "[red]unreachable[/]" if _RICH_AVAILABLE else "unreachable",
        STATUS_UNAVAILABLE: "[dim]unavailable[/]" if _RICH_AVAILABLE else "unavailable",
        "": "",
    }
    return mapping.get(status, status)


# ----------------------------------------------------------------------
# Navigation exceptions
# ----------------------------------------------------------------------


class BackNavigation(Exception):
    """Raised by any prompt when the user types 'back'."""


class QuitNavigation(Exception):
    """Raised by any prompt when the user types 'quit'."""


class SkipStep(Exception):
    """A step raises this to tell the state machine to move on without persisting data."""


class JumpToStep(Exception):
    """Raised when the operator picks "↺ jump to a previous step…" (or
    types ``menu`` at any prompt) to request a non-linear hop back to
    an earlier step.

    The state machine catches this, shows a step picker, and sets the
    current step index to whatever the operator picked. Already-entered
    settings / credentials are preserved (the state object is the
    source of truth, never wiped on a jump); the operator can simply
    re-walk the picked step to overwrite a single answer.

    Carries an optional ``target`` so future call sites (e.g. a "jump
    straight to the key step" hot-button) can short-circuit the
    picker. When ``target`` is empty the state machine shows the full
    step list.
    """

    def __init__(self, target: str = ""):
        super().__init__(target)
        self.target = target
