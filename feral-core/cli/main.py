#!/usr/bin/env python3
"""
FERAL CLI — Interactive Terminal Agent
========================================
Connects to the FERAL Brain via the same WebSocket used by the web client.

Usage:
    feral                          # Interactive REPL
    feral "search the web for X"   # One-shot command
    feral status                   # System health
    feral devices                  # List connected hardware
    feral skills                   # List loaded skills
    feral identity                 # Show/edit agent identity
"""

import argparse
import asyncio
import json
import os
import platform
import shutil
import signal
import sys
import threading
from importlib import metadata as importlib_metadata
from pathlib import Path
from urllib.parse import urlparse

# audit-r14 / lane-07 (R2-002) — `feral --version` MUST NOT open a
# WebSocket to the brain. Importing ``websockets`` at module load
# also pulls in ``http``/``ssl`` machinery that the version probe
# never needs; the repl/one_shot paths import lazily via
# :func:`_require_websockets`. ``httpx`` import itself does NOT
# make any network calls (verified by wave1-summary §R2-002), so we
# import it eagerly to keep the existing ``cli_main.httpx`` mock
# surface intact. Pure-local commands still exit before any
# ``httpx.get/.post`` call site runs.
websockets = None  # populated by _require_websockets()

try:
    import httpx
except ImportError:
    httpx = None


def _require_websockets():
    """Lazy-import + cache the ``websockets`` package.

    Returns the imported module. Used by :func:`repl` and
    :func:`one_shot` — the only paths that actually open a brain
    WebSocket. Pure-local commands (``--version``, ``doctor``,
    ``key`` …) MUST NOT call this helper.
    """
    global websockets
    if websockets is None:
        try:
            import websockets as _ws
        except ImportError:
            print("websockets package required. Install: pip install websockets")
            sys.exit(1)
        websockets = _ws
    return websockets


from version import VERSION as __version__
from config.loader import feral_home
from config.runtime import (
    brain_bind_host,
    brain_port,
    brain_public_base_url,
    brain_public_host,
    brain_public_port,
    brain_public_scheme,
    brain_tls_enabled,
    hydrate_brain_runtime_env,
    record_bound_host,
)


# ── R2-002 ───────────────────────────────────────────────────────────
# Pure-local commands MUST NOT touch the network. Needs-brain commands
# may. The lists below are the source of truth referenced by:
#   * the early dispatch in :func:`main` (so ``--version`` short-
#     circuits before any parser cost)
#   * ``tests/test_cli_pure_local.py`` (R2-002 CI gate)
#   * ``tests/test_cli_no_phantom_commands.py`` (docs↔CLI parity)
# Update both lists when adding a new top-level command.
PURE_LOCAL_SUBCOMMANDS = frozenset({
    "doctor", "setup", "key", "grant", "access", "pair",
    "voice", "models", "integrations", "checkpoints",
    "install-service", "uninstall-service",
    "service-status", "logs", "stop", "restart",
    "wake-test", "publisher", "publish", "app",
})
NEEDS_BRAIN_SUBCOMMANDS = frozenset({
    "status", "devices", "skills", "identity",
    "memory", "sync", "twin", "marketplace", "install",
    "bridge",  # bridge install runs a script that contacts brain
    "start", "serve", "demo",
})


def _runtime_http_base() -> str:
    return brain_public_base_url().rstrip("/")


def _runtime_ws_url() -> str:
    parsed = urlparse(_runtime_http_base())
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{ws_scheme}://{parsed.hostname}{port}/v1/session"


WS_URL = _runtime_ws_url()
HTTP_BASE = _runtime_http_base()

BANNER = f"""
╔══════════════════════════════════════╗
║   🦝   F E R A L                       ║
║   Unleashed AI  v{__version__:<21s}║
╚══════════════════════════════════════╝
  Type a message to chat. Commands:
    /status   — system health
    /devices  — connected hardware
    /skills   — loaded skills
    /identity — agent identity
    /quit     — exit
"""


def _http_get(path: str) -> dict:
    """Quick synchronous HTTP GET to the Brain REST API."""
    if httpx:
        try:
            r = httpx.get(f"{HTTP_BASE}{path}", timeout=5)
            return r.json()
        except Exception as e:
            return {"error": str(e)}
    try:
        import urllib.request
        req = urllib.request.Request(f"{HTTP_BASE}{path}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _http_post(path: str, payload: dict) -> dict:
    """Synchronous HTTP POST — same URL base as _http_get."""
    if httpx:
        try:
            r = httpx.post(f"{HTTP_BASE}{path}", json=payload, timeout=10)
            try:
                return r.json()
            except Exception:
                return {"error": f"non-json {r.status_code}", "text": r.text}
        except Exception as e:
            return {"error": str(e)}
    try:
        import urllib.request
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{HTTP_BASE}{path}", data=data, method="POST",
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _http_delete(path: str) -> dict:
    """Synchronous HTTP DELETE."""
    if httpx:
        try:
            r = httpx.delete(f"{HTTP_BASE}{path}", timeout=10)
            try:
                return r.json()
            except Exception:
                return {"error": f"non-json {r.status_code}", "text": r.text}
        except Exception as e:
            return {"error": str(e)}
    try:
        import urllib.request
        req = urllib.request.Request(f"{HTTP_BASE}{path}", method="DELETE")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _installed_pkg_info() -> tuple[str, str]:
    """Return installed package version and location."""
    try:
        version = importlib_metadata.version("feral-ai")
        dist = importlib_metadata.distribution("feral-ai")
        location = str(dist.locate_file(""))
        return version, location
    except Exception:
        return "unknown", "unknown"


def cmd_stop():
    """Stop the FERAL Brain service (and any matching launchd plist)."""
    from cli import daemon as _daemon
    from cli.ui_kit import banner_line as _banner

    if not _daemon.is_service_supported():
        _banner("Service control is only available on macOS and Linux.", style="yellow")
        return

    status = _daemon.service_status()
    if not status.get("installed") and not status.get("running"):
        _banner("No FERAL Brain service installed.", style="dim")
        return

    ok = _daemon.stop_service()
    if ok:
        _banner(f"Stopped {_daemon.SERVICE_LABEL}.")
    else:
        _banner(f"Failed to stop {_daemon.SERVICE_LABEL} — check `feral status` and the launchd log.", style="red")


def cmd_restart():
    """Restart the FERAL Brain service. Re-renders the plist."""
    from cli import daemon as _daemon
    from cli.ui_kit import banner_line as _banner, brand_panel as _panel

    if not _daemon.is_service_supported():
        _banner("Service control is only available on macOS and Linux.", style="yellow")
        return

    _banner(f"Restarting {_daemon.SERVICE_LABEL}...")
    status = _daemon.restart_service()
    body = (
        f"[bold]Label:[/bold] {_daemon.SERVICE_LABEL}\n"
        f"[bold]PID:[/bold]   {status.get('pid') or '—'}\n"
        f"[bold]State:[/bold] {status.get('state') or 'starting'}"
    )
    _panel("Brain service restarted", body)


def cmd_service_status():
    """Show launchd / systemd state for the FERAL Brain service.

    Distinct from ``cmd_status`` (which hits the brain's HTTP API for
    a live dashboard snapshot). This one inspects the OS process
    manager, which is what an operator wants when the brain isn't
    responding to HTTP and they need to know whether it crashed.
    """
    from cli import daemon as _daemon
    from cli.ui_kit import brand_panel as _panel, banner_line as _banner

    if not _daemon.is_service_supported():
        _banner("Service control is only available on macOS and Linux.", style="yellow")
        return

    status = _daemon.service_status()
    if not status.get("installed"):
        _banner("No FERAL Brain service installed. Run `feral start` to install.", style="dim")
        return

    pid = status.get("pid")
    state = status.get("state") or ("running" if status.get("running") else "stopped")
    body = (
        f"[bold]Label:[/bold]   {_daemon.SERVICE_LABEL}\n"
        f"[bold]PID:[/bold]     {pid if pid is not None else '—'}\n"
        f"[bold]State:[/bold]   {state}\n"
        f"[bold]Plist:[/bold]   {status.get('plist', '—')}\n"
        f"[bold]Stdout:[/bold]  {status.get('stdout_log', '—')}\n"
        f"[bold]Stderr:[/bold]  {status.get('stderr_log', '—')}"
    )
    _panel("Service status", body)


def cmd_logs(*, follow: bool = True, n: int = 50, stderr: bool = False):
    """Tail the brain's launchd / systemd log file. Ctrl+C to exit."""
    from cli import daemon as _daemon
    from cli.ui_kit import banner_line as _banner

    if not _daemon.is_service_supported():
        _banner("Service control is only available on macOS and Linux.", style="yellow")
        return

    stdout_log, stderr_log = _daemon.log_paths()
    target = stderr_log if stderr else stdout_log

    if not target.exists():
        _banner(f"No log file yet at {target}. Run `feral start` first.", style="dim")
        return

    _banner(
        f"Tailing {target} — Ctrl+C to exit.",
        style="dim",
    )
    args = ["tail", "-n", str(n)]
    if follow:
        args.append("-F")
    args.append(str(target))
    try:
        subprocess_proc = __import__("subprocess").Popen(args)
        try:
            subprocess_proc.wait()
        except KeyboardInterrupt:
            subprocess_proc.terminate()
    except FileNotFoundError:
        _banner("`tail` not found on PATH — print the file contents instead.", style="yellow")
        sys.stdout.write(target.read_text())


def cmd_status():
    data = _http_get("/api/dashboard")
    if "error" in data:
        print(f"  Error: {data['error']}")
        return
    print(f"  Sessions:   {data.get('session_count', '?')}")
    print(f"  Devices:    {data.get('device_count', '?')}")
    print(f"  Skills:     {data.get('skills_count', '?')}")
    print(f"  LLM:        {'ready' if data.get('llm_available') else 'not connected'}")
    print(f"  Audio:      {'ready' if data.get('audio_available') else 'off'}")
    print(f"  WASM:       {'ready' if data.get('wasm_available') else 'disabled'}")
    print(f"  Wake Word:  {'on' if data.get('wake_word_enabled') else 'off'}")
    sync = data.get("sync", {})
    print(f"  Sync:       {'running' if sync.get('running') else 'off'} ({sync.get('peer_count', 0)} peers)")
    mem = data.get("memory", {})
    print(f"  Memory:     {mem.get('notes', 0)} notes, {mem.get('episodes', 0)} episodes, {mem.get('knowledge_triples', 0)} knowledge")


def cmd_devices():
    data = _http_get("/api/devices")
    devices = data.get("devices", [])
    if not devices:
        print("  No devices connected.")
        return
    for d in devices:
        status = "connected" if d.get("connected") else "disconnected"
        print(f"  [{status}] {d.get('node_id', '?')} — {d.get('type', 'unknown')}")


def cmd_skills():
    data = _http_get("/skills")
    if isinstance(data, list):
        if not data:
            print("  No skills loaded.")
            return
        for s in data:
            print(f"  {s['name']} ({s['skill_id']}) — {s.get('endpoints', 0)} endpoints")
    else:
        print(f"  Error: {data.get('error', 'unknown')}")


def cmd_identity():
    data = _http_get("/api/identity")
    if "error" in data:
        print(f"  Error: {data['error']}")
        return
    print(f"  Name:        {data.get('name', '?')}")
    print(f"  Tagline:     {data.get('tagline', '?')}")
    print(f"  Personality: {data.get('personality', '?')}")
    rules = data.get("rules", [])
    if rules:
        print("  Rules:")
        for r in rules:
            print(f"    - {r}")
    style = data.get("communication_style", {})
    if style:
        print(f"  Style:       tone={style.get('tone', '?')}, verbosity={style.get('verbosity', '?')}")


async def repl():
    """Interactive REPL that chats with the Brain.

    Lifecycle contract: the REPL NEVER calls ``sys.exit``. When the brain
    process is colocated (``feral start``) it lives in a non-daemon
    thread sibling of this coroutine; an exit here used to bring the
    interpreter down with it (issue: clicking a button in the browser
    appeared to "kill the system" because the brain thread was actually
    being shut down by Python interpreter teardown that started when
    ``repl`` raised ``SystemExit``). The REPL now ``return``s on any
    terminal error so the caller (``cmd_start``) can keep the brain
    running.

    Connection contract: uses ``async with websockets.connect(uri) as ws:``
    which is the documented form for ``websockets>=11`` (we require
    ``>=13``). The previous pattern — ``ws = await websockets.connect(uri)``
    followed by ``async with ws as conn:`` — raises ``TypeError`` on
    every modern websockets release because the awaited result is a
    ``WebSocketClientProtocol`` and not an async context manager.
    """
    print(BANNER)
    uri = WS_URL
    ws_pkg = _require_websockets()

    backoff = 1.0
    max_backoff = 30.0

    while True:
        try:
            async with ws_pkg.connect(uri) as ws:
                # Reset backoff once we're actually connected.
                backoff = 1.0
                try:
                    greeting = await asyncio.wait_for(ws.recv(), timeout=5)
                    msg = json.loads(greeting)
                    if msg.get("payload", {}).get("text"):
                        print(f"  FERAL: {msg['payload']['text']}\n")
                except (asyncio.TimeoutError, json.JSONDecodeError):
                    # Brain didn't send a greeting — that's fine, just
                    # drop into the prompt without one.
                    pass

                if not await _repl_session(ws):
                    return
                # Inner session ended due to disconnect — fall through
                # to outer loop which will reconnect.
                print("  Connection lost — reconnecting...")

        except (ConnectionRefusedError, OSError) as exc:
            print(
                f"  Brain unreachable at {uri} ({exc.__class__.__name__}) "
                f"— retrying in {backoff:.0f}s. Press Ctrl+C to give up."
            )
            try:
                await asyncio.sleep(backoff)
            except (asyncio.CancelledError, KeyboardInterrupt):
                print("\n  Goodbye!")
                return
            backoff = min(backoff * 2, max_backoff)
            continue
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n  Goodbye!")
            return
        except Exception as exc:
            # Catch-all for unexpected errors — including the
            # historical websockets-API mismatch. Print a friendly
            # message and return cleanly so the brain stays alive.
            print(f"  REPL error ({exc.__class__.__name__}): {exc}")
            print("  Brain is still running. Reconnect with `feral` (no args).")
            return


async def _repl_session(ws) -> bool:
    """Run one connected REPL session against ``ws``.

    Returns ``True`` if the session ended due to disconnect (caller
    should reconnect), ``False`` if the user asked to quit (caller
    should exit cleanly).
    """
    while True:
        try:
            user_input = await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("you > ")
            )
        except (EOFError, KeyboardInterrupt):
            print("\n  Goodbye!")
            return False

        text = user_input.strip()
        if not text:
            continue

        if text.startswith("/"):
            cmd = text.lower().split()[0]
            if cmd in ("/quit", "/exit", "/q"):
                print("  Goodbye!")
                return False
            elif cmd == "/status":
                cmd_status()
            elif cmd == "/devices":
                cmd_devices()
            elif cmd == "/skills":
                cmd_skills()
            elif cmd == "/identity":
                cmd_identity()
            else:
                print(f"  Unknown command: {cmd}")
            continue

        try:
            await ws.send(json.dumps({
                "type": "text_command",
                "payload": {"text": text},
            }))
        except Exception:
            return True

        full_response = ""
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                if full_response:
                    break
                print("  (timeout waiting for response)")
                break
            except Exception:
                return True

            msg = json.loads(raw)
            mtype = msg.get("type", "")

            if mtype == "stream_delta":
                delta = msg.get("payload", {}).get("delta", "")
                print(delta, end="", flush=True)
                full_response += delta
            elif mtype == "stream_end":
                if full_response:
                    print()
                break
            elif mtype == "text_response":
                text_resp = msg.get("payload", {}).get("text", "")
                if text_resp:
                    print(f"  FERAL: {text_resp}")
                break
            elif mtype == "sdui":
                print(f"  [UI Component: {msg.get('payload', {}).get('component', '?')}]")
                break
            elif mtype == "error":
                print(f"  Error: {msg.get('payload', {}).get('message', '?')}")
                break

        print()


async def one_shot(text: str):
    """Send a single command and print the response.

    Uses ``async with websockets.connect(uri) as ws:`` for compatibility
    with ``websockets>=11`` (we require ``>=13``). Unlike ``repl``, a
    one-shot call has no colocated brain to protect — exit codes are the
    contract for shell scripting, so we keep ``sys.exit(1)`` here.
    """
    ws_pkg = _require_websockets()
    try:
        last_err: Exception | None = None
        for _attempt in range(3):
            try:
                async with ws_pkg.connect(WS_URL) as ws:
                    _ = await asyncio.wait_for(ws.recv(), timeout=5)

                    await ws.send(json.dumps({
                        "type": "text_command",
                        "payload": {"text": text},
                    }))

                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            break

                        msg = json.loads(raw)
                        mtype = msg.get("type", "")

                        if mtype == "stream_delta":
                            print(msg.get("payload", {}).get("delta", ""), end="", flush=True)
                        elif mtype == "stream_end":
                            print()
                            break
                        elif mtype == "text_response":
                            print(msg.get("payload", {}).get("text", ""))
                            break
                        elif mtype == "error":
                            print(f"Error: {msg.get('payload', {}).get('message', '?')}", file=sys.stderr)
                            break
                return
            except (ConnectionRefusedError, OSError) as exc:
                last_err = exc
                if _attempt < 2:
                    await asyncio.sleep(2 ** _attempt)
        if last_err is not None:
            raise last_err

    except ConnectionRefusedError:
        print(f"Cannot connect to FERAL Brain at {WS_URL}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"Cannot connect to FERAL Brain at {WS_URL}: {exc}", file=sys.stderr)
        sys.exit(1)


def _ensure_tls_certs():
    """Generate self-signed TLS certificate if none exists."""
    from config.runtime import brain_tls_cert, brain_tls_key
    cert_path = Path(brain_tls_cert())
    key_path = Path(brain_tls_key())

    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)

    cert_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime
        import ipaddress

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "FERAL Brain"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FERAL"),
        ])

        import socket
        hostname = socket.gethostname()

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.DNSName(hostname),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        key_path.write_bytes(
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
        )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        print(f"Generated self-signed TLS certificate at {cert_path}")
        return str(cert_path), str(key_path)
    except ImportError:
        print("Install 'cryptography' package for auto-generated TLS certs")
        return None, None


def _brain_ssl_kwargs(*, tls: bool) -> dict:
    """Build uvicorn TLS kwargs when CLI ``--tls`` or settings enable TLS."""
    ssl_kwargs: dict = {}
    if tls or brain_tls_enabled():
        cert, key = _ensure_tls_certs()
        if cert and key:
            ssl_kwargs["ssl_certfile"] = cert
            ssl_kwargs["ssl_keyfile"] = key
        else:
            print("TLS requested but no certificates available")
            return {}
    return ssl_kwargs


def _settings_for_doctor() -> dict:
    """Read settings.json directly. Doctor must not need a live brain."""
    import json

    from config.loader import feral_home

    try:
        return json.loads((feral_home() / "settings.json").read_text())
    except Exception:
        return {}

def _print_pairing_line(bound: str, *, console=None) -> None:
    """State whether a phone can pair, right where the operator is looking.

    The ready panel printed ``localhost`` unconditionally, which is the
    one address a phone can never use. Worse, it printed it identically
    whether pairing was possible or structurally impossible. Ask the
    resolver and print its answer, including its refusal text, which is
    already written for a human.
    """
    from cli.ui_kit import banner_line as _banner

    try:
        from api.routes.devices import PairUnavailable, _resolve_pair_origin
        from config.runtime import bound_host

        # The resolver compares intent against the live listener, so it
        # needs to know what we actually bound. _spawn_brain_server
        # records it; this is a no-op restatement for the in-process case
        # and a correction if anything reassigned it.
        if bound_host() is None:
            from config.runtime import record_bound_host

            record_bound_host(bound)

        try:
            origin = _resolve_pair_origin()
            _banner(f"Pair a phone: `feral pair` — QR points at {origin}", console=console)
        except PairUnavailable as exc:
            _banner(f"Phone pairing unavailable: {exc}", style="yellow", console=console)
    except Exception as exc:  # pragma: no cover - never block startup
        import logging as _logging

        _logging.getLogger("feral.cli").debug("pairing line skipped: %s", exc)


def _spawn_brain_server(
    host: str,
    port: int,
    ssl_kwargs: dict,
) -> tuple[threading.Thread, threading.Event, dict]:
    """Start uvicorn in a non-daemon thread (shared by ``serve`` + ``start``)."""
    server_ready = threading.Event()
    server_holder: dict = {"server": None, "exc": None}

    # Record what we are *actually* binding, so `restart_required` and
    # the pair-URL resolver can compare reality against configuration
    # instead of trusting configuration twice.
    record_bound_host(host)

    def _run_server():
        import uvicorn as _uvicorn
        try:
            config = _uvicorn.Config(
                "api.server:app",
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
                # Explicit, not inherited. uvicorn 0.30.6 defaults
                # proxy_headers=True with forwarded_allow_ips="127.0.0.1",
                # and that default is the only reason Tailscale Funnel
                # traffic does not hit the loopback auth bypass in
                # api/server.py. A uvicorn bump flipping it would open
                # the entire API. Pin it here so that cannot happen
                # silently.
                proxy_headers=True,
                forwarded_allow_ips="127.0.0.1",
                **ssl_kwargs,
            )
            server = _uvicorn.Server(config)
            server_holder["server"] = server
            server_ready.set()
            server.run()
        except Exception as exc:
            server_holder["exc"] = exc
            server_ready.set()

    server_thread = threading.Thread(
        target=_run_server, daemon=False, name="feral-brain",
    )
    server_thread.start()
    return server_thread, server_ready, server_holder


def _brain_server_failure_reason(
    server_thread: threading.Thread | None,
    server_holder: dict | None,
) -> str | None:
    """Return a human-readable failure if the uvicorn thread died during boot."""
    if not server_holder:
        return None
    exc = server_holder.get("exc")
    if exc is not None:
        return str(exc)
    if server_thread is not None and not server_thread.is_alive():
        return "brain process exited before /health became ready"
    return None


def _wait_for_brain_health(
    port: int,
    ssl_kwargs: dict,
    *,
    console,
    timeout_s: int | None = None,
    server_thread: threading.Thread | None = None,
    server_holder: dict | None = None,
) -> bool:
    """Poll ``/health`` until the brain finishes ``state.init()``."""
    import time

    _scheme = "https" if ssl_kwargs else "http"
    health_url = os.getenv(
        "FERAL_HEALTH_URL", f"{_scheme}://127.0.0.1:{port}/health",
    )
    boot_report_url = f"{_scheme}://127.0.0.1:{port}/api/boot-report"
    # ``timeout_s`` is a *soft* deadline: how long a typical boot takes.
    # A cold first run (model + embedding warmup) can exceed it, and
    # because ``state.init()`` runs inside uvicorn's blocking startup
    # event the server serves *no* routes — not even /health — until init
    # finishes. Hard-failing at the soft deadline used to kill a brain
    # that was seconds from ready, so the operator retried and stacked
    # multiple brains on the same port. Instead we keep waiting as long as
    # the uvicorn thread is still alive (init still running, not crashed),
    # up to a generous hard ceiling that only bounds a genuine hang.
    timeout_s = int(timeout_s or os.getenv("FERAL_BOOT_TIMEOUT", "180"))
    hard_cap_s = max(timeout_s, int(os.getenv("FERAL_BOOT_HARD_CAP", "600")))

    def _server_died() -> bool:
        return _brain_server_failure_reason(server_thread, server_holder) is not None

    def _health_ok() -> bool:
        try:
            if httpx:
                return httpx.get(health_url, timeout=2, verify=False).status_code == 200
            import urllib.request
            urllib.request.urlopen(health_url, timeout=2)
            return True
        except Exception:
            return False

    def _current_subsystem() -> str | None:
        try:
            if httpx:
                rr = httpx.get(boot_report_url, timeout=1.5, verify=False)
                if rr.status_code == 200:
                    body = rr.json() or {}
                    return body.get("current") or (body.get("last") or {}).get("name")
        except Exception:
            return None
        return None

    def _slow_note(elapsed: int) -> str:
        # Once past the soft deadline, reassure the operator that a live
        # (still-booting) brain is being waited on, not hung.
        if elapsed >= timeout_s:
            return "still warming up (first run can take a few minutes)..."
        return "warming up..."

    try:
        from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
        _RICH_PROGRESS = True
    except Exception:
        _RICH_PROGRESS = False

    if _RICH_PROGRESS:
        with Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[bold]Starting brain[/bold] · {task.description}"),
            TimeElapsedColumn(),
            transient=False,
            console=console,
        ) as progress:
            task = progress.add_task("warming up...", start=True)
            last_desc: str | None = None
            for _i in range(hard_cap_s):
                if _server_died():
                    return False
                time.sleep(1)
                if _server_died():
                    return False
                if _health_ok():
                    return True
                subsystem = _current_subsystem()
                desc = f"{subsystem}..." if subsystem else _slow_note(_i + 1)
                if desc != last_desc:
                    progress.update(task, description=desc)
                    last_desc = desc
        return False

    sys.stdout.write("  Starting brain...")
    sys.stdout.flush()
    last_subsystem = None
    for i in range(hard_cap_s):
        if _server_died():
            sys.stdout.write("\n")
            return False
        time.sleep(1)
        if _server_died():
            sys.stdout.write("\n")
            return False
        if _health_ok():
            sys.stdout.write("\n")
            return True
        subsystem = _current_subsystem()
        if subsystem and subsystem != last_subsystem:
            sys.stdout.write(f"\n    [{i + 1}s] {subsystem}...")
            last_subsystem = subsystem
        elif (i + 1) == timeout_s:
            sys.stdout.write(
                f"\n    [{i + 1}s] still warming up (first run can take "
                "a few minutes)..."
            )
        else:
            sys.stdout.write(".")
        sys.stdout.flush()
    sys.stdout.write("\n")
    return False


def _stop_brain_server(server_thread: threading.Thread, server_holder: dict, *, join_timeout: float = 15) -> None:
    srv = server_holder.get("server")
    if srv is not None:
        srv.should_exit = True
    server_thread.join(timeout=join_timeout)


def cmd_serve(host: str | None = None, port: int | None = None, tls: bool = False):
    """Start the FERAL Brain server."""
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print("uvicorn not installed. Run: pip install 'feral-ai[all]'")
        sys.exit(1)

    core_root = str(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if core_root not in sys.path:
        sys.path.insert(0, core_root)

    hydrate_brain_runtime_env()

    host = host or brain_bind_host()
    port = int(port or brain_port())

    ssl_kwargs = _brain_ssl_kwargs(tls=tls)
    if (tls or brain_tls_enabled()) and not ssl_kwargs:
        return

    from cli.ui_kit import (
        get_console as _get_console,
        print_start_banner as _print_start_banner,
        print_ready_panel as _print_ready_panel,
        banner_line as _banner_line,
    )

    _console = _get_console()
    _print_start_banner(
        port=port,
        tls=bool(ssl_kwargs),
        bind_host=host,
        console=_console,
    )

    server_thread, server_ready, server_holder = _spawn_brain_server(host, port, ssl_kwargs)
    server_ready.wait(timeout=30)
    if server_holder.get("exc"):
        _banner_line(f"Brain failed to start: {server_holder['exc']}", style="red", console=_console)
        sys.exit(1)

    if not _wait_for_brain_health(
        port, ssl_kwargs, console=_console,
        server_thread=server_thread, server_holder=server_holder,
    ):
        boot_err = _brain_server_failure_reason(server_thread, server_holder)
        if boot_err:
            _banner_line(f"Brain failed to start: {boot_err}", style="red", console=_console)
        else:
            _banner_line(
                f"Failed to start after {os.getenv('FERAL_BOOT_HARD_CAP', '600')}s. Check logs or run: feral doctor",
                style="red",
                console=_console,
            )
            _banner_line(
                "Tip: FERAL_BOOT_HARD_CAP=1200 feral serve   # for very slow first runs",
                style="dim",
                console=_console,
            )
        _stop_brain_server(server_thread, server_holder, join_timeout=5)
        sys.exit(1)

    data = _http_get("/api/dashboard")
    _print_ready_panel(
        port=port,
        llm_ok=bool(data.get("llm_available")),
        skills_count=data.get("skills_count", "?"),
        memory_notes=(data.get("memory") or {}).get("notes", 0),
        public_url=os.getenv("FERAL_PUBLIC_BASE_URL"),
        tls=bool(ssl_kwargs),
        console=_console,
    )

    scheme = "https" if ssl_kwargs else "http"
    public_base = os.getenv("FERAL_PUBLIC_BASE_URL", f"{scheme}://localhost:{port}")
    _banner_line(f"Dashboard: {public_base}", console=_console)
    _banner_line(f"API docs:  {public_base}/docs", style="dim", console=_console)
    _print_pairing_line(host, console=_console)

    def _on_sigterm(_signum, _frame):  # pragma: no cover
        _stop_brain_server(server_thread, server_holder)

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass

    try:
        server_thread.join()
    except KeyboardInterrupt:
        _banner_line("Shutting down brain...", console=_console)
        _stop_brain_server(server_thread, server_holder)
        _banner_line("Goodbye!", console=_console)


def _is_first_run() -> bool:
    """Check if this is the first time running FERAL.

    The canonical source is ``settings.json.meta.setup_complete`` — it
    gets set to ``True`` by the setup wizard + the REST ``POST
    /api/llm/config`` route on success. Fall back to the historical
    heuristics (env API key / non-empty credentials.json / Ollama
    provider in settings) so existing installs upgraded from older
    versions don't get a surprise wizard.

    Local-only setups (Ollama, LMStudio) used to re-run the wizard on
    every boot because ``credentials.json`` was empty — this branch
    handles that case explicitly via the provider lookup.
    """
    home_path = feral_home()
    settings_path = home_path / "settings.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
            meta = settings.get("meta") or {}
            if meta.get("setup_complete"):
                return False
            llm = settings.get("llm") or {}
            provider = (llm.get("provider") or "").strip().lower()
            if provider in ("ollama", "lmstudio", "local") and llm.get("model"):
                return False
        except Exception:
            pass

    if os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") \
       or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GROQ_API_KEY"):
        return False

    creds_path = home_path / "credentials.json"
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text())
            if any(v for v in creds.values() if v):
                return False
        except Exception:
            pass

    return True


def cmd_start(
    port: int | None = None,
    no_browser: bool = False,
    tls: bool = False,
    foreground: bool = False,
):
    """One command to rule them all.

    Default behaviour on macOS / Linux: install + start the brain as a
    user-level service (``com.feral.brain`` launchd LaunchAgent on
    macOS, ``feral-brain.service`` user systemd unit on Linux) and
    return immediately. ``feral stop`` / ``feral status`` / ``feral
    logs`` / ``feral restart`` manage the running service.

    ``foreground=True`` (``feral start --foreground``) keeps the
    original interactive behaviour: the brain runs in a non-daemon
    thread of this process, the operator gets the REPL, and Ctrl+C
    shuts everything down. The launchd plist's ``ProgramArguments``
    delegates to this path so the same banner chrome ends up in the
    log file.

    Lifecycle invariant (the bug this docstring is here to prevent from
    ever shipping again): the brain is the long-lived service in this
    process; the REPL is a transient companion. Previously the brain
    ran in a ``daemon=True`` thread, so any ``sys.exit`` from the REPL
    (e.g. ``websockets`` API mismatch, transient WS hiccup) raised
    ``SystemExit``, which started Python interpreter teardown, which
    killed the daemon thread mid-flight. From the user's perspective
    the entire system "died" the next time they clicked a button.

    The fix is two-fold:
      1. The brain thread is ``daemon=False``. Interpreter teardown can
         no longer kill it — only an explicit ``server.should_exit``.
      2. We hold a reference to the ``uvicorn.Server`` in
         ``server_holder`` so SIGINT / SIGTERM / "REPL closed cleanly"
         paths can flip ``should_exit`` and join the thread.

    The only ways to stop the brain are now: explicit Ctrl+C / SIGTERM,
    or ``uvicorn.Server`` itself crashing.
    """
    import time

    try:
        import uvicorn  # noqa: F401  (we only need the dep check here)
    except ImportError:
        print("  Missing dependencies. Run: pip install 'feral-ai[llm]'")
        sys.exit(1)

    # Service-mode dispatch — when not asked for foreground, hand off
    # to launchd / systemd and exit immediately. The launchd plist's
    # ProgramArguments calls back into this function with --foreground
    # so the boot path is exercised end-to-end inside the service.
    from cli import daemon as _daemon
    from cli.ui_kit import (
        get_console as _get_console,
        banner_line as _banner_line_service,
        brand_panel as _brand_panel_service,
    )

    if not foreground and _daemon.is_service_supported():
        # Propagate CLI flags into the env so the LaunchAgent plist
        # captures them — operator's one-shot ``feral start --tls`` then
        # survives reboots until ``feral start`` is rerun.
        if port is not None:
            os.environ["FERAL_PORT"] = str(port)
        if tls:
            os.environ["FERAL_TLS"] = "1"

        _console_svc = _get_console()
        _banner_line_service(
            f"Installing {_daemon.SERVICE_LABEL} as a {('launchd' if sys.platform == 'darwin' else 'systemd --user')} service...",
            console=_console_svc,
        )
        try:
            status = _daemon.start_service()
        except Exception as exc:  # pragma: no cover — surfaces platform issues to the operator
            _banner_line_service(f"Service install failed: {exc}", style="red", console=_console_svc)
            _banner_line_service(
                "Falling back to foreground mode. Use --foreground next time to skip the service path.",
                style="dim",
                console=_console_svc,
            )
            foreground = True
        else:
            stdout_log, stderr_log = _daemon.log_paths()
            body = (
                f"[bold]Label:[/bold]   {_daemon.SERVICE_LABEL}\n"
                f"[bold]PID:[/bold]     {status.get('pid') or '—'}\n"
                f"[bold]State:[/bold]   {status.get('state') or 'starting'}\n"
                f"[bold]Stdout:[/bold]  {stdout_log}\n"
                f"[bold]Stderr:[/bold]  {stderr_log}"
            )
            _brand_panel_service("Brain service started", body, console=_console_svc)
            _banner_line_service(
                "Use `feral status` to check health, `feral logs` to tail, `feral stop` to stop.",
                style="dim",
                console=_console_svc,
            )
            return

    hydrate_brain_runtime_env()

    port = int(port or brain_port())

    ssl_kwargs = _brain_ssl_kwargs(tls=tls)
    if (tls or brain_tls_enabled()) and not ssl_kwargs:
        return

    # First run detection — auto-launch setup
    if _is_first_run():
        from cli.ui_kit import banner_line as _firstrun_banner
        _firstrun_banner("First time running FERAL? Let's set you up.")
        cmd_setup()

    # Check if already running. In this branch there is no local server
    # for us to manage, so a clean REPL exit is enough.
    try:
        scheme = "https" if ssl_kwargs else "http"
        health_url = os.getenv("FERAL_HEALTH_URL", f"{scheme}://127.0.0.1:{port}/health")
        if httpx:
            r = httpx.get(health_url, timeout=2, verify=False)
            if r.status_code == 200:
                from cli.ui_kit import banner_line as _alreadyup_banner
                _alreadyup_banner(f"FERAL is already running on {scheme}://127.0.0.1:{port}")
                if not no_browser:
                    _open_browser(port)
                try:
                    asyncio.run(repl())
                except KeyboardInterrupt:
                    pass
                return
    except Exception:
        pass

    from cli.ui_kit import (
        get_console as _get_console,
        print_start_banner as _print_start_banner,
        print_ready_panel as _print_ready_panel,
        banner_line as _banner_line,
    )

    _console = _get_console()
    _print_start_banner(
        port=port,
        tls=bool(ssl_kwargs),
        bind_host=brain_bind_host(),
        console=_console,
    )

    server_thread, server_ready, server_holder = _spawn_brain_server(
        brain_bind_host(), port, ssl_kwargs,
    )
    server_ready.wait(timeout=30)
    if server_holder.get("exc"):
        _banner_line(
            f"Brain failed to start: {server_holder['exc']}",
            style="red",
            console=_console,
        )
        sys.exit(1)

    healthy = _wait_for_brain_health(
        port, ssl_kwargs, console=_console,
        server_thread=server_thread, server_holder=server_holder,
    )

    if not healthy:
        boot_err = _brain_server_failure_reason(server_thread, server_holder)
        if boot_err:
            _banner_line(f"Brain failed to start: {boot_err}", style="red", console=_console)
        else:
            _banner_line(
                f"Failed to start after {os.getenv('FERAL_BOOT_HARD_CAP', '600')}s. Check logs or run: feral doctor",
                style="red",
                console=_console,
            )
            _banner_line(
                "Tip: FERAL_BOOT_HARD_CAP=1200 feral start   # for very slow first runs",
                style="dim",
                console=_console,
            )
        # Try to stop the brain we spawned, then exit with non-zero so
        # the user sees the failure.
        _stop_brain_server(server_thread, server_holder, join_timeout=5)
        sys.exit(1)

    # Render the post-boot panel using the same chrome as the wizard's
    # finish screen.
    data = _http_get("/api/dashboard")
    _print_ready_panel(
        port=port,
        llm_ok=bool(data.get("llm_available")),
        skills_count=data.get("skills_count", "?"),
        memory_notes=(data.get("memory") or {}).get("notes", 0),
        public_url=os.getenv("FERAL_PUBLIC_BASE_URL"),
        tls=bool(ssl_kwargs),
        console=_console,
    )
    _print_pairing_line(brain_bind_host(), console=_console)

    if not no_browser:
        _open_browser(port)

    print()

    # Install a SIGTERM handler so ``kill <pid>`` shuts the brain down
    # cleanly. SIGINT (Ctrl+C) is already handled by Python's default
    # KeyboardInterrupt mechanism inside ``asyncio.run(repl())`` and the
    # join loop below.
    shutdown_requested = threading.Event()

    def _on_sigterm(signum, frame):  # pragma: no cover — exercised in real signal flow
        shutdown_requested.set()
        srv = server_holder.get("server")
        if srv is not None:
            srv.should_exit = True

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        # signal.signal() requires the main thread; some embedded
        # environments don't allow it. SIGINT still works via the
        # default Python handler.
        pass

    # Drop into interactive REPL. The brain stays alive even if the
    # REPL crashes, exits cleanly, or returns early. SystemExit is
    # caught defensively in case some code path inside repl() ever
    # reaches for sys.exit again.
    try:
        asyncio.run(repl())
    except KeyboardInterrupt:
        shutdown_requested.set()
    except SystemExit:
        # Defensive: prior versions of repl() called sys.exit on
        # connection errors and took the daemon brain down with them.
        # Modern repl() never raises SystemExit — this is belt + braces
        # against future regressions.
        pass
    except Exception as exc:
        print(f"\n  REPL crashed unexpectedly: {exc}")
        print("  Brain is still running.")

    if not shutdown_requested.is_set():
        _shutdown_scheme = "https" if ssl_kwargs else "http"
        _banner_line(
            f"REPL closed. Brain still running on {_shutdown_scheme}://localhost:{port}",
            console=_console,
        )
        _banner_line("Press Ctrl+C to stop the brain.", style="dim", console=_console)
        try:
            while server_thread.is_alive() and not shutdown_requested.is_set():
                server_thread.join(timeout=1.0)
        except KeyboardInterrupt:
            shutdown_requested.set()

    # Tell uvicorn to stop and wait for it to drain.
    _banner_line("Shutting down brain...", console=_console)
    srv = server_holder.get("server")
    if srv is not None:
        srv.should_exit = True
    server_thread.join(timeout=15)
    _banner_line("Goodbye!", console=_console)


def _open_browser(port: int):
    """Open the FERAL dashboard in the default browser."""
    try:
        import webbrowser
        url = os.getenv("FERAL_PUBLIC_BASE_URL", f"http://localhost:{port}")
        webbrowser.open(url)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# Doctor — probe-driven helpers (Lane 07 )
# ─────────────────────────────────────────────────────────────────────


def _voice_catalogue_or_empty() -> list:
    """Lazy-load + degrade: if Lane 05's voice catalogue is missing
    (e.g. partial install), doctor returns an empty list rather
    than crashing the whole probe sweep."""
    try:
        from security.probe import voice_provider_catalogue
        return voice_provider_catalogue()
    except Exception:
        return []


def _run_doctor_probes(console) -> dict:
    """Run every registered probe in parallel and return id→ProbeResult.

    Doctor MUST not block on a slow provider for >~5s; ``probe()``
    itself enforces ``PROBE_TIMEOUT_SECONDS``. We use ``asyncio.gather``
    so the whole sweep completes in roughly the slowest probe's time
    (typically <1s when keys are present, ~5s when they time out).
    """
    try:
        from security.probe import probe, registered_probe_ids
    except Exception as exc:
        console.print(f"[red]✘[/red]  Probe registry unavailable: {exc}")
        return {}

    ids = registered_probe_ids()
    if not ids:
        return {}

    async def _gather():
        tasks = [probe(pid) for pid in ids]
        return await asyncio.gather(*tasks, return_exceptions=True)

    try:
        raw = asyncio.run(_gather())
    except RuntimeError:
        # Already inside an event loop — fall back to sequential.
        loop = asyncio.new_event_loop()
        try:
            raw = loop.run_until_complete(_gather())
        finally:
            loop.close()

    out = {}
    for pid, res in zip(ids, raw):
        if isinstance(res, Exception):
            # Synthesize a degraded ProbeResult so the renderer
            # still has a uniform shape to work with.
            from security.probe import ProbeResult
            import time as _t
            out[pid] = ProbeResult(
                provider=pid, ok=False, status_code=None,
                reason="probe_exception", detail=str(res),
                probed_at=_t.time(), latency_ms=0.0,
            )
        else:
            out[pid] = res
    return out


def _render_probe_row(pid: str, result, _pass, _info, _warn, _fail):
    """Render one probe row with consistent green/yellow/red semantics."""
    label_map = {
        "openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Gemini",
        "openrouter": "OpenRouter", "deepseek": "DeepSeek", "groq": "Groq",
        "ollama": "Ollama (local)", "lmstudio": "LM Studio (local)",
        "bedrock": "AWS Bedrock",
        "google": "Google (Calendar / Gmail / Drive / Contacts)",
        "notion": "Notion", "spotify": "Spotify",
        "whoop": "Whoop", "oura": "Oura",
        "microsoft": "Microsoft 365",
        "home_assistant": "Home Assistant",
        "telegram": "Telegram", "slack": "Slack", "discord": "Discord",
        "openai_realtime": "OpenAI Realtime",
        "gemini_live": "Gemini Live",
        "deepgram": "Deepgram (STT)",
        "elevenlabs": "ElevenLabs (TTS)",
        "cartesia": "Cartesia (TTS)",
        "openai_whisper": "OpenAI Whisper (STT)",
        "groq_whisper": "Groq Whisper (STT)",
        "openai_tts": "OpenAI TTS",
    }
    label = label_map.get(pid, pid)
    detail = f"{result.detail} ({result.latency_ms:.0f}ms)"
    if result.ok:
        _pass(label, detail)
        return
    reason = (result.reason or "").lower()
    # "missing_credential" / "no_credentials_loaded" / "missing_api_key"
    # / "no_key" / "no_token" are all "not yet configured" — render as
    # info (cyan ℹ), not warning. The probe registry uses any of these
    # values; we match permissively rather than coupling the renderer
    # to the registry's exact spelling.
    if any(
        s in reason
        for s in (
            "missing", "no_credential", "no_key", "no_token",
            "unconfigured", "not_set", "not_configured",
        )
    ):
        _info(label, "not configured")
        return
    if reason in ("auth_failed",) or result.status_code in (401, 403):
        _fail(
            label,
            f"key rejected by API: {result.detail}",
            f"Run: feral key add --provider {pid} --label default  (or set the env var and re-probe)",
        )
        return
    # Everything else (timeout, transient 5xx, unknown_provider): yellow.
    _warn(label, f"{reason or 'error'}: {result.detail}")


def _ollama_pulled_models_sync(base_url: str, timeout: float = 3.0) -> list[str] | None:
    """Return the list of model names pulled on the local Ollama server.

    Returns ``None`` when Ollama is unreachable so callers can
    distinguish "definitely not pulled" from "couldn't check" — the
    former is a real configuration error (doctor fails), the latter
    is informational (doctor warns once, doesn't block).

    Uses ``urllib`` instead of httpx so the helper has no async / event
    loop coupling — doctor runs sync and we want the lookup to behave
    the same in tests that don't spin up an event loop.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"{base_url.rstrip('/')}/api/tags", timeout=timeout,
        ) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:
        return None
    models = payload.get("models") or []
    out: list[str] = []
    for entry in models:
        name = (entry.get("name") or "").strip()
        if name:
            out.append(name)
    return out


def _ollama_pulled_aliases(pulled: list[str]) -> set[str]:
    """Expand pulled model names into the set of strings that should
    match a configured model id. Ollama tags look like ``llama3.1:8b``;
    operators routinely configure the model as just ``llama3.1``. Both
    must be considered "pulled"."""
    out: set[str] = set()
    for name in pulled:
        out.add(name)
        if ":" in name:
            out.add(name.split(":", 1)[0])
    return out


def _check_configured_ollama_model(_pass, _info, _warn, _fail) -> None:
    """Verify the currently-configured Ollama model is actually pulled.

    Reads ``llm.provider`` + ``llm.model`` from the merged settings
    snapshot. Only runs the check when the configured provider is
    ``ollama`` — for cloud providers the LLM probe row above already
    covers credential validity. When Ollama is unreachable the local
    server probe will already have flagged it, so this helper stays
    quiet to avoid two warnings for the same root cause.
    """
    try:
        from config.loader import load_settings
        from config.runtime import ollama_base_url
    except Exception:
        return
    try:
        settings = load_settings() or {}
    except Exception:
        return
    llm = settings.get("llm") or {}
    provider = str(llm.get("provider") or "").strip().lower()
    model = str(llm.get("model") or "").strip()
    if provider != "ollama" or not model:
        return
    pulled = _ollama_pulled_models_sync(ollama_base_url())
    if pulled is None:
        # Server unreachable — the LLM probe row above is the right
        # place to surface that, so we stay quiet here.
        return
    aliases = _ollama_pulled_aliases(pulled)
    if not pulled:
        _fail(
            f"Ollama model {model!r}",
            "no models pulled on the local Ollama server",
            f"Run: ollama pull {model}  (or pick an installed model in Settings → Providers → Ollama)",
        )
        return
    base_name = model.split(":", 1)[0]
    if model in aliases or base_name in aliases:
        _pass(f"Ollama model {model!r}", "pulled and ready")
        return
    installed = ", ".join(sorted({n.split(":", 1)[0] for n in pulled})[:6]) or "(none)"
    _fail(
        f"Ollama model {model!r}",
        f"configured but not pulled — installed: {installed}",
        f"Run: ollama pull {model}  (or pick an installed model in Settings → Providers → Ollama)",
    )


def cmd_doctor():
    """Run comprehensive diagnostics and report what's working."""
    try:
        from rich.console import Console
        from rich.panel import Panel
    except ImportError:
        print("rich is required for the doctor command: pip install rich")
        sys.exit(1)

    console = Console()
    passed = 0
    infos = 0
    warnings = 0
    failures = 0
    fixes: list[str] = []

    # v2026.5.36 — four severity tiers, not three. The pre-v2026.5.36
    # doctor only had pass / warn / fail, which forced every probe to
    # pick between "everything is great" and "there is a problem".
    # That made a fresh install look broken: a clean Mac with FERAL
    # newly installed produced ~5 yellow warnings (memory DB not
    # created yet, Chrome CDP not running, Local STT not installed,
    # Voice key not set, …) for things that are *expected* to be
    # absent on first boot. The new ``_info`` tier is reserved for
    # "not configured yet" / "opt-in feature you haven't enabled" —
    # zero remediation expected, never counts toward warnings, and
    # never adds noise to the Suggested-fixes list.
    #
    # The summary panel renders all four counts (passes, infos,
    # warnings, failures) so the operator can still tell at a glance
    # whether anything in the install is actually broken.
    # Everything interpolated into a Rich markup string has to be escaped
    # first, or Rich parses the operator's own text as style tags.
    #
    # This was not theoretical. Every extras hint in this command came out
    # wrong: "pip install 'feral-ai[embeddings]'" rendered as
    # "pip install 'feral-ai'", because Rich consumed [embeddings] as a
    # tag. Same for [browser], [stt], [tts], [memory-chroma],
    # [memory-qdrant] and [macos-extras]. The doctor was confidently
    # printing install commands that install the wrong thing.
    #
    # Escaping here rather than at each call site, so a future probe
    # cannot reintroduce it by writing a bracket in a detail string.
    from rich.markup import escape as _esc

    def _pass(label: str, detail: str = ""):
        nonlocal passed
        passed += 1
        msg = f"[green]✔[/green]  {_esc(label)}"
        if detail:
            msg += f"  [dim]{_esc(detail)}[/dim]"
        console.print(msg)

    def _info(label: str, detail: str = ""):
        """Optional / not-yet-configured probe.

        Used for features that are explicitly opt-in (local STT/TTS,
        voice realtime key, workspace grants) or that auto-initialise
        on first use (memory DB, Chrome CDP auto-launch). No fix is
        offered because nothing is broken — the operator simply has
        not turned this thing on yet.
        """
        nonlocal infos
        infos += 1
        msg = f"[cyan]ℹ[/cyan]  {_esc(label)}"
        if detail:
            msg += f"  [dim]{_esc(detail)}[/dim]"
        console.print(msg)

    def _warn(label: str, detail: str = "", fix: str = ""):
        nonlocal warnings
        warnings += 1
        msg = f"[yellow]⚠[/yellow]  {_esc(label)}"
        if detail:
            msg += f"  [dim]{_esc(detail)}[/dim]"
        console.print(msg)
        if fix:
            fixes.append(fix)

    def _fail(label: str, detail: str = "", fix: str = ""):
        nonlocal failures
        failures += 1
        msg = f"[red]✘[/red]  {_esc(label)}"
        if detail:
            msg += f"  [dim]{_esc(detail)}[/dim]"
        console.print(msg)
        if fix:
            fixes.append(fix)

    console.print(Panel(
        "[bold]🦝  FERAL Doctor[/bold] — installation health check",
        border_style="cyan",
    ))
    console.print()

    # ── 1. Python version ──
    py_ver = sys.version_info
    ver_str = f"{py_ver.major}.{py_ver.minor}.{py_ver.micro}"
    if (py_ver.major, py_ver.minor) >= (3, 11):
        _pass("Python version", ver_str)
    else:
        _fail("Python version", f"{ver_str} (need >= 3.11)", "Install Python 3.11+: https://python.org")

    # ── 2. FERAL package importable ──
    try:
        pkg_version, pkg_location = _installed_pkg_info()
        if pkg_version != "unknown":
            _pass("FERAL package", f"feral-ai {pkg_version}  ({pkg_location})")
        else:
            _warn("FERAL package", "installed from source (no pip metadata)")
    except Exception as exc:
        _fail("FERAL package", str(exc), "pip install -e '.[all]'")

    # ── 3. Config directory ──
    home = feral_home()
    if home.exists() and home.is_dir():
        _pass("Config directory", str(home))
    else:
        _fail("Config directory", f"{home} does not exist", "Run: feral setup")

    # ── 4. Provider probes (validity, not just presence) ──
    #
    # Pre-Lane-07 this section only checked vault/env *presence* of
    # an LLM key — finding 07's D-D defect ("`feral doctor` reports
    # ✔ LLM credentials when the key is invalid / 401"). The fix
    # routes every provider/integration/voice probe through the
    # Wave 1 ``security.probe.probe()`` registry so doctor reflects
    # actual API round-trip validity. Each probe runs once with a
    # 5s ceiling (``PROBE_TIMEOUT_SECONDS``); first invocation
    # populates the in-process cache so the brain runtime can read
    # the same result without re-paying the network cost.
    probe_results = _run_doctor_probes(console)

    # Categorise probe ids so doctor renders three labelled
    # sections instead of one undifferentiated wall. The catalogue
    # is pinned in ``security.probe`` (LLM ids match
    # ``providers/catalog.py``; voice ids match
    # ``VOICE_PROVIDER_CATALOGUE``; integration ids match
    # ``integrations/oauth_manager.BUILTIN_PROVIDERS`` + the
    # messaging hub).
    LLM_PROBE_IDS = (
        "openai", "anthropic", "gemini", "openrouter", "deepseek",
        "groq", "ollama", "lmstudio", "bedrock",
    )
    INTEGRATION_PROBE_IDS = (
        "google", "notion", "spotify", "whoop", "oura", "microsoft",
        "home_assistant", "telegram", "slack", "discord",
    )
    VOICE_PROBE_IDS = tuple(p["id"] for p in _voice_catalogue_or_empty())

    # Render each section. We reuse ``_render_probe_row`` so green/
    # yellow/red semantics stay consistent across sections:
    #   * ok=True → green ✔ (passed)
    #   * ok=False & reason="missing_credential" → cyan ℹ (info,
    #     opt-in / not-yet-configured); doesn't add to warnings
    #   * ok=False & status_code=401 / 403 → red ✘ (real failure)
    #   * ok=False & other → yellow ⚠ (transient / unknown error)
    console.print()
    console.print("[bold]LLM providers[/bold]")
    any_llm_ok = False
    for pid in LLM_PROBE_IDS:
        result = probe_results.get(pid)
        if result is None:
            continue  # registry is single-source-of-truth; tolerate gaps
        if result.ok:
            any_llm_ok = True
        _render_probe_row(pid, result, _pass, _info, _warn, _fail)
    if not any_llm_ok:
        # Closes finding 07's D-D: at least one LLM provider must
        # actually authenticate, not just be "configured". The fix
        # tag steers the operator to the wizard or `feral key add`.
        _fail(
            "LLM providers",
            "no provider passed probe — chat will not work until at least one is green",
            "Run: feral setup  OR  feral key add --provider <id> --label default",
        )

    console.print()
    console.print("[bold]Integrations[/bold]")
    for pid in INTEGRATION_PROBE_IDS:
        result = probe_results.get(pid)
        if result is None:
            continue
        _render_probe_row(pid, result, _pass, _info, _warn, _fail)

    console.print()
    console.print("[bold]Voice providers[/bold]")
    for pid in VOICE_PROBE_IDS:
        result = probe_results.get(pid)
        if result is None:
            continue
        _render_probe_row(pid, result, _pass, _info, _warn, _fail)

    # ── 4b. Local model availability (Ollama) ──
    #
    # The Ollama LLM probe above only checks that the local server is
    # reachable (HTTP 200 on /api/tags). It does NOT check that the
    # *currently configured* chat model actually exists in the pulled
    # set. Reachable + missing-model produced the operator report
    # "Switched LLM to ollama/<name> (available=True)" immediately
    # followed by a 404 on /v1/chat/completions, with doctor reporting
    # everything as green.
    #
    # We do that lookup here, after the generic LLM probe rows so the
    # context is obvious to the operator, and only when the active
    # provider is Ollama (cloud providers don't need this check).
    _check_configured_ollama_model(_pass, _info, _warn, _fail)

    # ── 5. Identity files — USER.md ──
    user_md = home / "USER.md"
    if user_md.exists():
        content = user_md.read_text().strip()
        if len(content) > 10:
            _pass("Identity (USER.md)", f"{len(content)} chars")
        else:
            _warn("Identity (USER.md)", "file exists but is nearly empty",
                  "Edit ~/.feral/USER.md with info about yourself")
    else:
        _warn("Identity (USER.md)", "not found — agent won't know who you are",
              "Run: feral setup  (creates ~/.feral/USER.md)")

    # ── 6. Memory database ──
    from config.loader import feral_data_home
    mem_db = feral_data_home() / "memory.db"
    if mem_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(mem_db))
            conn.execute("SELECT 1")
            conn.close()
            size_kb = mem_db.stat().st_size // 1024
            _pass("Memory database", f"{mem_db}  ({size_kb} KB)")
        except Exception as exc:
            _fail("Memory database", f"exists but not accessible: {exc}",
                  "Check permissions on ~/.feral/memory.db")
    else:
        # v2026.5.36 — was `_warn`. The brain auto-creates `memory.db`
        # the first time MemoryStore opens, so "not created yet" is
        # the *expected* state immediately after `pip install`. No
        # operator action required.
        _info("Memory database", "not created yet — will be created on first run")

    # ── 6b. Memory vector backend optional deps ──
    try:
        from config.loader import load_settings as _load_settings_for_vec
        _memory_cfg = (_load_settings_for_vec() or {}).get("memory") or {}
        _vec_backend = _memory_cfg.get("backend") or "sqlite_vec"
        if _vec_backend == "sqlite_vec":
            _pass("Memory vector backend", "sqlite_vec (built-in default)")
        elif _vec_backend == "chroma":
            try:
                import chromadb  # noqa: F401
                _pass("Memory vector backend", "chroma — chromadb installed")
            except ImportError:
                _fail(
                    "Memory vector backend",
                    "settings.json has memory.backend=chroma but chromadb is not installed",
                    "Run: pip install 'feral-ai[memory-chroma]'  "
                    "OR set memory.backend to sqlite_vec in ~/.feral/settings.json",
                )
        elif _vec_backend == "qdrant":
            try:
                import qdrant_client  # noqa: F401
                _pass("Memory vector backend", "qdrant — qdrant-client installed")
            except ImportError:
                _fail(
                    "Memory vector backend",
                    "settings.json has memory.backend=qdrant but qdrant-client is not installed",
                    "Run: pip install 'feral-ai[memory-qdrant]'  "
                    "OR set memory.backend to sqlite_vec in ~/.feral/settings.json",
                )
        else:
            _warn(
                "Memory vector backend",
                f"unknown backend id {_vec_backend!r}",
                "Set memory.backend to sqlite_vec, chroma, or qdrant in ~/.feral/settings.json",
            )
    except Exception as exc:
        _warn("Memory vector backend", f"could not verify: {exc}")

    # ── 6b-2. Embedding provider ──
    #
    # Reported because the degraded state is otherwise invisible and
    # silent. "hash" is a deterministic SHA-256 projection: the index
    # keeps working and every query still returns rows, so semantic
    # search looks alive while actually being lexical-only. Nothing in
    # the CLI or the UI said which provider was live, so a user could
    # run for months believing memory search was semantic.
    #
    # This became reachable in normal use when embeddings stopped
    # treating the mere presence of OPENAI_API_KEY as consent to send
    # every note to a paid endpoint. That change is right, and it makes
    # naming the fallback out loud a requirement rather than a nicety.
    try:
        from memory.embeddings import EmbeddingProvider

        _emb = EmbeddingProvider()
        _emb_name = _emb.provider_name
        _emb_mode = getattr(_emb, "provider_mode", "auto")
        if _emb_name in ("fastembed", "sentence_transformers"):
            _pass(
                "Embedding provider",
                f"{_emb_name} ({_emb.dimension}d, local and free)",
            )
        elif _emb_name == "openai":
            _info(
                "Embedding provider",
                "OpenAI text-embedding-3-small (explicitly selected, billed per call)",
            )
        else:
            # _info, not _warn. On a fresh install with no extras this is
            # the designed default rather than a malfunction, and
            # test_doctor_severity is right to insist a clean first boot
            # shows zero warnings. The install hint rides in the detail so
            # the state is still named out loud without registering a
            # suggested fix for something that is not broken.
            _info(
                "Embedding provider",
                "hash fallback, memory search is keyword-only and NOT semantic. "
                "pip install 'feral-ai[embeddings]' for local, free semantic "
                "search (no torch, ~130MB)",
            )
        if _emb_mode == "openai" and _emb_name != "openai":
            _warn(
                "Embedding provider mode",
                "FERAL_EMBED_PROVIDER=openai but no OPENAI_API_KEY is set",
                "Set OPENAI_API_KEY, or unset FERAL_EMBED_PROVIDER to use local embeddings",
            )
    except Exception as exc:
        _warn("Embedding provider", f"could not verify: {exc}")

    # ── 6c. Memory at-rest encryption (v2026.5.43) ──
    #
    # Only renders a row when the operator has explicitly opted in
    # via ``feral memory encrypt`` (i.e. memory.db.enc exists). The
    # plaintext-only happy path stays silent — doctor never scolds an
    # operator for not having enabled an opt-in feature.
    mem_enc_path = mem_db.with_name(mem_db.name + ".enc")
    if mem_enc_path.exists():
        try:
            from security.vault import get_vault as _gv
            _vault_probe = _gv()
            _vault_probe._master_key()  # raises if keychain broken
            _pass("Memory at-rest encryption", f"enabled — {mem_enc_path}")
        except Exception as exc:
            _fail(
                "Memory at-rest encryption",
                f"{mem_enc_path} exists but vault cannot be unlocked: {exc}",
                "Restore keychain entry or run `feral key recover` before "
                "starting the brain",
            )

    # ── 7. Port availability ──
    import socket
    port = int(brain_port())
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(1)
        sock.connect(("127.0.0.1", port))
        sock.close()
        health = _http_get("/health")
        if "error" not in health:
            _pass("Port availability", f":{port} — FERAL brain already running")
        else:
            _warn("Port availability", f":{port} in use by another process",
                  f"Kill the process on port {port} or set FERAL_PORT to another value")
    except (ConnectionRefusedError, OSError):
        _pass("Port availability", f":{port} is free")
    finally:
        sock.close()

    # ── 7a. LLM endpoint coherence ──
    #
    # A real install ran provider=openrouter with
    # base_url=https://api.anthropic.com/v1 and logged 610 consecutive
    # 401s while every status surface reported the provider healthy. The
    # two fields were never compared by anything.
    try:
        from providers.catalog import provider_base_url_mismatch

        _llm_cfg = (_settings_for_doctor().get("llm") or {})
        _mismatch = provider_base_url_mismatch(
            str(_llm_cfg.get("provider") or ""), _llm_cfg.get("base_url")
        )
        if _mismatch:
            _fail("LLM endpoint", _mismatch,
                  "Remove llm.base_url from ~/.feral/settings.json")
        elif _llm_cfg.get("provider"):
            _pass("LLM endpoint",
                  f"{_llm_cfg.get('provider')} -> "
                  f"{_llm_cfg.get('base_url') or 'provider default'}")
    except Exception as exc:  # pragma: no cover - defensive
        _warn("LLM endpoint", f"could not check: {exc}")

    # ── 7b. Pairing & access ──
    #
    # Doctor reported green while pairing was structurally impossible.
    # A probe of 127.0.0.1:port proves the process is up; it proves
    # nothing about the address printed into a QR code, which is the
    # only address a phone ever tries. These checks compare the
    # configured intent against what a phone would actually be handed.
    from config.access_mode import AccessMode, configured_bind_host, current_mode
    from config.loader import ConfigLoader

    try:
        cfg_for_access = ConfigLoader()
        cfg_for_access.discover()
    except Exception as exc:  # pragma: no cover - defensive
        cfg_for_access = None
        _warn("Pairing & access", f"could not read settings: {exc}")

    if cfg_for_access is not None:
        access_mode = current_mode(cfg_for_access)
        persisted_bind = configured_bind_host(cfg_for_access)

        if persisted_bind != access_mode.bind_host:
            _fail(
                "Access mode coherence",
                f"mode {access_mode.value!r} requires bind_host "
                f"{access_mode.bind_host} but settings.json says {persisted_bind}",
                "Run `feral doctor --fix`, or re-apply the mode from Settings",
            )
        else:
            _pass(
                "Access mode coherence",
                f"{access_mode.value} (binds {access_mode.bind_host})",
            )

        if access_mode is AccessMode.LOCALHOST:
            _info(
                "Phone pairing",
                "disabled in 'This computer only' mode — switch to Same WiFi to pair",
            )
        elif access_mode is AccessMode.RELAY:
            _warn(
                "Phone pairing",
                "'Any network' is selected but the relay tunnel is not implemented",
                "Switch to Same WiFi in Settings until the relay ships",
            )
        else:
            # Ask the resolver the same question the pair modal asks. Its
            # refusal text is written for a human, so print it verbatim
            # rather than paraphrasing it into something vaguer.
            try:
                from api.routes.devices import PairUnavailable, _resolve_pair_origin

                try:
                    origin = _resolve_pair_origin()
                    _pass("Phone pairing", f"a QR would point at {origin}")
                except PairUnavailable as exc:
                    _fail("Phone pairing", str(exc),
                          "Fix the condition above, then re-open the pair screen")
            except Exception as exc:  # pragma: no cover - defensive
                _warn("Phone pairing", f"could not evaluate: {exc}")

        if brain_tls_enabled():
            # iOS has no trust override and the brain's own cert is
            # self-signed, so TLS on the LAN is strictly worse than
            # cleartext there: the phone refuses the connection outright.
            _warn(
                "TLS vs phone pairing",
                "TLS is on; iOS refuses the brain's self-signed certificate",
                "Disable TLS for LAN pairing, or pair over a trusted remote URL",
            )

    # ── 7c. Tailscale (Mode C remote pairing transport) ──
    #
    # WHY: `feral access remote-up` shells out to
    # `tailscale funnel --bg <port>` with a 20s ceiling. An operator
    # whose machine had no Tailscale at all waited the full 20 seconds
    # to be told the command "timed out after 20.0s", then pasted a
    # download URL into a shell as if it were a command. Doctor, the
    # one tool whose entire job is answering "is my install healthy?",
    # had zero Tailscale probes and reported green the whole time.
    # These rows make the remote transport visible *before* the operator
    # spends 20 seconds discovering it is missing.
    #
    # Severity is a function of intent. Not having Tailscale is the
    # normal, correct state for someone pairing over WiFi, so it reports
    # as ℹ; it is only a ✘ when the operator has actually selected
    # remote mode and is therefore depending on it.
    #
    # Every probe runs on a 2.5s budget: a diagnostic for a hang must
    # not itself be able to hang. A probe that runs out of budget is
    # reported as its own state ("did not answer") rather than being
    # collapsed into "not installed", because a wedged daemon and a
    # missing binary need completely different fixes.
    import subprocess as _ts_subprocess

    console.print()
    console.print("[bold]Tailscale (remote access)[/bold]")

    TS_PROBE_BUDGET = 2.5

    # "Am I depending on Tailscale?". Guarded on cfg_for_access so an
    # unreadable settings.json (already reported by 7b) degrades to
    # "not remote" instead of raising NameError on access_mode.
    ts_remote_mode = (
        cfg_for_access is not None and access_mode is AccessMode.TAILSCALE
    )
    ts_install_cmd = (
        "brew install --cask tailscale"
        if sys.platform == "darwin"
        else "curl -fsSL https://tailscale.com/install.sh | sh"
    )

    try:
        from integrations import tailscale as ts_mod
    except Exception as exc:  # pragma: no cover - module ships in the wheel
        ts_mod = None
        _warn(
            "Tailscale integration",
            f"integrations.tailscale failed to import: {exc}",
            "Reinstall FERAL: pip install -e '.[all]'",
        )

    def _ts_probe(args: list[str]) -> tuple[str, str]:
        """Run one short `tailscale` command and classify the outcome.

        Returns ``(state, payload)`` where state is one of:

          ``ok``          rc 0; payload is stdout
          ``timeout``     no answer inside the probe budget
          ``down``        daemon socket missing / not answering
          ``logged_out``  daemon up, node not authenticated
          ``missing``     binary disappeared between checks
          ``error``       anything else; payload is stderr

        Reuses ``integrations.tailscale._run`` so the probe inherits the
        exact binary lookup and userspace-socket handling that the real
        ``remote-up`` path uses. A doctor that probed a different
        socket than the feature would be worse than no doctor. The one
        thing it does not inherit is the timeout: that is passed
        explicitly, because doctor asks questions, it does not wait on
        daemons.
        """
        try:
            proc = ts_mod._run(args, timeout=TS_PROBE_BUDGET)
        except ts_mod.TailscaleNotInstalled:
            return ("missing", "")
        except ts_mod.TailscaleError as exc:
            # ``_run`` folds subprocess.TimeoutExpired into a
            # TailscaleSubprocessFailure but chains the original as
            # __cause__, which is the only reliable way to tell "the
            # daemon is wedged" apart from "the CLI errored".
            if isinstance(exc.__cause__, _ts_subprocess.TimeoutExpired):
                return (
                    "timeout",
                    f"`tailscale {' '.join(args)}` did not answer within "
                    f"{TS_PROBE_BUDGET}s",
                )
            return ("error", str(exc))

        if proc.returncode == 0:
            return ("ok", proc.stdout or "")

        stderr = (proc.stderr or "").strip()
        classified = ts_mod._classify_stderr(stderr)
        if isinstance(classified, ts_mod.TailscaleDaemonUnreachable):
            return ("down", stderr)
        if isinstance(classified, ts_mod.TailscaleNotLoggedIn):
            return ("logged_out", stderr)
        # `tailscale status` phrases a dead daemon as "failed to connect
        # to local Tailscale service; is Tailscale running?". There is
        # no socket path in that string, so ``_classify_stderr`` cannot
        # recognise it. Observed on macOS with the app not launched.
        low = stderr.lower()
        if "failed to connect to local tailscale" in low or "is tailscale running" in low:
            return ("down", stderr)
        return ("error", stderr)

    def _ts_funnel_ports(raw: str) -> list[int]:
        """Local ports the running Funnel forwards to.

        Mirrors the parse in ``integrations.tailscale.funnel_status``
        (``Web.<host>.Handlers.<path>.Proxy`` looks like
        ``http://127.0.0.1:9090``). It is re-derived here rather than
        calling ``funnel_status()`` because that helper runs the CLI on
        the module's 8s default timeout, and no doctor probe is allowed
        to block for that long.
        """
        try:
            data = json.loads((raw or "").strip() or "{}")
        except (ValueError, TypeError):
            return []
        if not isinstance(data, dict):
            return []
        ports: set[int] = set()
        for _host, conf in (data.get("Web") or {}).items():
            for _path, handler in ((conf or {}).get("Handlers") or {}).items():
                proxy = (handler or {}).get("Proxy") or ""
                if ":" not in proxy:
                    continue
                tail = proxy.rsplit(":", 1)[-1].split("/")[0]
                digits = "".join(ch for ch in tail if ch.isdigit())
                if digits:
                    ports.add(int(digits))
        return sorted(ports)

    ts_logged_in = False
    ts_dns_name = ""
    # Tri-state on purpose: None means "we could not find out", which is
    # different from False ("there is no funnel"). The coherence row
    # below refuses to accuse the operator of anything on unknown data.
    ts_funnel_serves_brain: bool | None = None

    ts_binary = shutil.which("tailscale") if ts_mod is not None else None

    if ts_mod is None:
        pass  # already reported above; nothing else is probeable
    elif not ts_binary:
        if ts_remote_mode:
            _fail(
                "Tailscale binary",
                f"not installed, but access mode is '{AccessMode.TAILSCALE.value}' "
                f"({AccessMode.TAILSCALE.label}), so remote pairing cannot work",
                f"Install Tailscale: {ts_install_cmd}   then run: feral access remote-up",
            )
        else:
            # Not having Tailscale is the *expected* state for the
            # WiFi-pairing majority. Say what it would be for, and stop.
            _info(
                "Tailscale binary",
                f"not installed. Only needed for '{AccessMode.TAILSCALE.label}' "
                f"pairing (install: {ts_install_cmd})",
            )
    else:
        _pass("Tailscale binary", ts_binary)

        # ── daemon ──
        state, payload = _ts_probe(["status", "--json"])
        if state == "ok":
            _pass("Tailscale daemon", "tailscaled responding")
        elif state == "logged_out":
            # The daemon answered; it just has no account. That is the
            # next row's story, not a daemon problem.
            _pass("Tailscale daemon", "tailscaled responding")
        elif state == "down":
            if ts_remote_mode:
                _fail(
                    "Tailscale daemon",
                    "installed but tailscaled is not running",
                    "Start Tailscale (open the Tailscale app, or `sudo tailscaled` "
                    "on Linux), then: tailscale up",
                )
            else:
                _info("Tailscale daemon", "not running. Start Tailscale if you want remote pairing")
        elif state == "timeout":
            # Distinct from "not installed" and distinct from "down":
            # the socket is there and something is holding it.
            if ts_remote_mode:
                _fail(
                    "Tailscale daemon",
                    f"{payload}. The daemon is installed but wedged",
                    "Restart Tailscale (quit the menu-bar app and reopen it, or "
                    "`sudo launchctl kickstart -k system/com.tailscale.tailscaled`), "
                    "then re-run: feral doctor",
                )
            else:
                _warn(
                    "Tailscale daemon",
                    f"{payload}. The daemon is installed but wedged",
                    "Restart Tailscale (quit the menu-bar app and reopen it), "
                    "then re-run: feral doctor",
                )
        elif state == "missing":  # pragma: no cover - race with the which() above
            _info("Tailscale daemon", "binary disappeared mid-probe")
        else:
            detail = payload or "unknown error"
            if ts_remote_mode:
                _fail(
                    "Tailscale daemon",
                    f"`tailscale status` failed: {detail[:200]}",
                    "Run `tailscale status` yourself to see the full error, "
                    "then restart Tailscale",
                )
            else:
                _warn(
                    "Tailscale daemon",
                    f"`tailscale status` failed: {detail[:200]}",
                    "Run `tailscale status` yourself to see the full error",
                )

        # ── logged-in account ──
        if state in ("ok", "logged_out"):
            backend = ""
            ts_tailnet = ""
            if state == "ok":
                try:
                    ts_data = json.loads((payload or "").strip() or "{}")
                except (ValueError, TypeError):
                    ts_data = {}
                if not isinstance(ts_data, dict):
                    ts_data = {}
                backend = str(ts_data.get("BackendState") or "")
                self_node = ts_data.get("Self") or {}
                ts_dns_name = str(self_node.get("DNSName") or "").rstrip(".")
                ts_tailnet = str((ts_data.get("CurrentTailnet") or {}).get("Name") or "")

            if state == "ok" and backend in ("Running", "Starting"):
                ts_logged_in = True
                detail = ts_dns_name or "logged in"
                if ts_tailnet:
                    detail += f"  (tailnet {ts_tailnet})"
                _pass("Tailscale account", detail)
            elif state == "ok" and backend == "Stopped":
                # Authenticated but administratively down (`tailscale
                # down`). Funnel will not carry traffic in this state,
                # and the phone sees a connection that never completes.
                if ts_remote_mode:
                    _fail(
                        "Tailscale account",
                        "logged in but Tailscale is stopped, so no traffic is carried",
                        "Run: tailscale up",
                    )
                else:
                    _info("Tailscale account", "logged in but stopped (`tailscale up` to connect)")
            else:
                # Logged out: either the CLI said so on stderr, or the
                # JSON reports NeedsLogin / NoState.
                if ts_remote_mode:
                    _fail(
                        "Tailscale account",
                        "logged out. A logged-out node has no tailnet name to pair against",
                        "Run: tailscale up   (opens the browser login), "
                        "then: feral access remote-up",
                    )
                else:
                    _info("Tailscale account", "logged out. Run `tailscale up` if you want remote pairing")

        # ── funnel serving the brain port ──
        if ts_logged_in:
            fstate, fpayload = _ts_probe(["funnel", "status", "--json"])
            if fstate == "ok":
                funnel_ports = _ts_funnel_ports(fpayload)
                ts_funnel_serves_brain = port in funnel_ports
                if ts_funnel_serves_brain:
                    public = ts_mod.funnel_url(port, dns_name=ts_dns_name) or "(unknown URL)"
                    _pass("Tailscale Funnel", f"serving :{port} at {public}")
                elif funnel_ports:
                    # A funnel exists but points somewhere else, so the
                    # brain is not the thing on the public URL.
                    other = ", ".join(f":{p}" for p in funnel_ports)
                    if ts_remote_mode:
                        _fail(
                            "Tailscale Funnel",
                            f"forwarding {other}, not the brain port :{port}",
                            "Re-point Funnel at the brain: feral access remote-up",
                        )
                    else:
                        _info("Tailscale Funnel", f"forwarding {other} (not the brain port :{port})")
                else:
                    if ts_remote_mode:
                        _fail(
                            "Tailscale Funnel",
                            f"no funnel is serving the brain port :{port}",
                            "Run: feral access remote-up",
                        )
                    else:
                        _info("Tailscale Funnel", "not enabled, remote pairing is off")
            elif fstate == "timeout":
                if ts_remote_mode:
                    _fail(
                        "Tailscale Funnel",
                        f"{fpayload}. Cannot confirm the brain is reachable remotely",
                        "Restart Tailscale, then re-run: feral doctor",
                    )
                else:
                    _warn(
                        "Tailscale Funnel",
                        f"{fpayload}",
                        "Restart Tailscale, then re-run: feral doctor",
                    )
            else:
                detail = fpayload or "unknown error"
                if ts_remote_mode:
                    _fail(
                        "Tailscale Funnel",
                        f"could not read funnel status: {detail[:200]}",
                        "Run `tailscale funnel status` yourself to see the full error",
                    )
                else:
                    _info("Tailscale Funnel", f"could not read funnel status: {detail[:120]}")

    # ── mode coherence: what settings.json promises vs what is live ──
    #
    # This is the row that would have caught the original incident: the
    # pair QR is built from ``access.tailscale.tailnet_url``, so a
    # remote-mode brain with an empty or stale URL hands the phone an
    # address that answers nothing, while every other probe stays green.
    if cfg_for_access is not None and ts_mod is not None:
        try:
            ts_stored_url = cfg_for_access.access_remote_url
        except Exception as exc:  # pragma: no cover - defensive
            ts_stored_url = ""
            _warn("Remote access coherence", f"could not read access settings: {exc}")

        if ts_remote_mode:
            if not ts_stored_url:
                _fail(
                    "Remote access coherence",
                    f"access mode is '{AccessMode.TAILSCALE.value}' but no Funnel URL "
                    "is stored (access.tailscale.tailnet_url is empty), so the pair QR "
                    "has nothing to point at",
                    "Publish a Funnel URL with `feral access remote-up`, or switch to "
                    "Same WiFi in Settings",
                )
            elif ts_funnel_serves_brain is True:
                _pass("Remote access coherence", f"remote mode → {ts_stored_url}")
            elif ts_funnel_serves_brain is False:
                _fail(
                    "Remote access coherence",
                    f"settings advertise {ts_stored_url} but no funnel is serving "
                    f":{port}. Phones will be handed a dead URL",
                    "Re-publish the funnel with `feral access remote-up`, or clear the "
                    "stale URL with `feral access remote-down`",
                )
            else:
                # Funnel state unknown (daemon down, probe timed out).
                # The rows above already carry the actionable failure;
                # repeating it here in red would be noise.
                _info(
                    "Remote access coherence",
                    f"stored remote URL {ts_stored_url}; live funnel state unknown (see above)",
                )
        elif ts_funnel_serves_brain is True:
            # A funnel is publishing the brain to the internet while the
            # operator's stated intent is loopback/LAN. Nothing is
            # broken, but nobody asked for this exposure.
            _warn(
                "Remote access coherence",
                f"a Funnel is publishing :{port} to the internet, but access mode is "
                f"'{access_mode.value}' ({access_mode.label})",
                "Stop the funnel with `feral access remote-down`, or switch to "
                "Tailscale mode so the pair URL matches",
            )

    # ── 8. Browser runtime (Chrome + CDP + Playwright) ──
    #
    # The actual runtime path is `BrowserController.connect_over_cdp` to
    # whatever Chrome the user is running on `FERAL_CDP_PORT` (default
    # 9222). The previous probe only verified `pw.chromium.launch`,
    # which is the bundled-headless path FERAL does NOT use. That gave
    # operators a false green light. The new probe is layered:
    #
    #   8a. Real CDP endpoint (running Chrome / Chromium / Brave on
    #       the configured port). This is the production signal.
    #   8b. Playwright Python library importable. Required to drive
    #       the connected Chrome via DOM/locator calls. CDP-only mode
    #       still works without it but loses selector healing.
    #   8c. A Chrome / Chromium / Brave binary on disk. Required for
    #       the auto-launch fallback when CDP is cold.
    #
    # The summary line tells the operator exactly which step they are
    # missing and how to fix it — no more "Playwright OK" while the
    # actual browser surface is dead.

    cdp_host = os.getenv("FERAL_CDP_HOST", "localhost")
    cdp_port = int(os.getenv("FERAL_CDP_PORT", "9222"))

    cdp_alive = False
    try:
        import urllib.request as _urlreq
        with _urlreq.urlopen(
            f"http://{cdp_host}:{cdp_port}/json/version",
            timeout=2,
        ) as resp:
            if resp.status == 200:
                cdp_alive = True
    except Exception:
        cdp_alive = False
    if cdp_alive:
        _pass(
            "Chrome (CDP endpoint)",
            f"reachable on http://{cdp_host}:{cdp_port}",
        )
    else:
        # v2026.5.36 — was `_warn`. The CDP endpoint being cold on a
        # fresh install is the default state: FERAL's BrowserController
        # auto-launches Chrome with the right `--remote-debugging-port`
        # flag the first time an agent asks for a browser, provided a
        # binary exists (probed separately below). The probe is
        # informational; an absent CDP only blocks computer-use the
        # instant a binary is *also* missing.
        _info(
            "Chrome (CDP endpoint)",
            f"not running on http://{cdp_host}:{cdp_port} (FERAL will "
            "auto-launch on first computer-use action)",
        )

    try:
        import importlib
        importlib.import_module("playwright.async_api")
        _pass(
            "Playwright (driver lib)",
            "importable — DOM/locator actions enabled (used over CDP, "
            "not bundled chromium)",
        )
    except ImportError:
        _warn(
            "Playwright (driver lib)",
            "not installed — CDP-only mode (no selector healing)",
            "pip install 'feral-ai[browser]'  (or: pip install playwright)",
        )

    chrome_candidates: list[str] = []
    if platform.system() == "Darwin":
        chrome_candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    elif platform.system() == "Linux":
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            p = shutil.which(name)
            if p:
                chrome_candidates.append(p)
    else:
        for name in ("chrome.exe", "chromium.exe"):
            p = shutil.which(name)
            if p:
                chrome_candidates.append(p)

    chrome_bin = next((c for c in chrome_candidates if os.path.isfile(c)), None)
    if chrome_bin:
        _pass("Chrome binary", f"found at {chrome_bin}")
    else:
        _warn(
            "Chrome binary",
            "no Chrome / Chromium / Brave on disk — auto-launch will fail",
            "Install Google Chrome, Chromium, or Brave so FERAL can "
            "boot a CDP-enabled browser when one isn't already running",
        )

    # ── 9. Node.js ──
    node_bin = shutil.which("node")
    if node_bin:
        import subprocess
        try:
            ver_out = subprocess.check_output([node_bin, "--version"], text=True).strip()
            major = int(ver_out.lstrip("v").split(".")[0])
            if major >= 20:
                _pass("Node.js", ver_out)
            else:
                _warn("Node.js", f"{ver_out} (recommend >= 20 for client dev)",
                      "Install Node 20+: https://nodejs.org")
        except Exception:
            _warn("Node.js", "found but could not determine version")
    else:
        # v2026.5.36 — was `_warn`. Node.js is only required if the
        # operator wants to rebuild the webui_v2 bundle locally. The
        # shipped wheel already carries the compiled bundle, so the
        # runtime path is fully Node-free. Demoted to info.
        _info("Node.js", "not found — only needed if you plan to rebuild webui_v2 locally")

    # ── 10. Local audio backends ──
    console.print()
    console.print("[bold]Local Audio[/bold]")
    try:
        from perception.audio_pipeline import detect_local_audio_capabilities
        caps = detect_local_audio_capabilities()
        if caps["local_stt"]:
            _pass("Local STT (faster-whisper)", f"models: {', '.join(caps['stt_models'])}")
        else:
            # v2026.5.36 — was `_warn`. Local STT is explicitly an
            # opt-in extra (`pip install 'feral-ai[stt]'`). Cloud STT
            # via OpenAI / Google works without it. Not installing it
            # is a deliberate choice, not a problem.
            _info("Local STT (faster-whisper)",
                  "not installed — cloud STT only (install via `pip install 'feral-ai[stt]'`)")
        if caps["local_tts"]:
            _pass("Local TTS (piper)", f"voices: {', '.join(caps['tts_voices'])}")
        else:
            # v2026.5.36 — was `_warn`. Symmetric demote with the STT
            # case above: Piper is opt-in via `[tts]`.
            _info("Local TTS (piper)",
                  "not installed — cloud TTS only (install via `pip install 'feral-ai[tts]'`)")
    except Exception as exc:
        _warn("Local Audio", f"detection failed: {exc}")

    # ── 11. macOS GUI permissions (Screen Recording + Accessibility) ──
    # Only meaningful on Darwin: gui_computer_use / agentic_computer_use
    # cannot synthesize input (Accessibility) or capture pixels beyond
    # the menu bar wallpaper (Screen Recording) without explicit grants.
    # We surface the *real* TCC state via Apple's APIs rather than
    # claiming readiness based on package presence alone.
    if platform.system() == "Darwin":
        console.print()
        console.print("[bold]macOS GUI Permissions[/bold]")
        # Lane 07  — surface the canonical macOS Settings deeplink
        # alongside the human remediation text. Lane 06's TCC_CATALOG
        # holds the ``x-apple.systempreferences:`` URL per permission;
        # rendering it makes the doctor row "click-to-fix" once the
        # operator's terminal supports OSC-8 hyperlinks.
        try:
            from agents.tcc_card import TCC_CATALOG
        except Exception:
            TCC_CATALOG = {}

        def _tcc_remediation(probe) -> str:
            deeplink = (TCC_CATALOG.get(probe.permission) or {}).get("macos_deeplink", "")
            if deeplink:
                return f"{probe.setup_step}  ({deeplink})"
            return probe.setup_step

        try:
            from security.macos_permissions import all_gui_permission_statuses
            for probe in all_gui_permission_statuses():
                label = f"{probe.permission.replace('_', ' ').title()} (TCC)"
                if probe.status == "granted":
                    _pass(label, f"{probe.api}: granted")
                elif probe.status == "denied":
                    # v2026.5.36 — was `_fail`. A denied TCC grant
                    # only blocks the GUI computer-use code path
                    # (synthetic clicks via Accessibility, screen
                    # capture via Screen Recording). Users who never
                    # touch GUI computer-use shouldn't see a red ✘
                    # for an entitlement they intentionally withheld.
                    # We keep the remediation in `fixes` so anyone
                    # who *does* want GUI computer-use has a path,
                    # but the probe no longer claims the install is
                    # broken.
                    _warn(
                        label,
                        f"{probe.api}: denied (only blocks GUI computer-use)",
                        _tcc_remediation(probe),
                    )
                elif probe.status == "unknown":
                    # v2026.5.38 (audit-r12 / Lane 06) — the new
                    # Calendar / Reminders / Contacts / FDA probes
                    # rely on PyObjC EventKit / Contacts bindings
                    # which are NOT base dependencies (they're only
                    # needed when the operator actually uses those
                    # skills). Demote those to _info so a clean
                    # install stays warning-free. Accessibility +
                    # Screen Recording stay as _warn because their
                    # PyObjC bindings ARE base deps; reaching
                    # ``unknown`` there is a real install issue.
                    detail = probe.error or "PyObjC not available"
                    if probe.permission in (
                        "calendar",
                        "reminders",
                        "contacts",
                        "full_disk_access",
                    ):
                        _info(
                            label,
                            f"{detail} — install with "
                            f"`pip install 'feral-ai[macos-extras]'` if you "
                            f"plan to use this surface",
                        )
                    else:
                        _warn(
                            label,
                            f"{detail} (upgrade to feral-ai>=2026.5.36 to fix)",
                            probe.setup_step,
                        )
                elif probe.status == "restricted":
                    _warn(
                        label,
                        f"{probe.api}: restricted (MDM/parental controls)",
                        probe.setup_step,
                    )
                else:
                    _info(label, "not_applicable")
        except Exception as exc:
            _warn("macOS GUI Permissions", f"probe failed: {exc}")

    # ── 12. Key dependencies ──
    console.print()
    console.print("[bold]Dependencies[/bold]")
    dep_pkgs = [
        ("fastapi", "FastAPI", True),
        ("uvicorn", "Uvicorn", True),
        ("websockets", "WebSockets", True),
        ("httpx", "HTTPX", True),
        ("pydantic", "Pydantic", True),
    ]
    for pkg, label, critical in dep_pkgs:
        try:
            __import__(pkg)
            _pass(label, "importable")
        except ImportError:
            if critical:
                _fail(label, "not installed", f"pip install {pkg}")
            else:
                _warn(label, "not installed")

    # ── PR 12: focused doctors for agent runtimes ──
    console.print()
    console.print("[bold]Agent runtimes (PR 12)[/bold]")

    # local-agent: workspace grants present?
    try:
        grants_path = home / "workspace_grants.json"
        if grants_path.exists():
            import json as _json
            grants_data = _json.loads(grants_path.read_text() or "{}")
            grant_count = len(grants_data.get("grants", grants_data)) if isinstance(grants_data, dict) else 0
            _pass(
                "Local-agent grants",
                f"{grant_count} workspace grant(s) registered" if grant_count else "no grants yet",
            )
        else:
            # v2026.5.36 — was `_warn`. The local-agent runtime
            # prompts interactively the first time `write_file` is
            # called against an un-granted directory; pre-authorizing
            # via `feral grant` is purely a convenience for headless
            # / scripted runs. No fix is *required* on a fresh
            # install.
            _info(
                "Local-agent grants",
                "no workspace_grants.json yet — write_file will prompt on first use",
            )
    except Exception as exc:
        _warn("Local-agent grants", f"could not read grants: {exc}")

    # coding-agent: CodingRunStore initialisable
    try:
        from agents.coding_run import CodingRunStore
        coding_db = home / "coding_runs.db"
        store = CodingRunStore(db_path=coding_db)
        _pass("Coding-agent store", f"SQLite ready at {coding_db}")
        del store
    except Exception as exc:
        _warn(
            "Coding-agent store",
            f"could not initialise: {exc}",
            "Ensure $FERAL_HOME is writable; rerun `feral setup` to fix.",
        )

    # voice doctor: realtime provider key set?
    #
    # Same vault-first probe shape as the LLM-credentials section
    # above. The pre-v2026.5.28 code relied on a local ``creds_data``
    # dict populated from the legacy plaintext ``credentials.json``;
    # that dict was deleted when the LLM section moved to BlindVault,
    # so we re-query the vault here directly.
    def _key_available(key: str) -> bool:
        if os.environ.get(key):
            return True
        try:
            from security.vault import BlindVault  # type: ignore

            return bool(BlindVault().get_credential(key))
        except Exception:
            return False

    voice_keys = ("OPENAI_API_KEY", "GOOGLE_API_KEY")
    have_voice_key = any(_key_available(k) for k in voice_keys)
    if have_voice_key:
        providers = []
        if _key_available("OPENAI_API_KEY"):
            providers.append("OpenAI Realtime")
        if _key_available("GOOGLE_API_KEY"):
            providers.append("Google Gemini Realtime")
        _pass("Voice runtime", "key set: " + ", ".join(providers))
    else:
        # v2026.5.36 — was `_warn`. Voice (in-composer realtime
        # speech) is opt-in. The text agent works perfectly without
        # an OpenAI/Google realtime key — many operators run FERAL
        # voice-free. Demoted to info so a clean install no longer
        # raises a yellow flag for a feature the user may never want.
        _info(
            "Voice runtime",
            "no realtime provider key set — voice is off (set OPENAI_API_KEY or "
            "GOOGLE_API_KEY to enable in-composer voice)",
        )

    # computer-use: provider-neutral driver importable
    try:
        from agents.computer_use_driver import normalize_action  # noqa: F401
        _pass("Computer-use driver", "ComputerUseDriver normalisation ready")
    except Exception as exc:
        _fail(
            "Computer-use driver",
            f"import failed: {exc}",
            "Reinstall feral-ai; the driver lives in feral-core/agents/computer_use_driver.py",
        )

    # upload store: PR 10
    try:
        from memory.uploads import UploadStore
        uploads_root = home / "uploads"
        _ = UploadStore(root=uploads_root)
        _pass("Upload store", f"local-first chat uploads at {uploads_root}")
    except Exception as exc:
        _warn(
            "Upload store",
            f"could not initialise: {exc}",
            "Ensure $FERAL_HOME is writable.",
        )

    # ── Cost budget (Lane 04 / Lane 07 ) ─────────────────────────
    #
    # Surfaces per-call-site caps + current spend + reset times. The
    # data is read straight off Lane 04's CostBudget rollups; if the
    # brain is running, its in-process state lives in ``cost.db`` so
    # doctor reads the same source of truth without a brain RTT. When
    # CostBudget is unavailable (degraded install), we render a single
    # info row rather than failing the whole doctor.
    console.print()
    console.print("[bold]Cost budget[/bold]")
    try:
        from cost.budget import CostBudget, DEFAULT_COST_SETTINGS, window_reset_at
        cb = CostBudget()
        # ``ensure_ready`` opens the persistent rollup DB and loads
        # the current hour/day spend snapshot for each call_site.
        asyncio.run(cb.ensure_ready())
        try:
            now_ts = __import__("time").time()
            for site, cap_cfg in DEFAULT_COST_SETTINGS["cost"]["per_call_site_caps"].items():
                hour_cap = cap_cfg.get("per_hour_usd", 0.0)
                spent = cb.current_spend(site, "hour")
                reset_at = window_reset_at("hour", now_ts)
                reset_min = max(0, int((reset_at - now_ts) / 60.0))
                detail = (
                    f"${spent:.4f} / ${hour_cap:.2f} per hour "
                    f"(resets in {reset_min}m)"
                )
                if hour_cap > 0 and spent >= hour_cap:
                    _fail(
                        f"cost.{site}",
                        detail,
                        f"Wait {reset_min}m for the cap to reset, or raise it via Settings → Cost.",
                    )
                elif hour_cap > 0 and spent >= hour_cap * 0.8:
                    _warn(f"cost.{site}", detail)
                else:
                    _pass(f"cost.{site}", detail)
            # Global caps
            global_hour = DEFAULT_COST_SETTINGS["cost"].get("global_per_hour_usd", 0.0)
            global_day = DEFAULT_COST_SETTINGS["cost"].get("global_per_day_usd", 0.0)
            if global_hour > 0:
                spent_h = cb.current_spend(None, "hour")
                _pass("cost.__global__/hour", f"${spent_h:.4f} / ${global_hour:.2f}")
            if global_day > 0:
                spent_d = cb.current_spend(None, "day")
                _pass("cost.__global__/day", f"${spent_d:.4f} / ${global_day:.2f}")
        finally:
            asyncio.run(cb.close())
    except Exception as exc:
        _info("Cost budget", f"unavailable ({exc})")

    # ── Summary ──
    console.print()
    parts = []
    if passed:
        parts.append(f"[green]{passed} passed[/green]")
    if infos:
        parts.append(f"[cyan]{infos} info[/cyan]")
    if warnings:
        parts.append(f"[yellow]{warnings} warnings[/yellow]")
    if failures:
        parts.append(f"[red]{failures} failures[/red]")
    # v2026.5.36 — panel border colour reflects the *actual* severity
    # so a fresh install (no warnings, no failures) shows a green
    # border even if there are info items, and a broken install shows
    # red. This makes the first impression honest at a glance.
    if failures:
        border = "red"
    elif warnings:
        border = "yellow"
    else:
        border = "green"
    console.print(Panel(", ".join(parts), title="Summary", border_style=border))

    if fixes:
        console.print()
        console.print("[bold]Suggested fixes:[/bold]")
        for i, fix in enumerate(fixes, 1):
            console.print(f"  {i}. {_esc(fix)}")
        console.print()

    # ── Lane 07  — exit code reflects severity ──
    #
    # Pre-Lane-07 doctor always returned 0; finding 07 #1 calls out
    # the operator-facing acceptance "exit 0 if all green or only-
    # yellow; 1 if any red." Shell scripts (CI, install wrappers)
    # rely on this contract.
    if failures:
        sys.exit(1)
    return 0


def cmd_setup(*, browser: bool = False, terminal: bool = False, from_step: str = ""):
    """Launch the guided setup wizard, then auto-generate a session token.

    When ``browser=True`` the CLI opens http://localhost:9090/setup in
    the default browser so the user gets the v2 /setup page instead of
    the terminal. Requires the brain to be running.

    Lane U1 — ``from_step`` mirrors the ``--from-step`` CLI flag and
    re-enters one specific wizard step (e.g. ``llm_model``) without
    deleting the resume sidecar or re-prompting on all earlier steps.
    """
    if browser and not terminal:
        _open_browser_setup()
        # Even in browser mode we still want a session token for the
        # server-side auth layer; generate it now.
    else:
        # Print a one-line ssh -t hint when the wizard is launched
        # without a controlling TTY so the operator isn't surprised
        # when the arrow-key picker silently degrades to a numeric
        # prompt.
        try:
            from cli import ui_kit

            ui_kit.warn_non_interactive_setup_hint()
        except Exception:
            pass
        try:
            from cli.setup import run_setup
            run_setup(from_step=from_step)
        except ImportError:
            print("Setup wizard not available.")
            sys.exit(1)

    from security.session_auth import generate_session_token, save_session_token, load_session_token
    if load_session_token() is None:
        token = generate_session_token()
        save_session_token(token)
        print(f"  Session token generated: {token[:8]}...{token[-4:]}")
        print(f"  Stored in {feral_home() / 'session_token'}")


def _open_browser_setup() -> None:
    """Open http://localhost:9090/setup in the default browser."""
    import webbrowser
    url = f"{_runtime_http_base()}/setup"
    print(f"  Opening {url} in your browser...")
    print("  (Start the brain first with `feral serve` if you see a connection error.)")
    try:
        webbrowser.open(url)
    except Exception as exc:
        print(f"  Could not open browser: {exc}")
        print(f"  Paste this into your browser instead: {url}")


def cmd_pair(name: str, list_devices: bool, revoke: str, prune: int = -1):
    """Manage per-node device pairing."""
    from security.device_pairing import DevicePairingStore

    store = DevicePairingStore()

    if list_devices:
        devices = store.list_devices()
        if not devices:
            print("  No paired devices.")
            return
        for d in devices:
            import datetime
            ts = datetime.datetime.fromtimestamp(d["paired_at"]).strftime("%Y-%m-%d %H:%M")
            seen = ""
            if d["last_seen"]:
                seen = f", last seen {datetime.datetime.fromtimestamp(d['last_seen']).strftime('%Y-%m-%d %H:%M')}"
            print(f"  {d['device_id'][:12]}...  {d['name']:20s}  paired {ts}{seen}")
        return

    if revoke:
        ok = store.revoke_device(revoke)
        if ok:
            print(f"  Revoked device {revoke}")
        else:
            print(f"  Device {revoke} not found.")
        return

    if prune >= 0:
        result = store.revoke_unclaimed(older_than_seconds=float(prune))
        print(f"  Pruned {result['pruned']} unclaimed pairings (kept {result['kept']}).")
        for row in result["rows"]:
            print(f"    - {row}")
        return

    if not name:
        name = "unnamed"
    result = store.pair_device(name)
    print(f"  Device paired: {result['name']}")
    print(f"  Device ID:     {result['device_id']}")
    print(f"  Token:         {result['token']}")
    print()
    qr_data = f"feral-pair://{result['token']}"
    print(f"  QR data: {qr_data}")
    print("  Pass the token as ?api_key=<token> when connecting to /v1/node")


def cmd_wake_test():
    """Test wake word detection from the microphone for 10 seconds."""
    print("\n  Wake Word Test")
    print("  " + "=" * 40)

    try:
        import openwakeword
    except ImportError:
        print("  openwakeword not installed.")
        print("  Install: pip install 'feral-ai[wake]'")
        print("  (Downloads ~50 MB model on first use)")
        sys.exit(1)

    from perception.wake_word import WakeWordDetector, WakeWordConfig
    detector = WakeWordDetector(WakeWordConfig(enabled=True))
    ml_mode = "ML (openwakeword)" if detector._oww_model else "Energy-based fallback"
    print(f"  Mode:   {ml_mode}")
    print(f"  Phrase: {detector._config.phrase}")
    model_name = os.environ.get("FERAL_WAKE_MODEL", "hey_jarvis_v0.1")
    print(f"  Model:  {model_name}")
    print("\n  Listening for 10 seconds... Say the wake phrase!\n")

    try:
        import pyaudio
    except ImportError:
        print("  pyaudio not installed — needed for mic access.")
        print("  Install: pip install pyaudio")
        sys.exit(1)

    import struct
    import time

    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=1280)
    start = time.time()
    detections = 0

    try:
        while time.time() - start < 10:
            pcm = stream.read(1280, exception_on_overflow=False)
            result = asyncio.get_event_loop().run_until_complete(
                detector.process_frame("test", pcm)
            ) if asyncio.get_event_loop().is_running() else asyncio.run(
                detector.process_frame("test", pcm)
            )
            if result and detector.get_state("test").value == "activated":
                detections += 1
                elapsed = time.time() - start
                print(f"  [{elapsed:.1f}s] WAKE WORD DETECTED! (#{detections})")
                detector.force_deactivate("test")
            remaining = 10 - (time.time() - start)
            if int(remaining) % 3 == 0 and remaining > 0:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

    print(f"\n  Done. Detections: {detections}")
    if detections == 0 and not detector._oww_model:
        print("  Tip: Install openwakeword for better detection: pip install openwakeword onnxruntime")


def cmd_marketplace(action: str, query: str, registry: str | None = None):
    """Marketplace CLI commands.

    ``install`` delegates to :mod:`cli.install`, which talks directly to
    the FERAL registry (default ``https://registry.feral.sh``) with
    Ed25519 signature verification. ``search``/``list`` still hit the
    local Brain's marketplace proxy.
    """
    if action == "search":
        q = query or "all"
        data = _http_get(f"/api/marketplace/search?q={q}")
        results = data.get("results", [])
        if not results:
            print("  No skills found.")
            return
        for s in results:
            print(f"  {s.get('name', s.get('skill_id', '?'))} — {s.get('description', '')[:60]}")
    elif action == "install":
        if not query:
            print("  Usage: feral marketplace install <item_id>")
            return
        from cli.install import cmd_install
        cmd_install(query, registry=registry)
    elif action == "list":
        data = _http_get("/api/marketplace/installed")
        skills = data.get("skills", [])
        if not skills:
            print("  No marketplace skills installed.")
            return
        for s in skills:
            print(f"  {s.get('name', s.get('skill_id', '?'))} v{s.get('version', '?')}")


def cmd_checkpoints(args) -> int:
    """`feral checkpoints list|show|revert`.

    Reads ``$FERAL_HOME/checkpoints/index.db`` directly instead of calling
    ``/api/checkpoints/*``. That is deliberate: the moment you most want
    to undo what the agent wrote is the moment the brain is wedged, mid
    restart, or answering nothing at all. A recovery tool that depends on
    the thing you are recovering from is not a recovery tool.
    """
    from skills.checkpoints import BASH_NOT_COVERED_NOTE, CheckpointStore, checkpoint_root

    action = getattr(args, "action", None) or "list"
    store = CheckpointStore(checkpoint_root())
    if not store.db_path.exists():
        print(f"  No checkpoints recorded yet ({store.db_path}).")
        return 0

    if action == "list":
        turns = store.list_turns(
            session_id=(getattr(args, "session", "") or "").strip() or None,
            limit=max(1, int(getattr(args, "limit", 20) or 20)),
        )
        if not turns:
            print("  No checkpointed turns.")
            return 0
        print(f"  {'TURN':24s} {'FILES':>5s} {'WRITES':>6s}  SESSION")
        for row in turns:
            print(
                f"  {row['turn_id']:24s} {row['files']:5d} {row['writes']:6d}  "
                f"{row['session_id'] or '-'}"
            )
        print(f"\n  {BASH_NOT_COVERED_NOTE}")
        return 0

    turn_id = (getattr(args, "turn_id", "") or "").strip() or (store.latest_turn() or "")
    if not turn_id:
        print("  No checkpointed turn to act on.")
        return 1

    if action == "show":
        plan = store.plan_revert(turn_id)
        print(f"  Turn {turn_id}")
        for entry in plan["files"]:
            print(f"    [{entry['status']:16s}] {entry['action']:8s} {entry['path']}")
            if entry["detail"]:
                print(f"        {entry['detail']}")
        print(f"\n  {BASH_NOT_COVERED_NOTE}")
        return 0

    if action == "revert":
        result = store.revert_turn(
            turn_id,
            force=bool(getattr(args, "force", False)),
            dry_run=bool(getattr(args, "cp_dry_run", False)),
        )
        for entry in result["files"]:
            print(f"    [{entry['status']:16s}] {entry['action']:8s} {entry['path']}")
            if entry["detail"]:
                print(f"        {entry['detail']}")
        if result.get("error"):
            print(f"\n  {result['error']}")
        else:
            verb = "Would revert" if result["dry_run"] else "Reverted"
            print(f"\n  {verb} {result['reverted_count']} file(s).")
        print(f"  {BASH_NOT_COVERED_NOTE}")
        return 0 if result.get("success") else 1

    print(f"  Unknown action: {action}")
    return 1


def cmd_sync(action: str, file_path: str):
    """Federated sync CLI commands."""
    if action == "status":
        data = _http_get("/api/sync/status")
        if "error" in data:
            print(f"  Error: {data['error']}")
            return
        print(f"  Enabled:     {data.get('enabled', False)}")
        print(f"  Running:     {data.get('running', False)}")
        print(f"  Node ID:     {data.get('node_id', '?')}")
        print(f"  Peers:       {data.get('peer_count', 0)}")
        sched = data.get("scheduler") or {}
        if sched:
            print(f"  Scheduler:   enabled={sched.get('enabled', False)} cadence={sched.get('cadence_seconds', 0)}s")
            for pid, ps in (sched.get("peers") or {}).items():
                lag = ps.get("lag_seconds")
                lag_str = f"{lag:.1f}s" if isinstance(lag, (int, float)) else "—"
                print(
                    f"    · {pid:32s} lag={lag_str:8s} fails={ps.get('consecutive_failures', 0)} "
                    f"sent={ps.get('ops_sent', 0)} recv={ps.get('ops_received', 0)}"
                )
        vc = data.get("vector_clock", {})
        if vc:
            print(f"  Clock:       {json.dumps(vc, indent=2)}")
    elif action == "node-id":
        data = _http_get("/api/sync/node-id")
        print(f"  Node ID: {data.get('node_id', '?')}")
        if data.get("note"):
            print(f"  {data['note']}")
    elif action == "now":
        # `feral sync now`            → sync all peers
        # `feral sync now <peer_id>`  → sync one peer
        import urllib.request
        body = {"peer": file_path} if file_path else {}
        req = urllib.request.Request(
            f"{HTTP_BASE}/api/sync/now",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except Exception as e:
            print(f"  Sync trigger failed: {e}")
            return
        if file_path:
            if result.get("ok"):
                print(f"  Synced {file_path}: sent={result.get('sent', 0)} received={result.get('received', 0)}")
            else:
                print(f"  Sync failed for {file_path}: {result.get('reason')} {result.get('detail', '')}")
        else:
            for r in result.get("results", []):
                if r.get("ok"):
                    print(f"  · {r['peer_id']}: sent={r['sent']} received={r['received']}")
                else:
                    print(f"  · {r.get('peer_id', '?')}: failed ({r.get('reason')})")
    elif action == "peers":
        # `feral sync peers`               → list peers (mDNS + manual)
        # `feral sync peers add host:port` → add manual peer
        # `feral sync peers remove <id>`   → remove peer
        if not file_path or file_path == "list":
            data = _http_get("/api/sync/peers")
            peers = data.get("peers", [])
            if not peers:
                print("  No peers known.")
            else:
                for p in peers:
                    lag_val = p.get("lag_seconds")
                    lag_str = f"{lag_val:.1f}s" if lag_val is not None else "—"
                    print(
                        f"  {p['peer_id']:32s} addr={p.get('address', '—'):24s} "
                        f"source={p.get('source', '—'):6s} "
                        f"lag={lag_str:8s} "
                        f"fails={p.get('consecutive_failures', 0)}"
                    )
        elif file_path.startswith("add "):
            addr = file_path[4:].strip()
            import urllib.request
            req = urllib.request.Request(
                f"{HTTP_BASE}/api/sync/peers",
                data=json.dumps({"address": addr}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    r = json.loads(resp.read())
                print(f"  Added peer: {r.get('peer_id', addr)}" if r.get("ok") else f"  Add failed: {r.get('error', '?')}")
            except Exception as e:
                print(f"  Add failed: {e}")
        elif file_path.startswith("remove "):
            pid = file_path[7:].strip()
            import urllib.request
            req = urllib.request.Request(
                f"{HTTP_BASE}/api/sync/peers/{pid}", method="DELETE",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    r = json.loads(resp.read())
                print(f"  Removed peer: {pid}" if r.get("ok") else f"  Remove failed: {r.get('error', '?')}")
            except Exception as e:
                print(f"  Remove failed: {e}")
        else:
            print("  Usage: feral sync peers [list | add <host:port> | remove <peer_id>]")
    elif action == "export":
        out = file_path or "feral_memory_export.json"
        data = _http_get("/api/sync/status")
        if data.get("enabled"):
            print(f"  Exporting memory bundle to {out}...")
            import urllib.request
            req = urllib.request.Request(f"{HTTP_BASE}/api/sync/export")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    bundle = resp.read()
                    with open(out, "wb") as f:
                        f.write(bundle)
                    print(f"  Exported to {out}")
            except Exception as e:
                print(f"  Export failed: {e}")
        else:
            print("  Sync engine not running.")
    elif action == "import":
        if not file_path:
            print("  Usage: feral sync import <file.json>")
            return
        print(f"  Importing from {file_path}...")
        try:
            with open(file_path) as f:
                bundle = json.load(f)
            import urllib.request
            req = urllib.request.Request(
                f"{HTTP_BASE}/api/sync/import",
                data=json.dumps(bundle).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                print(f"  Imported {result.get('applied', 0)} operations")
        except Exception as e:
            print(f"  Import failed: {e}")


def _apply_connection_args(args):
    global WS_URL, HTTP_BASE
    host = getattr(args, "host", None) or brain_public_host()
    port = str(getattr(args, "port", None) or brain_public_port())
    http_scheme = "https" if brain_public_scheme() == "https" else "http"
    ws_scheme = "wss" if http_scheme == "https" else "ws"
    origin = f"{host}:{port}"
    WS_URL = f"{ws_scheme}://{origin}/v1/session"
    HTTP_BASE = f"{http_scheme}://{origin}"


def main():
    # ── R2-002 — pure-local fast path ─────────────────────────────────
    # `feral --version` MUST print the version + exit 0 in <100ms with
    # NO network calls. We short-circuit BEFORE the heavy argparse tree
    # is built (registering 30+ subparsers, importing `cli.app_commands`,
    # `cli.key_commands`, etc., easily costs another 50ms). The argparse
    # `--version` action is also registered below for the canonical flag
    # discovery in `feral --help`.
    #
    # Pure-local commands more broadly are listed in
    # ``PURE_LOCAL_SUBCOMMANDS``; the network-touching ones in
    # ``NEEDS_BRAIN_SUBCOMMANDS``. ``tests/test_cli_pure_local.py``
    # pins both timing + no-network-on-pure-local behavior.
    argv = sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        print(f"feral-ai {__version__}")
        return 0

    parser = argparse.ArgumentParser(
        description="FERAL — Open AI agent with computer use, voice, GenUI, and hardware control",
        usage="feral [command] [options]",
    )
    parser.add_argument("--host", default=None, help="Brain hostname")
    parser.add_argument("--port", default=None, help="Brain port")
    # Argparse-driven --version: makes the flag visible in `feral --help`
    # and supports `feral --host x --port y --version` after the global
    # options. The fast-path above handles the bare `feral --version`
    # case without paying the parser-build cost.
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"feral-ai {__version__}",
        help="Print the installed feral-ai package version and exit",
    )

    sub = parser.add_subparsers(dest="subcommand")

    # feral start (THE main command)
    start_p = sub.add_parser("start", help="Start FERAL — installs + starts the brain as a background service")
    start_p.add_argument("--serve-port", default=str(brain_port()), help=f"Port (default {brain_port()})")
    start_p.add_argument("--no-browser", action="store_true", help="Don't open browser (foreground mode only)")
    start_p.add_argument("--tls", action="store_true", help="Enable TLS (auto-generates self-signed cert if needed)")
    start_p.add_argument(
        "--foreground",
        action="store_true",
        help=(
            "Run the brain in this terminal with the interactive REPL "
            "(legacy behaviour). Default is to detach into the launchd / "
            "systemd service so the terminal can be closed."
        ),
    )
    start_p.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)

    # feral stop / status / logs / restart — service lifecycle
    sub.add_parser("stop", help="Stop the FERAL Brain service")
    sub.add_parser("restart", help="Restart the FERAL Brain service (re-renders the plist)")
    sub.add_parser("service-status", help="Show launchd / systemd state for the FERAL Brain service")
    logs_p = sub.add_parser("logs", help="Tail the brain's service log file (Ctrl+C to exit)")
    logs_p.add_argument("--no-follow", action="store_true", help="Print last N lines and exit")
    logs_p.add_argument("--lines", "-n", type=int, default=50, help="Number of lines to show (default 50)")
    logs_p.add_argument("--stderr", action="store_true", help="Tail stderr instead of stdout")

    # feral demo (shortcut for start --demo)
    demo_p = sub.add_parser("demo", help=argparse.SUPPRESS)
    demo_p.add_argument("--scenario", default="", choices=["", "morning", "developer", "mesh"], help="Run a specific demo scenario")
    demo_p.add_argument("--serve-port", default=str(brain_port()), help=f"Port (default {brain_port()})")

    # feral serve (headless server only)
    serve_p = sub.add_parser("serve", help="Start the brain server (headless, no chat)")
    serve_p.add_argument("--bind", default=None, help=f"Bind address (default {brain_bind_host()})")
    serve_p.add_argument("--serve-port", default=str(brain_port()), help=f"Port (default {brain_port()})")
    serve_p.add_argument("--tls", action="store_true", help="Enable TLS (auto-generates self-signed cert if needed)")

    # feral setup — terminal or browser
    setup_p = sub.add_parser(
        "setup", help="Guided setup wizard — configure provider, keys, features",
    )
    setup_mode = setup_p.add_mutually_exclusive_group()
    setup_mode.add_argument(
        "--terminal", action="store_true", dest="setup_terminal",
        help="Stay in the terminal (default when no browser is available).",
    )
    setup_mode.add_argument(
        "--browser", action="store_true", dest="setup_browser",
        help="Open http://localhost:9090/setup in a browser window.",
    )
    # Lane U1 — re-enter one wizard step without deleting the resume
    # sidecar. Operators caught a model typo and want to re-run JUST
    # the model picker (``feral setup --from-step llm_model``) rather
    # than walk through provider / voice / audio / channels again.
    setup_p.add_argument(
        "--from-step", dest="setup_from_step", default="",
        help="Re-enter the wizard at the named step (e.g. llm_model, "
             "audio, identity). Bypasses the resume prompt.",
    )

    # feral doctor
    sub.add_parser("doctor", help="Run diagnostics — check deps, keys, brain health")

    # feral status / devices / skills / identity
    sub.add_parser("status", help="Show system health")
    sub.add_parser("devices", help="List connected hardware")
    sub.add_parser("skills", help="List loaded skills")
    sub.add_parser("identity", help="Show agent identity")

    # feral pair
    pair_p = sub.add_parser("pair", help="Manage per-node device pairing tokens")
    pair_p.add_argument("--name", default="", help="Friendly name for the device")
    pair_p.add_argument("--list", action="store_true", dest="list_devices", help="List paired devices")
    pair_p.add_argument("--revoke", default="", help="Revoke a device by ID")
    pair_p.add_argument(
        "--prune",
        type=int,
        default=-1,
        metavar="SECONDS",
        help="Bulk-revoke unclaimed pair tokens older than N seconds (0 = all)",
    )

    # feral wake-test
    sub.add_parser("wake-test", help="Test wake word detection from your microphone")

    # feral marketplace
    mp = sub.add_parser("marketplace", help="Skill marketplace commands")
    mp.add_argument("action", nargs="?", default="search", choices=["search", "install", "list"], help="Action")
    mp.add_argument("query", nargs="?", default="", help="Search query or item ID")
    mp.add_argument("--registry", default=None, help="Override registry base URL (default: https://registry.feral.sh)")

    # feral install <item_id> — direct registry install
    inst_p = sub.add_parser("install", help="Install a published item from the FERAL registry")
    inst_p.add_argument("item_id", help="Registry item id (from 'feral publish' output)")
    inst_p.add_argument("--registry", default=None, help="Override registry base URL (default: https://registry.feral.sh)")

    # feral publish --skill <dir> | --daemon <dir>
    pub_p = sub.add_parser("publish", help="Publish a skill or daemon bundle to the FERAL registry")
    pub_group = pub_p.add_mutually_exclusive_group(required=True)
    pub_group.add_argument("--skill", dest="skill_dir", default=None, help="Path to a skill directory with manifest.json")
    pub_group.add_argument("--daemon", dest="daemon_dir", default=None, help="Path to a daemon directory with manifest.json")
    pub_p.add_argument("--registry", default=None, help="Override registry base URL (default: https://registry.feral.sh)")

    # feral publisher login|register
    pubr_p = sub.add_parser("publisher", help="Manage FERAL publisher credentials")
    pubr_p.add_argument("action", choices=["login", "register"], help="login | register")
    pubr_p.add_argument("--registry", default=None, help="Override registry base URL (default: https://registry.feral.sh)")

    # feral sync
    sp = sub.add_parser("sync", help="Federated memory sync commands")
    sp.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["status", "peers", "export", "import", "now", "node-id"],
        help=(
            "status: show engine + per-peer health | "
            "peers [list|add <host:port>|remove <peer_id>] | "
            "now [<peer_id>]: trigger immediate sync | "
            "node-id: print persistent HLC id | "
            "export/import <file>: bundle round-trip"
        ),
    )
    sp.add_argument(
        "file",
        nargs="?",
        default="",
        help="File path for export/import, peer_id for `now`, or subcommand for `peers`",
    )

    # feral memory — backend selector + v2026.5.34 decay/forget/recall/compact
    # + Lane 07  `query` (closes THESIS_SCENARIOS S1 from the CLI).
    mem_p = sub.add_parser("memory", help="Memory backend + decay + query management")
    mem_p.add_argument(
        "action",
        choices=[
            "status", "switch", "list", "decay",
            "forget", "recall", "compact", "query", "encrypt",
            "reembed",
        ],
        help=(
            "status: show current backend | list: installed backends | "
            "switch <id>: select backend | "
            "decay now: run a one-shot Ebbinghaus sweep | "
            "forget <episode_id>: mark an episode forgotten | "
            "recall <episode_id>: reverse a forget | "
            "compact [<session_id>]: promote conversation turns to episodes | "
            "query <text>: search memory (THESIS S1) | "
            "encrypt: AEAD-encrypt memory.db at rest (brain must be stopped)"
        ),
    )
    mem_p.add_argument(
        "backend_id",
        nargs="?",
        default=None,
        help=(
            "Positional argument — backend id for `switch` "
            "(sqlite_vec / chroma / qdrant), session id for `compact`, "
            "or query text for `query` (quote multi-word queries)"
        ),
    )
    mem_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="encrypt: overwrite an existing memory.db.enc",
    )
    mem_p.add_argument(
        "--no-shred",
        dest="no_shred",
        action="store_true",
        default=False,
        help="encrypt: retain memory.db.bak.plaintext after verification",
    )

    # feral install-service / uninstall-service
    sub.add_parser("install-service", help="Install FERAL Brain as a system daemon (launchd/systemd)")
    sub.add_parser("uninstall-service", help="Remove the FERAL Brain system daemon")

    # feral twin — manage digital-twin policies + approvals
    twin_p = sub.add_parser("twin", help="Manage the digital twin's per-domain policies + approvals")
    twin_sub = twin_p.add_subparsers(dest="action")
    twin_grant = twin_sub.add_parser("grant", help="Grant / update a twin domain policy")
    twin_grant.add_argument("domain", help="respond_imessage / draft_email / …")
    twin_grant.add_argument("--draft-only", dest="twin_mode_draft", action="store_true")
    twin_grant.add_argument("--auto-send", dest="twin_mode_auto", action="store_true")
    twin_grant.add_argument("--disabled", dest="twin_mode_disabled", action="store_true")
    twin_grant.add_argument("--window", dest="twin_windows", action="append", default=[],
                            help="HH:MM-HH:MM (repeatable)")
    twin_grant.add_argument("--max-per-day", type=int, default=10)
    twin_grant.add_argument("--requires-user-online", action="store_true")
    twin_sub.add_parser("list", help="List every twin policy on this brain")
    twin_revoke = twin_sub.add_parser("revoke", help="Remove a twin policy")
    twin_revoke.add_argument("domain")
    twin_sub.add_parser("pending", help="List pending twin-approval queue rows")

    # feral access — Tailscale Mode C (remote pairing) management
    access_p = sub.add_parser(
        "access",
        help="Manage pairing access mode (LAN / localhost / Tailscale Funnel)",
    )
    access_sub = access_p.add_subparsers(dest="action")
    access_sub.add_parser(
        "status",
        help="Show current pairing mode + Tailscale status",
    )
    access_sub.add_parser(
        "remote-up",
        help="Enable Tailscale Funnel + switch pairing mode to remote",
    )
    access_sub.add_parser(
        "remote-down",
        help="Disable Tailscale Funnel + revert to localhost mode",
    )

    # feral grant — workspace folder grants (Desktop, Documents, project dirs)
    # so computer_use file tools can read/write outside the default sandbox.
    grant_p = sub.add_parser(
        "grant",
        help="Grant or revoke filesystem folder access for computer_use file tools",
    )
    grant_sub = grant_p.add_subparsers(dest="action")
    grant_add = grant_sub.add_parser(
        "add",
        help="Grant a folder (default mode: readwrite)",
    )
    grant_add.add_argument("path", help="Absolute folder path (e.g. ~/Desktop)")
    grant_add.add_argument(
        "--mode",
        choices=("read", "readwrite"),
        default="readwrite",
        help="Access mode (default readwrite)",
    )
    grant_sub.add_parser("list", help="List active workspace grants")
    grant_revoke = grant_sub.add_parser("revoke", help="Revoke a previously granted folder")
    grant_revoke.add_argument("path", help="Absolute folder path to revoke")

    # feral checkpoints: inspect or undo the agent's file writes.
    # Pure-local on purpose: it reads the checkpoint SQLite directly
    # rather than calling the brain's REST route, because the situation
    # you most need an undo in is the one where the brain is wedged.
    cp_p = sub.add_parser(
        "checkpoints",
        help="List or revert the file writes FERAL made, per turn",
    )
    cp_sub = cp_p.add_subparsers(dest="action")
    cp_list = cp_sub.add_parser("list", help="List recent turns that wrote files")
    cp_list.add_argument("--session", default="", help="Filter to one session id")
    cp_list.add_argument("--limit", type=int, default=20, help="Max turns to show")
    cp_show = cp_sub.add_parser("show", help="Show what a revert of a turn would do")
    cp_show.add_argument("turn_id", nargs="?", default="", help="Turn id (default: most recent)")
    cp_revert = cp_sub.add_parser("revert", help="Restore the files a turn wrote")
    cp_revert.add_argument("turn_id", nargs="?", default="", help="Turn id (default: most recent)")
    cp_revert.add_argument(
        "--force", action="store_true",
        help="Also revert files that changed after FERAL wrote them, "
             "discarding those changes. Refused without this flag.",
    )
    cp_revert.add_argument(
        "--dry-run", dest="cp_dry_run", action="store_true",
        help="Report what would happen without touching any file",
    )

    # feral bridge install — wraps scripts/install-phone-bridge.sh
    bridge_p = sub.add_parser("bridge", help="Install the FERAL phone-bridge daemon on this host")
    bridge_sub = bridge_p.add_subparsers(dest="action")
    bridge_install = bridge_sub.add_parser("install", help="Install + start the phone-bridge daemon")
    bridge_install.add_argument("--token", required=True, help="Pairing token from Pair modal > Daemon token")
    bridge_install.add_argument("--brain-url", required=True, help="ws://host:port/v1/node")
    bridge_install.add_argument("--node-id", default="", help="Stable node id (defaults to hostname)")
    bridge_install.add_argument("--prefix", default="", help="Install prefix (default ~/.feral/phone-bridge)")

    # feral app ...
    try:
        from cli.app_commands import register_app_subparser
        register_app_subparser(sub)
    except Exception:
        pass

    # feral key — vault key lifecycle ( + Lane 07  multi-key)
    from cli.key_commands import register_key_subparser
    register_key_subparser(sub)

    # ── Lane 07  — voice + models pure-local subcommands ──
    from cli.voice_commands import register_voice_subparser
    register_voice_subparser(sub)

    from cli.model_commands import register_models_subparser
    register_models_subparser(sub)

    # ── Lane 07  — integrations connect (Gmail / OAuth / HA) ──
    from cli.integration_commands import register_integrations_subparser
    register_integrations_subparser(sub)

    # Parse known args — everything else is treated as a message
    args, remaining = parser.parse_known_args()
    _apply_connection_args(args)

    if args.subcommand == "demo":
        os.environ["FERAL_DEV_DEMO"] = "1"
        if getattr(args, "scenario", ""):
            os.environ["FERAL_DEMO_SCENARIO"] = args.scenario
        cmd_start(port=int(args.serve_port), no_browser=False)
    elif args.subcommand == "start":
        if getattr(args, "demo", False):
            os.environ["FERAL_DEV_DEMO"] = "1"
        cmd_start(
            port=int(args.serve_port),
            no_browser=args.no_browser,
            tls=getattr(args, "tls", False),
            foreground=getattr(args, "foreground", False),
        )
    elif args.subcommand == "stop":
        cmd_stop()
    elif args.subcommand == "restart":
        cmd_restart()
    elif args.subcommand == "service-status":
        cmd_service_status()
    elif args.subcommand == "logs":
        cmd_logs(
            follow=not getattr(args, "no_follow", False),
            n=int(getattr(args, "lines", 50)),
            stderr=getattr(args, "stderr", False),
        )
    elif args.subcommand == "serve":
        cmd_serve(host=args.bind, port=int(args.serve_port), tls=getattr(args, "tls", False))
    elif args.subcommand == "setup":
        cmd_setup(
            browser=getattr(args, "setup_browser", False),
            terminal=getattr(args, "setup_terminal", False),
            from_step=getattr(args, "setup_from_step", "") or "",
        )
    elif args.subcommand == "doctor":
        cmd_doctor()
    elif args.subcommand == "status":
        cmd_status()
    elif args.subcommand == "devices":
        cmd_devices()
    elif args.subcommand == "skills":
        cmd_skills()
    elif args.subcommand == "identity":
        cmd_identity()
    elif args.subcommand == "pair":
        cmd_pair(
            name=getattr(args, "name", ""),
            list_devices=getattr(args, "list_devices", False),
            revoke=getattr(args, "revoke", ""),
            prune=getattr(args, "prune", -1),
        )
    elif args.subcommand == "wake-test":
        cmd_wake_test()
    elif args.subcommand == "marketplace":
        cmd_marketplace(args.action, args.query, registry=getattr(args, "registry", None))
    elif args.subcommand == "install":
        from cli.install import cmd_install
        cmd_install(args.item_id, registry=getattr(args, "registry", None))
    elif args.subcommand == "publish":
        from cli.publish import cmd_publish
        cmd_publish(
            skill_dir=getattr(args, "skill_dir", None),
            daemon_dir=getattr(args, "daemon_dir", None),
            registry=getattr(args, "registry", None),
        )
    elif args.subcommand == "publisher":
        from cli.publish import cmd_publisher_login, cmd_publisher_register
        if args.action == "login":
            cmd_publisher_login(registry=getattr(args, "registry", None))
        else:
            cmd_publisher_register(registry=getattr(args, "registry", None))
    elif args.subcommand == "sync":
        cmd_sync(args.action, getattr(args, "file", ""))
    elif args.subcommand == "memory":
        from cli.memory_cmd import cmd_memory
        cmd_memory(args.action, getattr(args, "backend_id", None), flags=args)
    elif args.subcommand == "install-service":
        from cli.daemon import install_service
        install_service()
    elif args.subcommand == "uninstall-service":
        from cli.daemon import uninstall_service
        uninstall_service()
    elif args.subcommand == "app":
        from cli.app_commands import dispatch_app_subcommand
        dispatch_app_subcommand(args)
    elif args.subcommand == "bridge":
        from cli.bridge_commands import cmd_bridge
        cmd_bridge(args)
    elif args.subcommand == "access":
        from cli.access_commands import cmd_access
        sys.exit(cmd_access(args))
    elif args.subcommand == "twin":
        from cli.twin_commands import cmd_twin
        cmd_twin(args)
    elif args.subcommand == "grant":
        from cli.grant_commands import cmd_grant
        sys.exit(cmd_grant(args))
    elif args.subcommand == "checkpoints":
        sys.exit(cmd_checkpoints(args))
    elif args.subcommand == "key":
        from cli.key_commands import dispatch_key_subcommand
        sys.exit(dispatch_key_subcommand(args))
    elif args.subcommand == "voice":
        from cli.voice_commands import dispatch_voice_subcommand
        sys.exit(dispatch_voice_subcommand(args))
    elif args.subcommand == "models":
        from cli.model_commands import dispatch_models_subcommand
        sys.exit(dispatch_models_subcommand(args))
    elif args.subcommand == "integrations":
        from cli.integration_commands import dispatch_integrations_subcommand
        sys.exit(dispatch_integrations_subcommand(args))
    elif args.subcommand is None and not remaining:
        asyncio.run(repl())
    elif args.subcommand is None and remaining:
        # R2-002: never silently route unknown flags to the brain. The
        # pre-Lane-07 fall-through joined `remaining` and pushed it to
        # ``one_shot()`` which then opened a WebSocket and printed
        # ``Cannot connect to FERAL Brain at ws://...`` — confusing and
        # wrong for typos like ``feral --verison``. Bare-word remainders
        # are still chat (``feral search the web``); flag-shaped
        # remainders raise a parser error.
        if remaining[0].startswith("-"):
            parser.error(f"unrecognized arguments: {' '.join(remaining)}")
        full_text = " ".join(remaining).strip()
        asyncio.run(one_shot(full_text))
    else:
        full_text = " ".join([args.subcommand or ""] + remaining).strip()
        if full_text:
            asyncio.run(one_shot(full_text))
        else:
            asyncio.run(repl())


if __name__ == "__main__":
    main()
