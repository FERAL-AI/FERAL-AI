"""macOS TCC preflight (audit-r14 / lane-07 W7 — R-PROD-004).

Reads the read-only TCC probes from ``security.macos_permissions`` and
shows the operator the status of Calendar / Reminders / Contacts /
Full Disk Access / Accessibility / Screen Recording. Surfaces the
``x-apple.systempreferences:`` deeplink for any denial so the
operator can click-to-open the right Settings pane.

The CLI is read-only here — the *request* flow (firing native consent
prompts) lives on the brain side / iOS app per R-PROD-004. On non-
Darwin platforms the step is a no-op.
"""

from __future__ import annotations

import platform

from cli import ui_kit

from ..helpers import (
    SkipStep,
    confirm,
    get_console,
    _RICH_AVAILABLE,
)
from ..state import WizardState


def run(state: WizardState) -> None:
    if platform.system() != "Darwin":
        raise SkipStep()

    console = get_console()

    try:
        from security.macos_permissions import all_gui_permission_statuses
        from agents.tcc_card import TCC_CATALOG
    except Exception as exc:
        console.print(
            f"[yellow]TCC preflight unavailable: {exc}[/]"
            if _RICH_AVAILABLE else f"TCC preflight unavailable: {exc}"
        )
        raise SkipStep()

    if _RICH_AVAILABLE:
        console.print(
            "[bold]macOS permission preflight[/bold] — read-only check.\n"
            "FERAL never auto-prompts here; the brain + iOS app trigger "
            "the native macOS consent dialogs when each surface is "
            "actually used. This step just shows you what's already "
            "granted and links to the System Settings pane for any "
            "you'll want to enable up front."
        )

    statuses = all_gui_permission_statuses()
    rendered = []
    for status in statuses:
        deeplink = (TCC_CATALOG.get(status.permission) or {}).get("macos_deeplink", "")
        rendered.append((status, deeplink))

    if _RICH_AVAILABLE:
        try:
            from rich.table import Table
        except ImportError:
            Table = None
        if Table is not None:
            table = Table(title="macOS permissions (TCC)")
            table.add_column("Permission", style="bold")
            table.add_column("Status")
            table.add_column("Open Settings")
            for status, deeplink in rendered:
                if status.status == "granted":
                    cell = "[green]✔ granted[/green]"
                elif status.status == "denied":
                    cell = "[red]✘ denied[/red]"
                elif status.status == "unknown":
                    cell = "[yellow]⚠ unknown[/yellow]"
                else:
                    cell = "[dim]—[/dim]"
                table.add_row(
                    status.permission.replace("_", " ").title(),
                    cell,
                    deeplink or "(none)",
                )
            console.print(table)
        else:
            _render_plain(console, rendered)
    else:
        _render_plain(console, rendered)

    # Persist a snapshot in settings.json so the doctor + dashboard
    # can reflect the wizard's last reading without re-probing.
    state.set_setting(
        "macos",
        "tcc_snapshot",
        [
            {
                "permission": s.permission,
                "status": s.status,
                "deeplink": d,
            }
            for s, d in rendered
        ],
    )

    # Offer to print the deeplink list one more time so the operator
    # can copy-paste before continuing — useful when the terminal
    # doesn't render OSC-8 hyperlinks. We don't block on this.
    try:
        if confirm("Print the list of System Settings deeplinks?", default=False):
            for status, deeplink in rendered:
                if deeplink:
                    console.print(f"  {status.permission}: {deeplink}")
    except Exception:
        # Confirm prompt failures (non-tty etc.) shouldn't kill the wizard.
        pass


def _render_plain(console, rendered):
    for status, deeplink in rendered:
        mark = (
            "✔" if status.status == "granted"
            else ("✘" if status.status == "denied" else "?")
        )
        console.print(
            f"  [{mark}] {status.permission:<22} {status.status:<10} {deeplink}"
        )
