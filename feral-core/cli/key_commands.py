"""
``feral key`` — vault key lifecycle CLI ().

Subcommands:
  - ``feral key status``  — show vault status (encrypted? master key
                            in keychain? legacy backup? rotation backup?)
  - ``feral key rotate``  — generate a new master key, re-encrypt the
                            vault, swap atomically, print the new
                            recovery code (shown ONCE).
  - ``feral key recover`` — restore the OS keychain master key from a
                            written-down recovery code. Use this when
                            the keychain is wiped (new laptop, OS
                            reinstall, accidental delete).

These commands are wired into ``feral`` via :func:`register_key_subparser`
which is invoked from ``cli/main.py``. Doing the registration in this
file keeps the CLI surface for the security path testable without
importing the whole brain.

audit-r14 / lane-06 (v2026.5.38) — the  ``feral key list / migrate
/ rotate --provider --agent --key`` commands and the per-agent
``AuthProfileFileStore`` they wrote to were removed. The audit
finding documented zero runtime consumers outside this file and the
 unit tests, and Lane 03's Wave 1 work made BlindVault the single
credential authority that every provider, skill, and integration
resolves through. Keeping  plaintext-JSON shadow store alive
forced operators to maintain two truths for the same key; deleting it
finishes the audit-r12 #2 fix.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from cli import ui_kit


def _signal_brain_reload(provider_id: str) -> str:
    """Cross-cut #1 (v2026.5.42) — nudge the running brain to pick up
    the newly-active labeled key without a restart.

    Posts to the local ``/api/llm/providers/{pid}/keys/active``
    endpoint with the just-written label. Best effort: a connection
    refused (brain not running) or non-2xx response returns a short
    operator-facing hint string instead of raising. Returns ``""`` on
    success so the caller can stay quiet on the happy path.

    Never raises, never blocks for more than a couple of seconds.
    """
    try:
        from security import vault_keys
        from config.runtime import brain_port
    except Exception:
        return ""
    try:
        active = vault_keys.get_active_label(provider_id)
    except Exception:
        active = None
    if not active:
        return ""
    try:
        import json as _json
        import urllib.error
        import urllib.request
        url = f"http://127.0.0.1:{brain_port()}/api/llm/providers/{provider_id}/keys/active"
        body = _json.dumps({"label": active}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if 200 <= resp.status < 300:
                return ""
            return f"(brain returned HTTP {resp.status} — restart will pick up the new key)"
    except urllib.error.URLError:
        return "(brain not running — restart will pick up the new key)"
    except Exception:
        return "(brain hot-swap failed — restart will pick up the new key)"


# ─────────────────────────────────────────────────────────────────────
# Argparse registration (called from cli/main.py)
# ─────────────────────────────────────────────────────────────────────


def register_key_subparser(sub: "argparse._SubParsersAction") -> None:
    """Register ``feral key {status,rotate,recover,add,list,remove}``
    under the main ``feral`` argparse subparsers group.

    audit-r14 / lane-07  — the per-provider multi-key surface
    (``add``/``list``/``remove`` and the label-aware ``rotate``)
    wraps Wave 2 Lane 09's ``security.vault_keys`` overlay. The
    legacy master-key ``rotate`` (no ``--provider``) is preserved
    so existing scripts keep working.
    """
    key_p = sub.add_parser(
        "key",
        help=(
            "Manage the encrypted credential vault — status, master "
            "rotate/recover, and per-provider multi-key add/list/remove."
        ),
    )
    key_sub = key_p.add_subparsers(dest="action")

    key_sub.add_parser(
        "status",
        help="Show vault status: encrypted on disk, master key in keychain, "
             "presence of legacy/rotation backups.",
    )

    rotate_p = key_sub.add_parser(
        "rotate",
        help=(
            "Master rotate (no flags): generate a new master key, re-encrypt "
            "the vault, and print a fresh recovery code (shown ONCE). "
            "Per-provider rotate (with --provider --label): replace the "
            "labeled API key with a new one (vault unaffected)."
        ),
    )
    rotate_p.add_argument(
        "--yes",
        action="store_true",
        dest="key_confirm",
        help="Skip the interactive confirmation prompt (use in scripts).",
    )
    rotate_p.add_argument(
        "--provider",
        default="",
        help="Provider id (e.g. openai, anthropic, gemini). When set, "
             "rotates the per-provider labeled API key instead of the "
             "vault master key.",
    )
    rotate_p.add_argument(
        "--label",
        default="",
        help="Label of the per-provider key to rotate (default: 'default').",
    )
    rotate_p.add_argument(
        "--api-key",
        default="",
        help="New API key. If omitted, you will be prompted interactively.",
    )

    recover_p = key_sub.add_parser(
        "recover",
        help="Restore the OS keychain master key from a written-down "
             "recovery code (when the keychain entry is wiped).",
    )
    recover_p.add_argument(
        "--code",
        default="",
        help="Recovery code; if omitted, you will be prompted "
             "interactively (recommended — paste-from-terminal-history "
             "leaves the secret in your shell history).",
    )

    # ── audit-r14 / lane-07  — multi-key per-provider commands ──

    add_p = key_sub.add_parser(
        "add",
        help=(
            "Add (or replace) a labeled API key for a provider. The key is "
            "stored in the encrypted vault and a probe is run immediately "
            "so you see green/red before the prompt closes."
        ),
    )
    add_p.add_argument("--provider", required=True, help="Provider id (openai, anthropic, ...)")
    add_p.add_argument(
        "--label", default="default",
        help="Short tag for this key (default 'default'). Use 'prod' / "
             "'dev' / 'team-a' to keep multiple credentials per provider.",
    )
    add_p.add_argument(
        "--api-key", default="",
        help="The API key. If omitted, you will be prompted interactively "
             "(recommended — keeps the secret out of shell history).",
    )
    add_p.add_argument(
        "--set-active", action="store_true",
        help="Make this label the runtime default for the provider.",
    )
    add_p.add_argument(
        "--no-probe", action="store_true",
        help="Skip the post-add probe (faster but you don't see validity).",
    )

    list_p = key_sub.add_parser(
        "list",
        help="List labeled keys for one or every provider with probe status.",
    )
    list_p.add_argument(
        "--provider", default="",
        help="Restrict to one provider id. Empty = list every provider that "
             "has at least one labeled key.",
    )

    remove_p = key_sub.add_parser(
        "remove",
        help="Delete a labeled API key from the vault.",
    )
    remove_p.add_argument("--provider", required=True, help="Provider id")
    remove_p.add_argument("--label", required=True, help="Label to remove")
    remove_p.add_argument(
        "--yes", action="store_true", dest="key_confirm",
        help="Skip the interactive confirmation prompt.",
    )


def dispatch_key_subcommand(args) -> int:
    action = getattr(args, "action", None) or "status"
    if action == "status":
        return cmd_key_status()
    if action == "rotate":
        provider = (getattr(args, "provider", "") or "").strip()
        if provider:
            return cmd_key_rotate_provider(
                provider_id=provider,
                label=(getattr(args, "label", "") or "").strip() or "default",
                api_key=(getattr(args, "api_key", "") or "").strip(),
                skip_confirm=getattr(args, "key_confirm", False),
            )
        return cmd_key_rotate(skip_confirm=getattr(args, "key_confirm", False))
    if action == "recover":
        return cmd_key_recover(code=getattr(args, "code", "") or "")
    if action == "add":
        return cmd_key_add(
            provider_id=getattr(args, "provider", "") or "",
            label=(getattr(args, "label", "") or "").strip() or "default",
            api_key=(getattr(args, "api_key", "") or "").strip(),
            set_active=bool(getattr(args, "set_active", False)),
            probe=not bool(getattr(args, "no_probe", False)),
        )
    if action == "list":
        return cmd_key_list(provider_id=(getattr(args, "provider", "") or "").strip())
    if action == "remove":
        return cmd_key_remove(
            provider_id=getattr(args, "provider", "") or "",
            label=(getattr(args, "label", "") or "").strip(),
            skip_confirm=getattr(args, "key_confirm", False),
        )
    print(
        f"Unknown action: {action}. "
        f"Try one of: status, rotate, recover, add, list, remove."
    )
    return 2


# ─────────────────────────────────────────────────────────────────────
# Status
# ─────────────────────────────────────────────────────────────────────


def cmd_key_status() -> int:
    """Print a one-screen vault report. Never echoes secrets."""
    from security.vault import get_vault, VaultError

    try:
        v = get_vault()
    except VaultError as exc:
        print("  feral key status — vault unavailable")
        print()
        print(f"  Error: {exc}")
        print()
        print("  Resolution: run `feral key recover` and supply your")
        print("  recovery code (the one you wrote down at first boot or")
        print("  after the most recent `feral key rotate`).")
        return 1

    s = v.status()
    print("  FERAL Vault — Status")
    print("  " + "=" * 40)
    print(f"  Encrypted file : {'yes' if s['encrypted'] else 'no (no vault yet)'}")
    print(f"  Path           : {s['encrypted_path']}")
    print(f"  Master key     : {'in OS keychain' if s['keychain'] else 'NOT in keychain'}")
    print(f"  Keychain entry : service={s['keychain_service']!r}, "
          f"user={s['keychain_user']!r}")
    print(f"  Legacy backup  : {'present (' + s['legacy_backup_path'] + ')' if s['legacy_backup'] else 'none'}")
    print(f"  Rotation prev  : {'present (' + s['prev_backup_path'] + ')' if s['prev_backup'] else 'none'}")
    print(f"  Namespaces     : {', '.join(s['namespaces']) or '(empty)'}")
    print(f"  Stored keys    : {s['key_count']}")

    code = v.consume_first_boot_recovery_code()
    if code:
        _print_recovery_code(code, occasion="first boot")

    if not s["keychain"] and s["encrypted"]:
        print()
        print("  WARNING: vault is encrypted on disk but the OS keychain has")
        print("  no master key. The brain will refuse to start until you run")
        print("  `feral key recover` (or set FERAL_VAULT_RECOVERY_CODE).")
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────
# Rotate
# ─────────────────────────────────────────────────────────────────────


def cmd_key_rotate(*, skip_confirm: bool = False) -> int:
    """Generate a new master key, re-encrypt the vault under it, swap
    atomically, print the new recovery code."""
    from security.vault import get_vault, VaultError

    try:
        v = get_vault()
    except VaultError as exc:
        print(f"  Cannot rotate: {exc}")
        return 1

    if not skip_confirm:
        ui_kit.brand_panel(
            "feral key rotate",
            body=(
                "About to rotate the vault master key.\n"
                "  - Previous master key will be REMOVED from the OS keychain.\n"
                "  - credentials.enc.prev kept until the next successful brain boot.\n"
                "  - A new recovery code will be printed ONCE. Write it down."
            ),
        )
        try:
            if not ui_kit.confirm("Continue with rotation?", default=False):
                print("  Cancelled.")
                return 1
        except KeyboardInterrupt:
            print()
            print("  Cancelled.")
            return 1

    try:
        new_code = v.rotate_master_key()
    except VaultError as exc:
        print()
        print(f"  Rotation FAILED: {exc}")
        print()
        print("  The vault file was NOT modified. Resolve the underlying")
        print("  issue (usually OS keychain access) and re-run.")
        return 1

    _print_recovery_code(new_code, occasion="rotation")
    return 0


# ─────────────────────────────────────────────────────────────────────
# Recover
# ─────────────────────────────────────────────────────────────────────


def cmd_key_recover(*, code: str = "") -> int:
    """Restore the OS keychain master key from a recovery code."""
    from security.vault import get_vault, VaultError, decode_recovery_code

    if not code:
        ui_kit.brand_panel(
            "feral key recover",
            body=(
                "Paste the recovery code you wrote down at first boot "
                "(or the most recent `feral key rotate`).\n"
                "Format: ABCD-EFGH-IJKL-MNOP-… (13 groups). "
                "Each character is masked as you paste."
            ),
        )
        try:
            code = ui_kit.password("Recovery code", allow_empty=False).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("  Cancelled.")
            return 1

    if not code:
        print("  No recovery code supplied. Aborting.")
        return 1

    try:
        decode_recovery_code(code)
    except ValueError as exc:
        print(f"  Recovery code is malformed: {exc}")
        return 1

    try:
        v = get_vault()
    except VaultError:
        os.environ["FERAL_VAULT_RECOVERY_CODE"] = code
        from security.vault import reset_vault, get_vault as _gv
        reset_vault()
        try:
            v = _gv()
        except VaultError as exc:
            print(f"  Recovery FAILED: {exc}")
            return 1

    try:
        v.restore_from_recovery_code(code)
    except VaultError as exc:
        print(f"  Recovery FAILED: {exc}")
        print()
        print("  The OS keychain was NOT modified. Double-check the code")
        print("  (case-insensitive, dashes/spaces ignored) and re-run.")
        return 1

    print("  Recovery succeeded.")
    print("  The OS keychain now holds the master key for this vault.")
    print("  You can run `feral key status` to confirm.")
    return 0


# ─────────────────────────────────────────────────────────────────────
# Per-provider multi-key (audit-r14 / lane-07 )
# ─────────────────────────────────────────────────────────────────────
#
# These commands wrap ``security.vault_keys`` (Wave 2 Lane 09's
# additive overlay on BlindVault). Multi-key support means an
# operator can stash both a personal "dev" OpenAI key and a team
# "prod" key under the same provider id without one clobbering the
# other; ``set_active_label`` decides which one the runtime resolves.
#
# Every command runs in pure-local mode — no brain WebSocket round-
# trip. ``add`` runs the post-add probe via ``security.probe.probe``
# directly; ``list`` reads the stored ``last_probe_*`` metadata so
# the operator sees green/red without paying another network call.


def _format_ts(ts):
    """Render an epoch second as 'Xs ago' / 'Xm ago' / 'Xh ago'."""
    import time as _time

    if ts is None:
        return "—"
    delta = max(0, _time.time() - float(ts))
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _probe_one_label(provider_id: str, *, force: bool = True) -> tuple[bool, str]:
    """Run the registry probe for ``provider_id`` and return
    ``(ok, detail)``. Imported lazily so ``feral key list`` (which
    only reads cached metadata) doesn't pay the import cost when no
    probe is needed."""
    import asyncio

    try:
        from security.probe import probe
    except Exception as exc:
        return False, f"probe registry unavailable: {exc}"

    try:
        result = asyncio.run(probe(provider_id, force=force))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(probe(provider_id, force=force))
        finally:
            loop.close()
    return bool(result.ok), result.detail or result.reason or "no detail"


def cmd_key_add(
    *,
    provider_id: str,
    label: str,
    api_key: str,
    set_active: bool = False,
    probe: bool = True,
) -> int:
    """Store a labeled API key under ``provider_id`` and probe it.

    The actual write goes through Lane 09's ``vault_keys`` overlay.
    After the write succeeds, we run ``security.probe.probe`` for
    ``provider_id`` so the operator sees a green/red verdict before
    the prompt returns. Probe metadata is persisted via
    ``record_probe_result`` so ``feral key list`` can render the
    same verdict later without another network call.
    """
    from security.vault_keys import (
        add_provider_key,
        record_probe_result,
        InvalidProviderId,
        InvalidLabel,
    )

    if not provider_id:
        print("  --provider is required.")
        return 2

    if not api_key:
        try:
            api_key = ui_kit.password(
                f"API key for {provider_id} (label='{label}')",
                allow_empty=False,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("  Cancelled.")
            return 1

    if not api_key:
        print("  No API key supplied. Aborting.")
        return 1

    try:
        entry = add_provider_key(
            provider_id, label, api_key, set_active=set_active,
        )
    except (InvalidProviderId, InvalidLabel, ValueError) as exc:
        print(f"  {exc}")
        return 2

    print()
    print(f"  Saved {entry.provider_id}:{entry.label}  "
          f"({entry.fingerprint})"
          + ("  [active]" if entry.is_active else ""))

    # Cross-cut #1 (v2026.5.42): when the new key is the active one,
    # poke the running brain so the next chat turn picks it up without
    # a restart. Best effort — if the brain isn't running, we print a
    # one-line hint instead of erroring out (the vault write is the
    # operator's source of truth either way).
    if set_active:
        _hint = _signal_brain_reload(entry.provider_id)
        if _hint:
            print(f"  {_hint}")

    if not probe:
        print("  --no-probe set; skipping validity check.")
        return 0

    print()
    print("  Probing key…")
    ok, detail = _probe_one_label(entry.provider_id, force=True)
    record_probe_result(entry.provider_id, entry.label, ok=ok)
    if ok:
        print(f"  ✔ Probe OK — {detail}")
        return 0

    print(f"  ✘ Probe FAILED — {detail}")
    print()
    print("  The key is stored but the API rejected it. Run")
    print(f"  `feral key add --provider {entry.provider_id} --label {entry.label}`")
    print("  again with a fresh key, or check the provider's dashboard.")
    return 1


def cmd_key_list(*, provider_id: str = "") -> int:
    """Print labeled keys + probe status for one or every provider.

    Empty ``provider_id`` lists every provider that has at least one
    labeled key. The output never includes the secret — only the
    fingerprint + metadata, matching the ``ProviderKeyEntry`` shape.
    """
    from security.vault_keys import (
        PROVIDER_KEYS_NAMESPACE,
        list_provider_keys,
        InvalidProviderId,
    )
    from security.vault import get_vault, VaultError

    try:
        vault = get_vault()
    except VaultError as exc:
        print(f"  Vault unavailable: {exc}")
        return 1

    if provider_id:
        try:
            entries = list_provider_keys(provider_id, vault=vault)
        except InvalidProviderId as exc:
            print(f"  {exc}")
            return 2
        provider_groups = {provider_id.strip().lower(): entries}
    else:
        # Walk the keys namespace, group by provider prefix.
        all_keys = vault.list_namespace(PROVIDER_KEYS_NAMESPACE)
        groups: dict[str, list] = {}
        for k in all_keys:
            if ":" not in k:
                continue
            pid = k.split(":", 1)[0]
            groups.setdefault(pid, [])
        provider_groups = {
            pid: list_provider_keys(pid, vault=vault) for pid in sorted(groups)
        }

    if not provider_groups:
        print("  No labeled provider keys stored.")
        print("  Add one with: feral key add --provider <id> --label default")
        return 0

    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
    except ImportError:
        console = None

    for pid, entries in provider_groups.items():
        if not entries:
            print(f"\n  [{pid}] (no labeled keys)")
            continue
        if console is not None:
            table = Table(title=f"[bold]{pid}[/bold]", show_lines=False)
            table.add_column("Label", style="bold")
            table.add_column("Active")
            table.add_column("Fingerprint", style="dim")
            table.add_column("Probe")
            table.add_column("Last probe")
            table.add_column("Last used")
            for e in entries:
                if e.last_probe_ok is True:
                    probe_cell = "[green]✔ ok[/green]"
                elif e.last_probe_ok is False:
                    probe_cell = "[red]✘ failed[/red]"
                else:
                    probe_cell = "[dim]—[/dim]"
                table.add_row(
                    e.label,
                    "✔" if e.is_active else "",
                    e.fingerprint,
                    probe_cell,
                    _format_ts(e.last_probe_at),
                    _format_ts(e.last_used_at),
                )
            console.print(table)
        else:
            print(f"\n  [{pid}]")
            for e in entries:
                marker = "*" if e.is_active else " "
                probe_str = (
                    "ok" if e.last_probe_ok is True
                    else ("failed" if e.last_probe_ok is False else "—")
                )
                print(
                    f"   {marker} {e.label:<20} {e.fingerprint:<30}  "
                    f"probe={probe_str}  "
                    f"last_probe={_format_ts(e.last_probe_at)}"
                )
    return 0


def cmd_key_remove(*, provider_id: str, label: str, skip_confirm: bool = False) -> int:
    """Remove a labeled API key from the vault."""
    from security.vault_keys import remove_provider_key, InvalidProviderId, InvalidLabel

    if not provider_id or not label:
        print("  Both --provider and --label are required.")
        return 2

    if not skip_confirm:
        ui_kit.brand_panel(
            "feral key remove",
            body=(
                f"About to remove the labeled key:\n"
                f"  provider: {provider_id}\n"
                f"  label:    {label}\n\n"
                "The encrypted secret + metadata will be deleted from the\n"
                "vault. Other labels for this provider are unaffected."
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
        removed = remove_provider_key(provider_id, label)
    except (InvalidProviderId, InvalidLabel) as exc:
        print(f"  {exc}")
        return 2

    if removed:
        print(f"  Removed {provider_id}:{label}.")
        return 0
    print(f"  No labeled key found at {provider_id}:{label}.")
    return 1


def cmd_key_rotate_provider(
    *,
    provider_id: str,
    label: str,
    api_key: str = "",
    skip_confirm: bool = False,
) -> int:
    """Rotate the per-provider labeled API key.

    Distinct from ``cmd_key_rotate`` (which rotates the *master* key
    that encrypts the whole vault). This path replaces just the
    labeled secret in place — fingerprint changes, ``last_probe_*``
    is cleared, and the post-rotate probe runs immediately so the
    operator sees the new key's validity before the prompt closes.
    """
    from security.vault_keys import (
        get_provider_key, add_provider_key, get_active_label,
        record_probe_result, InvalidProviderId, InvalidLabel,
    )

    if not provider_id:
        print("  --provider is required for per-provider rotate.")
        return 2

    if not skip_confirm:
        ui_kit.brand_panel(
            "feral key rotate (per-provider)",
            body=(
                f"Rotating {provider_id}:{label}.\n"
                "The current secret will be REPLACED with the new one.\n"
                "Other labels for this provider are unaffected.\n"
                "The new key is probed immediately."
            ),
        )
        try:
            if not ui_kit.confirm("Continue with rotation?", default=False):
                print("  Cancelled.")
                return 1
        except KeyboardInterrupt:
            print()
            print("  Cancelled.")
            return 1

    if not api_key:
        try:
            api_key = ui_kit.password(
                f"New API key for {provider_id}:{label}", allow_empty=False,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("  Cancelled.")
            return 1

    if not api_key:
        print("  No API key supplied. Aborting.")
        return 1

    try:
        existing = get_provider_key(provider_id, label)
    except (InvalidProviderId, InvalidLabel) as exc:
        print(f"  {exc}")
        return 2
    if existing is None:
        print(f"  No labeled key at {provider_id}:{label} — use `feral key add` instead.")
        return 1

    # Preserve active selection across the rotate.
    active = get_active_label(provider_id)
    keep_active = (active == label)

    entry = add_provider_key(
        provider_id, label, api_key, set_active=keep_active,
    )
    print(f"  Rotated {entry.provider_id}:{entry.label}  ({entry.fingerprint})"
          + ("  [active]" if entry.is_active else ""))

    print()
    print("  Probing new key…")
    ok, detail = _probe_one_label(provider_id, force=True)
    record_probe_result(provider_id, label, ok=ok)
    if ok:
        print(f"  ✔ Probe OK — {detail}")
        return 0
    print(f"  ✘ Probe FAILED — {detail}")
    print("  The new key is stored but the API rejected it; rotate again with a fresh key.")
    return 1


# ─────────────────────────────────────────────────────────────────────
# Recovery-code printing helper
# ─────────────────────────────────────────────────────────────────────


def _print_recovery_code(code: str, *, occasion: str) -> None:
    """Render the recovery code with framing so the user notices it.

    NEVER logged. NEVER echoed twice. The caller controls when this is
    invoked; in particular ``cmd_key_status`` only prints the first-boot
    code at the moment of vault construction (via
    ``consume_first_boot_recovery_code``), then forgets it.
    """
    bar = "  " + "─" * 60
    print()
    print(bar)
    print(f"  RECOVERY CODE — {occasion} (shown ONCE)")
    print(bar)
    print()
    print(f"     {code}")
    print()
    print("  Write this down NOW (paper, password manager, vault).")
    print("  It is the ONLY way to recover credentials if the OS keychain")
    print("  entry is lost. FERAL has no escrow copy.")
    print(bar)


# ─────────────────────────────────────────────────────────────────────
# Stand-alone entry point (for `python -m cli.key_commands ...`)
# ─────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    """Run a single `feral key …` command from a fresh argv. Mirrors
    the dispatch logic in cli/main.py so this module is testable in
    isolation."""
    parser = argparse.ArgumentParser(prog="feral key")
    sub = parser.add_subparsers(dest="subcommand")

    register_key_subparser(sub)

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if args.subcommand != "key":
        parser.print_help()
        return 2
    return dispatch_key_subcommand(args)


if __name__ == "__main__":
    raise SystemExit(main())
