"""Optional messaging channels — Telegram / Discord / Slack / WhatsApp."""

from __future__ import annotations

import os

from ..helpers import (
    ask_text,
    confirm,
    get_console,
    probe_and_report,
    _RICH_AVAILABLE,
)
from ..state import WizardState


_CHANNEL_FIELDS = {
    "telegram": [("FERAL_TELEGRAM_BOT_TOKEN", "Bot token", True)],
    "discord": [("FERAL_DISCORD_BOT_TOKEN", "Bot token", True)],
    "slack": [
        ("FERAL_SLACK_BOT_TOKEN", "Bot token (xoxb-...)", True),
        ("FERAL_SLACK_APP_TOKEN", "App token (xapp-...)", True),
    ],
    "whatsapp": [
        ("FERAL_WHATSAPP_PHONE_NUMBER_ID", "Phone number ID", False),
        ("FERAL_WHATSAPP_ACCESS_TOKEN", "Access token", True),
    ],
}

# Channels whose credentials ``security.probe`` can verify. WhatsApp has
# no registered probe, so its tokens are stored unverified and the step
# says so instead of implying they were checked.
_PROBEABLE_CHANNELS = ("telegram", "discord", "slack")


async def run(state: WizardState) -> None:
    console = get_console()
    console.print()
    console.print(
        "Talk to FERAL from a chat app. Each channel needs a bot token from "
        "that platform's developer console."
    )

    if not confirm("  Connect any messaging channels? (skip to add later)", default=False):
        return

    configured: list[str] = list(state.get_setting("channels", "configured", []) or [])
    for channel, fields in _CHANNEL_FIELDS.items():
        if not confirm(f"  Configure {channel}?", default=False):
            continue
        if await _configure_channel(state, channel, fields, console):
            if channel not in configured:
                configured.append(channel)

    state.set_setting("channels", "configured", configured)


async def _configure_channel(state: WizardState, channel: str, fields, console) -> bool:
    """Prompt for one channel's fields, then verify them.

    Returns True when the channel should be recorded as configured —
    which includes the "probe failed but the operator kept the token
    anyway" case, since the credential is genuinely stored.
    """
    first_pass = True
    while True:
        for env_key, label, secret in fields:
            # On a retry the operator already said the stored value is
            # wrong, so never offer to keep it.
            existing = state.credentials.get(env_key, "") if first_pass else ""
            if existing and not confirm(f"    Replace existing {env_key}?", default=False):
                # Make sure the probe below sees the stored value.
                os.environ.setdefault(env_key, existing)
                continue
            value = ask_text(f"    {label}", default="", allow_empty=False, secret=secret)
            state.set_credential(env_key, value)
            # The probes resolve tokens from the environment first, and
            # the vault write only lands in ``state.save()`` at the end
            # of the run — export now so the probe reads what was typed.
            os.environ[env_key] = value

        if channel not in _PROBEABLE_CHANNELS:
            console.print(
                f"  [dim]No probe registered for {channel} — token stored "
                f"without verification.[/]"
                if _RICH_AVAILABLE else
                f"  No probe registered for {channel} — token stored "
                f"without verification."
            )
            return True

        ok, _detail = await probe_and_report(
            channel, console=console, display_name=channel,
        )
        if ok:
            return True

        first_pass = False
        if not confirm(f"    Re-enter the {channel} credentials?", default=True):
            console.print(
                f"  [yellow]Keeping the {channel} token as typed — fix it "
                f"later with `feral key add`.[/]"
                if _RICH_AVAILABLE else
                f"  Keeping the {channel} token as typed — fix it later "
                f"with `feral key add`."
            )
            return True
