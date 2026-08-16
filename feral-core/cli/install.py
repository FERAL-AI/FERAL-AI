"""
FERAL install subcommand
========================

Trust model (installer side)
----------------------------
When the user runs ``feral install <item_id>`` (or the legacy
``feral marketplace install <item_id>``) we treat the registry as a
transport, not a trust anchor. The real trust root is the publisher's
Ed25519 public key, which was registered out-of-band via
``feral publisher register``.

For every install we:

1. Resolve the registry base URL in priority order:
   ``--registry`` flag > ``FERAL_REGISTRY_URL`` env >
   ``~/.feral/config.yaml`` ``registry_url`` > ``https://registry.feral.sh``.
2. ``GET {registry}/api/v1/item/{id}`` — returns ``manifest``,
   ``download_url``, ``sha256``, ``signature`` (base64), and
   ``publisher_pubkey_hex``.
3. Download the tarball from ``download_url``.
4. Recompute the SHA-256 locally and require an exact match.
5. Verify the detached Ed25519 signature over the SHA-256 bytes using
   the publisher's public key. Any mismatch aborts with
   ``signature verification failed`` — we never touch disk layout.
6. Extract into ``~/.feral/skills/<id>/`` or ``~/.feral/daemons/<id>/``.
   ``kind=mcp`` bundles are stitched into ``~/.feral/mcp_servers.json``
   and announced to the running Brain (if any).
7. Best-effort hot-reload via ``POST /api/skills/reload`` so the
   installed skill is usable without a restart. "Best-effort" means the
   install still succeeds when no brain is running; it does not mean the
   outcome is unreported. A reload that the brain refused, or that no
   brain answered, is printed with its reason, and the closing line says
   "Restart the brain to use it" instead of "Ready to use."
"""

from __future__ import annotations

import json
import os
import sys
import tarfile
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any, Optional

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

try:
    from nacl.signing import VerifyKey  # type: ignore
    from nacl.encoding import HexEncoder  # type: ignore
    from nacl.exceptions import BadSignatureError  # type: ignore
    _NACL_AVAILABLE = True
except ImportError:
    VerifyKey = None  # type: ignore
    HexEncoder = None  # type: ignore
    BadSignatureError = Exception  # type: ignore
    _NACL_AVAILABLE = False


_HTTP_TIMEOUT = 30.0


def _feral_home() -> Path:
    env = os.environ.get("FERAL_HOME")
    if env:
        return Path(env)
    return Path.home() / ".feral"


def _require_nacl() -> None:
    if not _NACL_AVAILABLE:
        print("  pynacl is required for install. Install: pip install pynacl")
        sys.exit(1)


def _require_httpx() -> None:
    if httpx is None:
        print("  httpx is required for install. Install: pip install httpx")
        sys.exit(1)


def _fetch_item(registry: str, item_id: str) -> dict:
    url = f"{registry}/api/v1/item/{item_id}"
    try:
        resp = httpx.get(url, timeout=_HTTP_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        print(f"  Could not reach registry at {url}: {exc}")
        sys.exit(1)
    if resp.status_code == 404:
        print(f"  Item '{item_id}' not found in registry {registry}.")
        sys.exit(1)
    if resp.status_code >= 400:
        print(f"  Registry error ({resp.status_code}) fetching item: {resp.text[:500]}")
        sys.exit(1)
    try:
        return resp.json()
    except Exception:
        print(f"  Registry returned non-JSON response from {url}.")
        sys.exit(1)


def _download_bundle(download_url: str) -> Path:
    tmp_dir = Path(tempfile.mkdtemp(prefix="feral-install-"))
    out = tmp_dir / "bundle.tar.gz"
    try:
        with httpx.stream("GET", download_url, timeout=_HTTP_TIMEOUT, follow_redirects=True) as resp:
            if resp.status_code >= 400:
                print(f"  Download failed ({resp.status_code}) from {download_url}.")
                sys.exit(1)
            with open(out, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    except httpx.HTTPError as exc:
        print(f"  Download failed: {exc}")
        sys.exit(1)
    return out


def _sha256_file(path: Path) -> bytes:
    h = sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.digest()


def _verify(item: dict, tarball: Path) -> bytes:
    """Verify sha256 + signature. Returns the raw digest bytes on success."""
    import base64

    expected_hex = str(item.get("sha256", "")).lower().strip()
    actual = _sha256_file(tarball)
    actual_hex = actual.hex()
    if not expected_hex or actual_hex != expected_hex:
        print(f"  signature verification failed: sha256 mismatch ({actual_hex} != {expected_hex})")
        sys.exit(1)

    sig_b64 = item.get("signature_b64") or item.get("signature") or ""
    pub_hex = item.get("publisher_pubkey") or item.get("publisher_pubkey_hex") or ""
    if not sig_b64 or not pub_hex:
        print("  signature verification failed: missing signature or publisher key in registry response")
        sys.exit(1)

    try:
        vk = VerifyKey(pub_hex, encoder=HexEncoder)
        # The registry signs over ``sha256_hex.encode("ascii")``
        # (see feral-registry/feral_registry/signing.py::verify_bundle_signature).
        # The client must verify the same bytes.
        vk.verify(actual_hex.encode("ascii"), base64.b64decode(sig_b64))
    except (BadSignatureError, ValueError, TypeError) as exc:
        print(f"  signature verification failed: {exc}")
        sys.exit(1)
    return actual


def _safe_extract(tarball: Path, dest: Path) -> None:
    """Extract the tarball, refusing any path escape (CVE-2007-4559 style)."""
    dest.mkdir(parents=True, exist_ok=True)
    dest_abs = dest.resolve()
    with tarfile.open(tarball, "r:gz") as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest_abs) + os.sep) and target != dest_abs:
                print(f"  signature verification failed: tar member escapes target dir ({member.name})")
                sys.exit(1)
        tar.extractall(dest)


# ─────────────────────────────────────────────
# Brain-local helpers (hot reload, MCP announce)
# ─────────────────────────────────────────────

def _brain_base_url() -> Optional[str]:
    """Return a best-effort http base URL for the locally running Brain."""
    try:
        from config.runtime import brain_public_base_url
        return brain_public_base_url().rstrip("/")
    except Exception:
        port = os.environ.get("FERAL_PORT", "8765")
        return f"http://127.0.0.1:{port}"


def _brain_auth_headers() -> dict[str, str]:
    try:
        from api.keys import load_api_key

        key = load_api_key()
    except Exception:
        key = None
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


def _maybe_reload_skill(skill_id: str) -> bool:
    """Hot-reload ``skill_id`` on the running brain and say what happened.

    Returns True only when the brain confirmed the reload, i.e. only when
    the installed skill is usable right now.

    This used to be unreportable by construction: the body was
    ``if resp.status_code < 400: return``, and then it fell off the end
    on every other path too, so a refused reload, a 500, an unreachable
    brain and a successful reload were all the same ``None``. ``feral
    install`` then printed "Ready to use." over a brain that had not
    loaded the skill and would not until a restart.

    ``ok is False`` is checked separately from the status because a brain
    that predates the reload-status fix answers a reload that did nothing
    with HTTP 200 and ``{"ok": false, "skill_id": ...}``, no ``error``
    key. Trusting the status alone here would reproduce exactly the
    defect this call site exists to report.
    """
    base = _brain_base_url()
    if not base or httpx is None:
        print(f"  No brain URL or httpx available, so {skill_id} was not hot-reloaded. "
              "It loads on the next brain start.")
        return False
    try:
        resp = httpx.post(
            f"{base}/api/skills/reload",
            params={"skill_id": skill_id},
            headers=_brain_auth_headers(),
            timeout=5.0,
        )
    except Exception as exc:
        print(f"  No running brain answered at {base} ({exc.__class__.__name__}), "
              f"so {skill_id} was not hot-reloaded. It loads on the next brain start.")
        return False

    body: Any = {}
    try:
        body = resp.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    if resp.status_code < 400 and body.get("ok") is not False:
        return True

    reason = (
        body.get("error")
        or body.get("detail")
        or (resp.text or "").strip()[:200]
        or f"HTTP {resp.status_code}"
    )
    print(f"  The brain did not hot-reload {skill_id}: {reason}")
    print(f"  The files are installed. Restart the brain to load {skill_id}.")
    return False


def _announce_mcp(server_config: dict) -> None:
    base = _brain_base_url()
    if not base or httpx is None:
        return
    try:
        httpx.post(
            f"{base}/api/mcp/connect",
            json=server_config,
            headers=_brain_auth_headers(),
            timeout=5.0,
        )
    except Exception:
        pass


def _append_mcp_config(server_config: dict) -> None:
    path = _feral_home() / "mcp_servers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: Any = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            existing = {}
    name = server_config.get("name") or server_config.get("id") or "unnamed"
    if isinstance(existing, dict):
        existing.setdefault("servers", {}) if "servers" in existing or not existing else existing
        # If the file is a flat mapping { name: cfg }, keep that shape.
        if "servers" in existing and isinstance(existing["servers"], dict):
            existing["servers"][name] = server_config
        else:
            existing[name] = server_config
        path.write_text(json.dumps(existing, indent=2))
    else:
        path.write_text(json.dumps({name: server_config}, indent=2))


# ─────────────────────────────────────────────
# Main install flow
# ─────────────────────────────────────────────

def cmd_install(item_id: str, registry: Optional[str] = None) -> None:
    """Install a published item by id from the FERAL registry."""
    from cli import ui_kit
    from cli.publish import registry_base_url

    _require_nacl()
    _require_httpx()

    if not item_id:
        print("  Usage: feral install <item_id>")
        sys.exit(2)

    base = registry_base_url(registry)
    ui_kit.brand_panel(f"feral install — {item_id}", body=f"registry: {base}")
    ui_kit.banner_line(f"Fetching {item_id} from {base}…")
    item = _fetch_item(base, item_id)

    manifest = item.get("manifest") or {}
    kind = (item.get("kind") or manifest.get("kind") or "skill").lower()
    download_url = item.get("download_url")
    if not download_url:
        print("  Registry response missing 'download_url'.")
        sys.exit(1)

    name = manifest.get("name") or manifest.get("brand", {}).get("name") or manifest.get("id") or item_id
    version = manifest.get("version", "?")

    ui_kit.banner_line(f"Downloading bundle ({kind}) {name} v{version}…")
    tarball = _download_bundle(download_url)
    ui_kit.banner_line("Verifying SHA-256 + Ed25519 signature…")
    _verify(item, tarball)

    home = _feral_home()

    # "Ready to use." is a claim about the running brain, not about the
    # files on disk, so it is only printed when the hot-reload said so.
    live = dispatch_install(kind, manifest, tarball, item_id, home)
    if live:
        ui_kit.banner_line(f"Installed {name} v{version}. Ready to use.", style="green")
    else:
        ui_kit.banner_line(
            f"Installed {name} v{version}, but it is not loaded in the running brain. "
            "Restart the brain to use it.",
            style="yellow",
        )


def dispatch_install(
    kind: str,
    manifest: dict,
    tarball: Path,
    item_id: str,
    home: Path,
) -> bool:
    """Route a verified bundle to the correct install target per kind.

    Each of the eight registry categories extracts (or announces) to a
    different location under ``~/.feral/`` and pings a different
    hot-reload endpoint on the running Brain (best-effort).

    Returns whether the item is live in the running brain now. Only the
    ``skill`` path can answer that today: it is the one whose hot-reload
    endpoint reports its own outcome. The others go through
    ``_maybe_post``, which is silent by construction and cannot tell a
    reload that worked from one that did not, so they answer True and the
    caller's "Ready to use." stays as truthful as it was before.
    """
    kind = (kind or "skill").lower()

    if kind == "skill":
        skill_id = manifest.get("skill_id") or manifest.get("id") or item_id
        dest = home / "skills" / str(skill_id)
        dest.mkdir(parents=True, exist_ok=True)
        _safe_extract(tarball, dest)
        return _maybe_reload_skill(str(skill_id))

    if kind == "daemon":
        daemon_id = manifest.get("node_id") or manifest.get("id") or item_id
        dest = home / "daemons" / str(daemon_id)
        dest.mkdir(parents=True, exist_ok=True)
        _safe_extract(tarball, dest)
        _maybe_post("/api/devices/register", {"daemon_id": str(daemon_id)})
        return True

    if kind == "mcp":
        server_config = manifest.get("server") or manifest.get("mcp_command") or manifest
        _append_mcp_config(server_config if isinstance(server_config, dict) else {"command": server_config})
        _announce_mcp(server_config if isinstance(server_config, dict) else {"command": server_config})
        return True

    if kind == "channel":
        channel_id = manifest.get("channel_id") or manifest.get("id") or item_id
        dest = home / "channels" / str(channel_id)
        dest.mkdir(parents=True, exist_ok=True)
        _safe_extract(tarball, dest)
        _maybe_post("/api/channels/reload", {"channel_id": str(channel_id)})
        return True

    if kind == "provider":
        provider_id = manifest.get("provider_id") or manifest.get("id") or item_id
        dest = home / "providers" / str(provider_id)
        dest.mkdir(parents=True, exist_ok=True)
        _safe_extract(tarball, dest)
        _maybe_post("/api/providers/reload", {"provider_id": str(provider_id)})
        return True

    if kind == "memory":
        memory_id = manifest.get("memory_id") or manifest.get("id") or item_id
        dest = home / "memory-backends" / str(memory_id)
        dest.mkdir(parents=True, exist_ok=True)
        _safe_extract(tarball, dest)
        print(f"  Memory backend {memory_id} extracted. Enable it in Settings → Memory.")
        return True

    if kind == "workflow":
        workflow_id = manifest.get("workflow_id") or manifest.get("id") or item_id
        dest = home / "workflows" / str(workflow_id)
        dest.mkdir(parents=True, exist_ok=True)
        _safe_extract(tarball, dest)
        _maybe_post("/api/workflows/reload", {"workflow_id": str(workflow_id)})
        return True

    if kind == "agent":
        agent_id = manifest.get("agent_id") or manifest.get("id") or item_id
        dest = home / "agents" / str(agent_id)
        dest.mkdir(parents=True, exist_ok=True)
        _safe_extract(tarball, dest)
        _maybe_post("/api/mitosis/reload", {"agent_id": str(agent_id)})
        return True

    print(f"  Unknown item kind '{kind}'. Supported: skill, daemon, mcp, channel, provider, memory, workflow, agent.")
    sys.exit(1)


def _maybe_post(path: str, body: dict) -> None:
    """Best-effort POST to a running Brain. Silent on failure — the Brain
    may be offline; installed assets are still on disk for next startup.
    """
    base = _brain_base_url()
    if not base or httpx is None:
        return
    try:
        httpx.post(
            f"{base}{path}",
            json=body,
            headers=_brain_auth_headers(),
            timeout=5.0,
        )
    except Exception:
        pass
