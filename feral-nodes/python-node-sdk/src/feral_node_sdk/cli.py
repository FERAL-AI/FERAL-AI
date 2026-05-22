"""Shippable CLI for the Python node SDK.

Vendors bundle this with their daemon so operators can run
``python -m feral_node_sdk pair --node-id foo --brain wss://...`` without
writing any code. Currently supports the `pair`, `discover`, and `version`
subcommands.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional

from . import __version__
from .discovery import discover_brain
from .pairing import load_key, pair


def _cmd_pair(args: argparse.Namespace) -> int:
    brain = args.brain
    if not brain:
        brain = asyncio.run(discover_brain(timeout_s=3.0))
    if not brain:
        print("error: no --brain provided and mDNS discovery found nothing.", file=sys.stderr)
        return 2
    try:
        asyncio.run(pair(
            node_id=args.node_id,
            brain_url=brain,
            code=args.code,
            name=args.name or args.node_id,
            timeout_s=args.timeout,
            verify_tls=not args.insecure,
        ))
        return 0
    except TimeoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


def _cmd_discover(args: argparse.Namespace) -> int:
    url: Optional[str] = asyncio.run(discover_brain(timeout_s=args.timeout))
    if url:
        print(url)
        return 0
    print("no brain found", file=sys.stderr)
    return 1


def _cmd_key(args: argparse.Namespace) -> int:
    k = load_key(args.node_id)
    if k:
        print(k)
        return 0
    print("no key stored", file=sys.stderr)
    return 1


def _cmd_run(args: argparse.Namespace) -> int:
    """Lane 11 (audit-r14) — long-lived bridge daemon.

    Stores the pre-minted ``--token`` to
    ``~/.feral/node-keys/<node_id>.key`` (matching the pair flow's
    persistence) and opens a HUP WebSocket loop against ``--brain-url``
    so the LaunchAgent / systemd unit installed by
    ``scripts/install-phone-bridge.sh`` keeps the node connected
    across reboots.

    The daemon is deliberately tiny — heartbeats + reconnect handled
    by the SDK. Adapters can be registered by importing
    ``feral_node_sdk.FeralNode`` in a host process; this command is
    the no-host fallback the install script ships.
    """
    from .node import FeralNode
    from .pairing import save_key

    save_key(args.node_id, args.token)

    async def _loop() -> None:
        node = FeralNode(
            node_id=args.node_id,
            brain_url=args.brain_url,
            api_key=args.token,
            name=args.name or args.node_id,
            node_type=args.node_type,
            capabilities=args.capabilities or [],
        )
        # The SDK's connect() already runs the handshake + heartbeat
        # loop; sleep forever until the process is killed.
        await node.connect()
        try:
            while True:
                await asyncio.sleep(60.0)
        finally:
            await node.disconnect()

    try:
        asyncio.run(_loop())
        return 0
    except KeyboardInterrupt:
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="feral-node",
        description="FERAL HUP v1 node CLI (pair, discover, inspect keys).",
    )
    p.add_argument("--version", action="version", version=f"feral-node-sdk {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pair", help="Run the 6-digit pairing flow.")
    sp.add_argument("--node-id", required=True)
    sp.add_argument("--brain", default=None, help="wss:// URL; defaults to mDNS discovery.")
    sp.add_argument("--code", default=None, help="Use a specific 6-digit code (default: random).")
    sp.add_argument("--name", default="", help="Human-readable device name.")
    sp.add_argument("--timeout", type=float, default=300.0)
    sp.add_argument("--insecure", action="store_true", help="Skip TLS verification (dev only).")
    sp.set_defaults(func=_cmd_pair)

    sd = sub.add_parser("discover", help="Print the URL of the first FERAL brain on the LAN.")
    sd.add_argument("--timeout", type=float, default=3.0)
    sd.set_defaults(func=_cmd_discover)

    sk = sub.add_parser("key", help="Print the stored API key for a node (if any).")
    sk.add_argument("--node-id", required=True)
    sk.set_defaults(func=_cmd_key)

    sr = sub.add_parser(
        "run",
        help=(
            "Long-lived bridge daemon: persists --token, opens the HUP "
            "WebSocket to --brain-url, and stays connected until killed. "
            "Used by scripts/install-phone-bridge.sh under launchctl/systemd."
        ),
    )
    sr.add_argument("--node-id", required=True)
    sr.add_argument("--brain-url", required=True, help="ws:// or wss:// URL.")
    sr.add_argument("--token", required=True, help="Pre-minted pair token.")
    sr.add_argument("--name", default="", help="Human-readable device name.")
    sr.add_argument(
        "--node-type", default="bridge",
        help="HUP node_type advertised in node_register (default: bridge).",
    )
    sr.add_argument(
        "--capability", dest="capabilities", action="append", default=[],
        help="Capability to advertise; repeat for multiple.",
    )
    sr.set_defaults(func=_cmd_run)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
