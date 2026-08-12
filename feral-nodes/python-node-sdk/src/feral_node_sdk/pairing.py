"""First-time pairing helpers for HUP v1 daemons.

Implements the client side of the 6-digit-code flow described in
`HUP_SPEC.md` §4.1: generate a code, poll the brain's pair-status endpoint
until the user types the code, then persist the returned API key to
``~/.feral/node-keys/<safe>.key`` with mode 0600, where ``<safe>`` is
:func:`key_filename` as specified in `HUP_SPEC.md` §4.1.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import ssl
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("feral_node_sdk.pairing")

KEYS_DIR = Path.home() / ".feral" / "node-keys"

# Exactly the brain's NodeRegisterPayload node_id class, and deliberately
# ASCII. This used to be `c.isalnum() or c in "._-:"`, and str.isalnum() is
# Unicode-aware, so "café" was written verbatim here while the TypeScript SDK
# wrote "caf_" for the same node.
_DISALLOWED_IN_KEY_FILENAME = re.compile(r"[^A-Za-z0-9._:-]")
_MAX_NODE_ID_LENGTH = 128
_DISAMBIGUATOR_LENGTH = 8


def key_filename(node_id: str) -> str:
    """Canonical key filename for ``node_id``. See `HUP_SPEC.md` §4.1 step 5.

    Mirrored by ``keyFilename`` in ``feral-nodes/ts-node-sdk/src/pairing.ts``
    and pinned by the shared fixture table at
    ``feral-nodes/spec-fixtures/node_key_filename.json``.

    The two SDKs each derived this by hand from the spec's prose and got
    different answers, so a node paired through one silently re-paired under
    the other: "sensor 01" was ``sensor01.key`` in Python and ``sensor_01.key``
    in TypeScript.

    Both old rules also mapped distinct node ids onto one file, which is the
    worse half. Python dropped disallowed characters, so every all-punctuation
    node id and the empty one all wrote to a hidden file literally named
    ``.key``. TypeScript replaced them, so "a b" and "a_b" shared
    ``a_b.key``. The hash suffix is what makes the mapping injective again;
    without it, replacement alone is still many-to-one.

    The suffix is applied only when sanitising changed something, so every node
    id the brain accepts keeps the filename both SDKs already write and no
    working pairing moves.
    """
    sanitised = _DISALLOWED_IN_KEY_FILENAME.sub("_", node_id)
    if sanitised == node_id and 1 <= len(node_id) <= _MAX_NODE_ID_LENGTH:
        return f"{sanitised}.key"
    # Truncated before hashing so an over-long node id cannot produce a
    # filename the filesystem refuses; the hash still covers the whole id.
    digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()
    return (
        f"{sanitised[:_MAX_NODE_ID_LENGTH]}"
        f"-{digest[:_DISAMBIGUATOR_LENGTH]}.key"
    )


def _key_path(node_id: str) -> Path:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    return KEYS_DIR / key_filename(node_id)


def load_key(node_id: str) -> Optional[str]:
    p = _key_path(node_id)
    if not p.exists():
        return None
    return p.read_text().strip() or None


def save_key(node_id: str, api_key: str) -> Path:
    p = _key_path(node_id)
    p.write_text(api_key.strip() + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def generate_code() -> str:
    """Generate a cryptographically-random 6-digit pairing code (leading zeros preserved)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _http_base(brain_url: str) -> str:
    u = brain_url
    if u.startswith("wss://"):
        u = "https://" + u[len("wss://"):]
    elif u.startswith("ws://"):
        u = "http://" + u[len("ws://"):]
    if "/v1/node" in u:
        u = u.split("/v1/node", 1)[0]
    return u.rstrip("/")


async def pair(
    node_id: str,
    brain_url: str,
    *,
    name: str = "",
    code: Optional[str] = None,
    poll_interval_s: float = 2.0,
    timeout_s: float = 300.0,
    verify_tls: bool = True,
) -> str:
    """Run the interactive 6-digit pairing flow and return the API key.

    Prints the pairing code to stdout so the user can type it into
    the FERAL UI. Blocks until the brain confirms or `timeout_s` elapses.
    """
    code = code or generate_code()
    base = _http_base(brain_url)
    print(f"\n  FERAL pairing code: {code[:3]} {code[3:]}\n")
    print("  → Open FERAL → Settings → Devices → Pair and enter the code.")
    print(f"  (Will wait up to {int(timeout_s)}s against {base}…)\n")

    ctx: Optional[ssl.SSLContext] = None
    if base.startswith("https://") and not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s

    def _poll() -> Optional[str]:
        url = f"{base}/api/devices/pair/status?code={code}&node_id={node_id}"
        try:
            with urllib.request.urlopen(url, timeout=5, context=ctx) as resp:
                data = json.loads(resp.read().decode())
                if data.get("status") == "paired" and data.get("token"):
                    return str(data["token"])
        except Exception as exc:
            logger.debug("pair-status poll failed: %s", exc)
        return None

    def _announce() -> None:
        url = f"{base}/api/devices/pair/announce"
        body = json.dumps({"code": code, "node_id": node_id, "name": name or node_id}).encode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5, context=ctx).read()
        except Exception as exc:
            logger.debug("pair-announce failed (non-fatal): %s", exc)

    await loop.run_in_executor(None, _announce)

    while loop.time() < deadline:
        token = await loop.run_in_executor(None, _poll)
        if token:
            save_key(node_id, token)
            print(f"  ✓ Paired. API key saved to {_key_path(node_id)}")
            return token
        await asyncio.sleep(poll_interval_s)

    raise TimeoutError("Pairing timed out; ask the user to try again.")
