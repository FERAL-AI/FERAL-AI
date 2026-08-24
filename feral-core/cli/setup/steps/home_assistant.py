"""Optional Home Assistant step — URL + long-lived token, verified."""

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

#: How many times the operator may re-enter the URL and token before
#: the wizard stops asking. See the comment at the loop.
_MAX_HA_ATTEMPTS = 3


async def run(state: WizardState) -> None:
    console = get_console()
    console.print()
    console.print(
        "Point FERAL at your Home Assistant instance so the agent can read "
        "sensor state and call services."
    )

    if not confirm("  Connect a Home Assistant instance?", default=False):
        return

    default_url = state.get_setting(
        "home_assistant", "url", "http://homeassistant.local:8123"
    )
    # Bounded, for the reason steps/llm.py's _MAX_MODEL_ATTEMPTS states:
    # the only non-success exit below is a confirm defaulting to yes, so
    # an operator pressing enter through the defaults re-asks until
    # stdin runs out, and a piped run never answers at all.
    for attempt in range(_MAX_HA_ATTEMPTS):
        url = ask_text("  Home Assistant URL", default=default_url, allow_empty=False)
        token = ask_text(
            "  Long-lived access token", default="", allow_empty=False, secret=True,
        )

        # ``integrations/home_assistant.py`` and ``security.probe``'s
        # home_assistant probe both read HA_URL / HA_TOKEN. The wizard
        # previously stored HOME_ASSISTANT_URL / HOME_ASSISTANT_TOKEN,
        # a namespace nothing reads — the operator typed a valid token
        # into a black hole and the integration stayed dark.
        state.set_credential("HA_URL", url)
        state.set_credential("HA_TOKEN", token)
        # Export before probing: the probe resolves its base URL and
        # bearer from the environment.
        os.environ["HA_URL"] = url
        os.environ["HA_TOKEN"] = token

        state.set_setting("home_assistant", "enabled", True)
        state.set_setting("home_assistant", "url", url)

        ok, _detail = await probe_and_report(
            "home_assistant", console=console, display_name="Home Assistant",
        )
        if ok:
            return

        console.print(
            "  The URL + token are saved, but Home Assistant did not accept "
            "them. Check the URL is reachable from this machine and that the "
            "token is a Long-Lived Access Token (Profile → Security)."
            if _RICH_AVAILABLE else
            "  Saved, but Home Assistant did not accept them. Check the URL "
            "and that the token is a Long-Lived Access Token."
        )
        if attempt == _MAX_HA_ATTEMPTS - 1 or not confirm(
            "  Re-enter the URL / token?", default=True,
        ):
            # The credential IS stored either way, so the operator is
            # not losing what they typed. Say where to fix it rather
            # than leaving them to wonder.
            console.print(
                "  Keeping what you entered. Fix it later with "
                "`feral setup --from-step home_assistant`."
            )
            return
        default_url = url
