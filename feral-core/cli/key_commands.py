"""
``feral key`` — vault key lifecycle CLI (W9).

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

audit-r14 / lane-06 (v2026.5.38) — the W16 ``feral key list / migrate
/ rotate --provider --agent --key`` commands and the per-agent
``AuthProfileFileStore`` they wrote to were removed. The audit
finding documented zero runtime consumers outside this file and the
W16 unit tests, and Lane 03's Wave 1 work made BlindVault the single
credential authority that every provider, skill, and integration
resolves through. Keeping W16's plaintext-JSON shadow store alive
forced operators to maintain two truths for the same key; deleting it
finishes the audit-r12 #2 fix.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from cli import ui_kit


# ─────────────────────────────────────────────────────────────────────
# Argparse registration (called from cli/main.py)
# ─────────────────────────────────────────────────────────────────────


def register_key_subparser(sub: "argparse._SubParsersAction") -> None:
    """Register ``feral key {status,rotate,recover}`` under the main
    ``feral`` argparse subparsers group."""
    key_p = sub.add_parser(
        "key",
        help="Manage the encrypted credential vault (status, rotate, recover)",
    )
    key_sub = key_p.add_subparsers(dest="action")

    key_sub.add_parser(
        "status",
        help="Show vault status: encrypted on disk, master key in keychain, "
             "presence of legacy/rotation backups.",
    )

    rotate_p = key_sub.add_parser(
        "rotate",
        help="Generate a new master key, re-encrypt the vault, and print "
             "a fresh recovery code (shown ONCE).",
    )
    rotate_p.add_argument(
        "--yes",
        action="store_true",
        dest="key_confirm",
        help="Skip the interactive confirmation prompt (use in scripts).",
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


def dispatch_key_subcommand(args) -> int:
    action = getattr(args, "action", None) or "status"
    if action == "status":
        return cmd_key_status()
    if action == "rotate":
        return cmd_key_rotate(skip_confirm=getattr(args, "key_confirm", False))
    if action == "recover":
        return cmd_key_recover(code=getattr(args, "code", "") or "")
    print(
        f"Unknown action: {action}. "
        f"Try one of: status, rotate, recover."
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
