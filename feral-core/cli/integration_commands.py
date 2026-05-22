"""``feral integrations`` — list / connect / disconnect 3rd-party services.

Three connect flows are supported per the routed-in product decisions:

* ``feral integrations connect gmail`` — the **R-PROD-001** Gmail App
  Password walkthrough. Confirms 2FA, prompts for the Gmail address +
  16-character app password, validates the password format, probes
  IMAP + SMTP, and persists to the vault. Zero OAuth infra.
* ``feral integrations connect <id> --self-hosted`` — the
  **R-PROD-002** OAuth self-serve walkthrough. Prints the redirect URI
  + vendor-docs link, prompts the operator for ``client_id`` /
  ``client_secret``, writes them to the vault keys
  ``OAuthManager`` reads, then opens the browser to
  ``build_authorize_url`` and waits for the brain's
  ``/api/oauth/callback`` to land. Available for ``google``,
  ``notion``, ``spotify``, ``microsoft``.
* ``feral integrations connect home-assistant`` — long-lived token
  paste. Same path the wizard's HA step uses.

``feral integrations list`` and ``feral integrations disconnect`` are
the table + cleanup paths. All three writes go to the vault so the
brain runtime resolves them through the same ``BlindVault`` it
already uses — no settings.json drift.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

from cli import ui_kit


# Providers that support the ``--self-hosted`` OAuth walkthrough. Other
# OAuth providers either ship first-party (no walkthrough needed) or
# use a non-OAuth flow (Home Assistant token, Gmail App Password).
SELF_HOSTED_OAUTH_PROVIDERS = ("google", "notion", "spotify", "microsoft")


# ─────────────────────────────────────────────────────────────────────
# argparse registration
# ─────────────────────────────────────────────────────────────────────


def register_integrations_subparser(sub: "argparse._SubParsersAction") -> None:
    """Register ``feral integrations {list,connect,disconnect}``."""
    int_p = sub.add_parser(
        "integrations",
        help="Connect / disconnect 3rd-party services (Gmail, OAuth, HA).",
    )
    int_sub = int_p.add_subparsers(dest="action")

    int_sub.add_parser(
        "list",
        help="List integrations + connection status.",
    )

    connect_p = int_sub.add_parser(
        "connect",
        help="Run the connect walkthrough for one integration.",
    )
    connect_p.add_argument("integration_id", help="gmail / google / notion / spotify / microsoft / home-assistant")
    connect_p.add_argument(
        "--self-hosted", action="store_true",
        help="(OAuth providers) Use your own OAuth app instead of the "
             "first-party client. Prompts for client_id + secret, then "
             "opens the browser.",
    )
    connect_p.add_argument(
        "--no-browser", action="store_true",
        help="Print the OAuth URL instead of opening a browser (e.g. "
             "headless servers; copy/paste the URL into a browser on "
             "another device).",
    )

    disconnect_p = int_sub.add_parser(
        "disconnect",
        help="Remove stored credentials for one integration.",
    )
    disconnect_p.add_argument("integration_id", help="Integration id to disconnect")
    disconnect_p.add_argument("--yes", action="store_true", dest="confirm",
                              help="Skip the confirmation prompt.")


def dispatch_integrations_subcommand(args) -> int:
    action = getattr(args, "action", None) or "list"
    if action == "list":
        return cmd_integrations_list()
    if action == "connect":
        integ = (getattr(args, "integration_id", "") or "").strip().lower()
        # accept both "home-assistant" (cli convention) and
        # "home_assistant" (oauth_manager id) at the surface.
        canonical = "home_assistant" if integ in ("home-assistant", "home_assistant", "ha") else integ
        return cmd_integrations_connect(
            integration_id=canonical,
            self_hosted=bool(getattr(args, "self_hosted", False)),
            no_browser=bool(getattr(args, "no_browser", False)),
        )
    if action == "disconnect":
        integ = (getattr(args, "integration_id", "") or "").strip().lower()
        canonical = "home_assistant" if integ in ("home-assistant", "home_assistant", "ha") else integ
        return cmd_integrations_disconnect(
            integration_id=canonical,
            skip_confirm=bool(getattr(args, "confirm", False)),
        )
    print(f"Unknown action: {action}. Try one of: list, connect, disconnect.")
    return 2


# ─────────────────────────────────────────────────────────────────────
# list
# ─────────────────────────────────────────────────────────────────────


def cmd_integrations_list() -> int:
    """Render every integration with setup_status + connected + probe."""
    try:
        from integrations.oauth_manager import OAuthManager
    except Exception as exc:
        print(f"  OAuth manager unavailable: {exc}")
        return 1

    try:
        from security.vault import get_vault
        vault = get_vault()
    except Exception:
        vault = None

    mgr = OAuthManager(vault=vault, probe_on_add=False)
    rows = mgr.list_providers()

    # Augment with Gmail-app-password row (not OAuth, but a first-class
    # integration per R-PROD-001).
    rows.append({
        "id": "gmail",
        "name": "Gmail (App Password)",
        "auth_type": "app_password",
        "connected": _gmail_app_password_present(vault),
        "has_client_id": False,
        "setup_status": "ready",
        "setup_doc_url": "https://myaccount.google.com/apppasswords",
        "setup_doc_summary": (
            "Enable 2FA → generate an app password → paste it here."
        ),
        "scopes": [],
    })

    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
    except ImportError:
        console = None

    if console is not None:
        table = Table(title="Integrations", show_lines=False)
        table.add_column("Id", style="bold")
        table.add_column("Name")
        table.add_column("Auth")
        table.add_column("Connected")
        table.add_column("Setup")
        for r in rows:
            connected = "[green]✔[/green]" if r.get("connected") else "[dim]—[/dim]"
            setup = r.get("setup_status", "—")
            if setup == "provider_setup_required":
                setup_str = "[yellow]needs --self-hosted[/yellow]"
            elif setup == "ready":
                setup_str = "[green]ready[/green]"
            else:
                setup_str = setup
            table.add_row(r["id"], r["name"], r.get("auth_type", "?"), connected, setup_str)
        console.print(table)
    else:
        for r in rows:
            mark = "✔" if r.get("connected") else " "
            print(f"  [{mark}] {r['id']:<18} {r['name']:<28} {r.get('setup_status','—')}")

    print()
    print("  Connect flows:")
    print("    feral integrations connect gmail")
    print("    feral integrations connect google --self-hosted")
    print("    feral integrations connect home-assistant")
    return 0


def _gmail_app_password_present(vault) -> bool:
    """Check the vault + env for the IMAP user/pass pair that signals
    Gmail-via-App-Password is configured."""
    import os

    if vault is not None:
        try:
            user = vault.get_credential("FERAL_EMAIL_IMAP_USER") or ""
            pwd = vault.get_credential("FERAL_EMAIL_IMAP_PASS") or ""
            if user and pwd:
                return True
        except Exception:
            pass
    return bool(
        os.environ.get("FERAL_EMAIL_IMAP_USER")
        and os.environ.get("FERAL_EMAIL_IMAP_PASS")
    )


# ─────────────────────────────────────────────────────────────────────
# connect — dispatch by integration_id
# ─────────────────────────────────────────────────────────────────────


def cmd_integrations_connect(
    *,
    integration_id: str,
    self_hosted: bool = False,
    no_browser: bool = False,
) -> int:
    """Run the connect walkthrough for one integration."""
    if not integration_id:
        print("  Integration id is required (e.g. 'gmail', 'google', 'home-assistant').")
        return 2

    if integration_id == "gmail":
        return _connect_gmail_app_password()

    if integration_id == "home_assistant":
        return _connect_home_assistant_token()

    if integration_id in SELF_HOSTED_OAUTH_PROVIDERS:
        if not self_hosted:
            # The non-self-hosted path requires baked first-party
            # credentials; if they're missing we steer the operator to
            # ``--self-hosted`` rather than hitting `provider_setup_required`.
            from integrations.oauth_manager import OAuthManager
            from security.vault import get_vault
            try:
                vault = get_vault()
            except Exception:
                vault = None
            mgr = OAuthManager(vault=vault, probe_on_add=False)
            status = mgr.setup_status(integration_id)
            if status == "provider_setup_required":
                print(
                    f"  {integration_id} has no first-party OAuth client baked into "
                    f"this build. Run with `--self-hosted` to register your own:"
                )
                print(f"    feral integrations connect {integration_id} --self-hosted")
                return 2
            return _connect_oauth(integration_id, self_hosted=False, no_browser=no_browser)
        return _connect_oauth(integration_id, self_hosted=True, no_browser=no_browser)

    print(f"  Unknown integration: {integration_id!r}")
    print(f"  Supported: gmail, home-assistant, " + ", ".join(SELF_HOSTED_OAUTH_PROVIDERS))
    return 2


# ─────────────────────────────────────────────────────────────────────
# Gmail App Password walkthrough (R-PROD-001)
# ─────────────────────────────────────────────────────────────────────


# Google App Passwords are 16 lowercase letters, sometimes shown to the
# operator as 4-char groups separated by spaces ("xxxx xxxx xxxx xxxx").
# We accept both forms — any whitespace is stripped before validation.
_APP_PASSWORD_RE = re.compile(r"^[a-z]{16}$")
_GMAIL_ADDR_RE = re.compile(r"^[^@\s]+@(gmail\.com|googlemail\.com)$", re.IGNORECASE)


def _connect_gmail_app_password() -> int:
    """The terminal twin of the WebUI Settings → Gmail tile.

    Walkthrough:
      1. Brand panel with 5 numbered steps + the App Password URL.
      2. Confirm 2FA is enabled.
      3. Prompt for Gmail address (validated against gmail.com/
         googlemail.com).
      4. Prompt for the 16-character app password (validated client-
         side per parent ack reminder #2).
      5. IMAP + SMTP probe to imap.gmail.com:993 + smtp.gmail.com:587.
      6. Persist FERAL_EMAIL_IMAP_USER / IMAP_PASS / IMAP_HOST /
         SMTP_HOST / SMTP_PORT to the vault. Brain runtime resolves
         these via vault-then-env.
    """
    ui_kit.brand_panel(
        "Connect Gmail (App Password)",
        body=(
            "Gmail's App Password flow lets FERAL read + send mail via\n"
            "IMAP/SMTP without OAuth. Five steps:\n\n"
            "  1. Open https://myaccount.google.com/security\n"
            "  2. Turn on 2-Step Verification if you haven't already.\n"
            "  3. Open https://myaccount.google.com/apppasswords\n"
            "  4. Pick 'Mail' → 'Other (Custom name)' → enter 'FERAL'.\n"
            "  5. Paste the 16-character password below — Google\n"
            "     usually shows it as four groups of four (the spaces\n"
            "     are stripped automatically)."
        ),
    )

    try:
        if not ui_kit.confirm(
            "Have you enabled 2-Step Verification on this Google account?",
            default=False,
        ):
            print("  Enable 2FA first, then re-run `feral integrations connect gmail`.")
            return 1
    except KeyboardInterrupt:
        print()
        print("  Cancelled.")
        return 1

    while True:
        try:
            email = ui_kit.text(
                "Gmail address",
                allow_empty=False,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("  Cancelled.")
            return 1
        if _GMAIL_ADDR_RE.match(email):
            break
        print("  That doesn't look like a gmail.com / googlemail.com address. Try again.")

    while True:
        try:
            raw = ui_kit.password(
                "App password (16 lowercase letters)",
                allow_empty=False,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("  Cancelled.")
            return 1
        # Strip Google's 4-char-group spaces.
        candidate = re.sub(r"\s+", "", raw).lower()
        if _APP_PASSWORD_RE.match(candidate):
            app_password = candidate
            break
        print(
            f"  That doesn't match the App Password format "
            f"(got {len(candidate)} chars; expected 16 lowercase letters). "
            "Try again — paste exactly what Google's dialog showed."
        )

    print()
    print("  Probing imap.gmail.com (IMAP) + smtp.gmail.com (SMTP)…")
    imap_ok, imap_detail = _probe_imap("imap.gmail.com", 993, email, app_password)
    if not imap_ok:
        print(f"  ✘ IMAP probe failed: {imap_detail}")
        print("  The credentials are NOT saved. Check the address + password and re-run.")
        return 1
    print(f"  ✔ IMAP login OK ({imap_detail})")

    smtp_ok, smtp_detail = _probe_smtp("smtp.gmail.com", 587, email, app_password)
    if not smtp_ok:
        print(f"  ✘ SMTP probe failed: {smtp_detail}")
        print("  The credentials are NOT saved. Check the address + password and re-run.")
        return 1
    print(f"  ✔ SMTP login OK ({smtp_detail})")

    if not _persist_gmail_credentials(email, app_password):
        return 1
    print()
    print(f"  Gmail connected for {email}. The brain will use IMAP for reads + SMTP for sends.")
    return 0


def _probe_imap(host: str, port: int, user: str, password: str) -> tuple[bool, str]:
    """Login to an IMAP server and immediately log out. Returns
    ``(ok, detail)``."""
    import imaplib

    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=10)
        try:
            conn.login(user, password)
            conn.logout()
        except Exception:
            try:
                conn.shutdown()
            except Exception:
                pass
            raise
        return True, f"imap.gmail.com:{port} login as {user}"
    except imaplib.IMAP4.error as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _probe_smtp(host: str, port: int, user: str, password: str) -> tuple[bool, str]:
    """Login to an SMTP server and quit. Returns ``(ok, detail)``."""
    import smtplib

    try:
        conn = smtplib.SMTP(host, port, timeout=10)
        try:
            conn.starttls()
            conn.login(user, password)
            conn.quit()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        return True, f"smtp.gmail.com:{port} login as {user}"
    except smtplib.SMTPAuthenticationError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _persist_gmail_credentials(email: str, app_password: str) -> bool:
    """Write the App Password credentials to the vault so the brain
    runtime resolves them through the same path as every other
    integration."""
    try:
        from security.vault import get_vault
        vault = get_vault()
    except Exception as exc:
        print(f"  Could not open the vault: {exc}")
        return False

    pairs = {
        "FERAL_EMAIL_IMAP_HOST": "imap.gmail.com",
        "FERAL_EMAIL_IMAP_PORT": "993",
        "FERAL_EMAIL_IMAP_USER": email,
        "FERAL_EMAIL_IMAP_PASS": app_password,
        "FERAL_EMAIL_SMTP_HOST": "smtp.gmail.com",
        "FERAL_EMAIL_SMTP_PORT": "587",
        "FERAL_EMAIL_SMTP_USER": email,
        "FERAL_EMAIL_SMTP_PASS": app_password,
    }
    try:
        for k, v in pairs.items():
            vault.set_credential(k, v)
        return True
    except Exception as exc:
        print(f"  Vault write failed: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────
# Home Assistant long-lived token (R-PROD-002 / wizard parity)
# ─────────────────────────────────────────────────────────────────────


def _connect_home_assistant_token() -> int:
    """Prompt for HA URL + long-lived token and probe ``/api/states``."""
    ui_kit.brand_panel(
        "Connect Home Assistant",
        body=(
            "FERAL talks to Home Assistant via its REST API + WebSocket\n"
            "using a long-lived access token.\n\n"
            "  1. In Home Assistant, click your user avatar → Profile →\n"
            "     Long-Lived Access Tokens → Create.\n"
            "  2. Copy the token (Home Assistant only shows it once).\n"
            "  3. Paste it below alongside your HA URL."
        ),
    )

    try:
        url = ui_kit.text(
            "Home Assistant URL (e.g. http://homeassistant.local:8123)",
            allow_empty=False,
        ).strip().rstrip("/")
    except (EOFError, KeyboardInterrupt):
        print()
        print("  Cancelled.")
        return 1

    try:
        token = ui_kit.password(
            "Long-Lived Access Token",
            allow_empty=False,
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print("  Cancelled.")
        return 1

    print()
    print(f"  Probing {url}/api/states with the supplied token…")
    ok, detail = _probe_home_assistant(url, token)
    if not ok:
        print(f"  ✘ Probe failed: {detail}")
        print("  The token is NOT saved. Check the URL + token and re-run.")
        return 1
    print(f"  ✔ Home Assistant reachable ({detail})")

    try:
        from security.vault import get_vault
        vault = get_vault()
        vault.set_credential("HOMEASSISTANT_URL", url)
        vault.set_credential("HOMEASSISTANT_TOKEN", token)
    except Exception as exc:
        print(f"  Vault write failed: {exc}")
        return 1

    print()
    print("  Home Assistant connected.")
    return 0


def _probe_home_assistant(url: str, token: str) -> tuple[bool, str]:
    import urllib.request
    import urllib.error

    req = urllib.request.Request(
        f"{url}/api/states",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read(2048)
            return True, f"HTTP {resp.status} — {len(data)} bytes preview"
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return False, f"HTTP {exc.code} — {body or exc.reason}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ─────────────────────────────────────────────────────────────────────
# OAuth self-serve (R-PROD-002)
# ─────────────────────────────────────────────────────────────────────


def _connect_oauth(
    provider_id: str,
    *,
    self_hosted: bool,
    no_browser: bool,
) -> int:
    """Run the OAuth flow against the user's own (or first-party) app.

    When ``self_hosted=True`` we prompt for ``client_id`` +
    ``client_secret`` and write them to the vault under the keys
    ``OAuthManager._VAULT_CREDENTIAL_KEYS`` reads at construction.
    The manager is then re-instantiated so the new credentials take
    effect, the authorize URL is built, the browser is opened, and
    we poll the vault for the resulting access token until it
    appears (or 5 minutes elapse).
    """
    from integrations.oauth_manager import OAuthManager, _VAULT_CREDENTIAL_KEYS
    from config.runtime import brain_public_base_url

    if provider_id not in _VAULT_CREDENTIAL_KEYS and self_hosted:
        print(f"  {provider_id} is not in the OAuth credential map; can't self-host.")
        return 2

    try:
        from security.vault import get_vault
        vault = get_vault()
    except Exception as exc:
        print(f"  Vault unavailable: {exc}")
        return 1

    redirect_uri = f"{brain_public_base_url().rstrip('/')}/api/oauth/callback"

    if self_hosted:
        # Prompt before constructing the manager so the new vault
        # creds are picked up by the manager's _load_providers layer 3.
        from integrations.oauth_manager import BUILTIN_PROVIDERS
        builtin = BUILTIN_PROVIDERS.get(provider_id, {})
        doc_summary = builtin.get("setup_doc_summary", "")

        ui_kit.brand_panel(
            f"Connect {provider_id} (self-hosted OAuth)",
            body=(
                f"Register your own OAuth app at the vendor's developer\n"
                f"console, then paste your Client ID + Secret below.\n\n"
                f"  Redirect URI to configure on the vendor side:\n"
                f"    {redirect_uri}\n\n"
                f"  Vendor docs / instructions:\n"
                f"    {doc_summary or '(no summary available — see the vendor portal)'}"
            ),
        )

        id_key, secret_key = _VAULT_CREDENTIAL_KEYS[provider_id]

        try:
            client_id = ui_kit.text(
                f"Client ID for {provider_id}",
                allow_empty=False,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("  Cancelled.")
            return 1
        try:
            client_secret = ui_kit.password(
                f"Client Secret for {provider_id} (paste exactly)",
                allow_empty=False,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("  Cancelled.")
            return 1

        try:
            vault.set_credential(id_key, client_id)
            vault.set_credential(secret_key, client_secret)
        except Exception as exc:
            print(f"  Vault write failed: {exc}")
            return 1
        print(f"  Saved {id_key} + {secret_key} to the vault.")

    # (Re-)instantiate the manager so new vault creds are loaded.
    mgr = OAuthManager(vault=vault, probe_on_add=False)
    auth_response = mgr.build_authorize_response(provider_id)
    if not auth_response.get("success"):
        print(f"  Could not build authorize URL: {auth_response.get('error')}")
        if auth_response.get("setup_doc_url"):
            print(f"  See: {auth_response['setup_doc_url']}")
        return 1
    url = auth_response["url"]
    state = auth_response["state"]

    print()
    if no_browser:
        print(f"  Open this URL in a browser to grant access:")
        print(f"    {url}")
    else:
        print(f"  Opening browser for {provider_id} OAuth consent…")
        try:
            webbrowser.open(url)
        except Exception:
            print("  Could not auto-open browser; copy this URL into one yourself:")
            print(f"    {url}")
    print()
    print(f"  Redirect URI: {redirect_uri}")
    print(f"  State: {state[:16]}…")
    print()
    print("  Waiting for the brain to receive the OAuth callback (Ctrl+C to abort)…")
    print("  (NOTE: this requires the brain to be running — `feral serve` in another terminal.)")

    # Poll the vault for the token, refreshing the manager's view each
    # iteration. Cap at 5 minutes per the prompt's contract.
    deadline = time.time() + 300.0
    while time.time() < deadline:
        try:
            polled = OAuthManager(vault=vault, probe_on_add=False)
            row = next(
                (p for p in polled.list_providers() if p["id"] == provider_id),
                None,
            )
            if row and row.get("connected"):
                print(f"  ✔ {provider_id} connected.")
                return 0
        except Exception:
            pass
        time.sleep(2.0)

    print("  Timed out waiting for the OAuth callback. Re-run when ready.")
    return 1


# ─────────────────────────────────────────────────────────────────────
# disconnect
# ─────────────────────────────────────────────────────────────────────


def cmd_integrations_disconnect(*, integration_id: str, skip_confirm: bool = False) -> int:
    """Remove stored credentials for one integration."""
    if not integration_id:
        print("  integration_id is required.")
        return 2

    if not skip_confirm:
        ui_kit.brand_panel(
            "Disconnect integration",
            body=(
                f"About to remove all stored credentials for "
                f"{integration_id!r} from the vault. The brain runtime\n"
                f"will lose access to this integration immediately."
            ),
        )
        try:
            if not ui_kit.confirm("Continue?", default=False):
                print("  Cancelled.")
                return 1
        except KeyboardInterrupt:
            print()
            print("  Cancelled.")
            return 1

    try:
        from security.vault import get_vault
        vault = get_vault()
    except Exception as exc:
        print(f"  Vault unavailable: {exc}")
        return 1

    keys_to_clear: list[str] = []
    if integration_id == "gmail":
        keys_to_clear = [
            "FERAL_EMAIL_IMAP_HOST", "FERAL_EMAIL_IMAP_PORT",
            "FERAL_EMAIL_IMAP_USER", "FERAL_EMAIL_IMAP_PASS",
            "FERAL_EMAIL_SMTP_HOST", "FERAL_EMAIL_SMTP_PORT",
            "FERAL_EMAIL_SMTP_USER", "FERAL_EMAIL_SMTP_PASS",
        ]
    elif integration_id == "home_assistant":
        keys_to_clear = ["HOMEASSISTANT_URL", "HOMEASSISTANT_TOKEN"]
    else:
        from integrations.oauth_manager import _VAULT_CREDENTIAL_KEYS
        if integration_id in _VAULT_CREDENTIAL_KEYS:
            id_key, secret_key = _VAULT_CREDENTIAL_KEYS[integration_id]
            keys_to_clear = [id_key, secret_key, f"oauth_{integration_id}"]
        else:
            print(f"  Unknown integration: {integration_id!r}")
            return 2

    removed = 0
    for k in keys_to_clear:
        try:
            if vault.get_credential(k):
                # ``BlindVault.remove`` is the canonical delete API
                # (no ``delete_credential`` alias). Reads from the
                # default namespace match how ``set_credential``
                # writes — they're symmetrical.
                vault.remove(k, removed_by="cli.integrations")
                removed += 1
        except Exception:
            pass
    print(f"  Removed {removed} vault entr{'y' if removed == 1 else 'ies'} for {integration_id}.")
    return 0
