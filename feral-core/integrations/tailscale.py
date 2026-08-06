"""Tailscale CLI integration for FERAL Mode C (remote pairing).

Provides a thin wrapper around the ``tailscale`` CLI so the brain can:

  * detect whether Tailscale is installed and the daemon is reachable
  * read the local node's tailnet name + IPs (``tailscale status --json``)
  * enable Tailscale Funnel on the brain port (``tailscale funnel <port> on``)
  * disable Funnel (``tailscale funnel <port> off``)

This is the implementation of the ``feral access remote-up`` flow
described in ``A4-pairing-redesign.md`` §2 Mode C onboarding step.

Honesty
-------
1. We only support the userspace-networking socket layout when the
   binary path *isn't* on the standard daemon socket. Tailscale's
   userspace mode runs at ``/tmp/tailscaled-userspace.sock`` (the
   pattern used by the FERAL desktop wrapper today). If both sockets
   are missing, the integration returns ``not_running``.
2. Funnel must be enabled in the operator's tailnet at
   https://login.tailscale.com/admin/settings/features. If it isn't,
   ``funnel_enable`` returns a structured error with the remediation
   URL — we do not try to silently configure ACLs.
3. We do not attempt to install Tailscale automatically. We detect
   absence and surface the install URL; the operator runs
   ``brew install --cask tailscale`` (macOS) or
   ``curl -fsSL https://tailscale.com/install.sh | sh`` (Linux) and
   re-runs the command.

All error paths raise the typed exceptions in this module so callers
(REST routes + CLI) can map them to status codes / exit codes
respectively.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("feral.integrations.tailscale")


# ── Exceptions ────────────────────────────────────────────────────


class TailscaleError(Exception):
    """Base class for Tailscale integration failures."""


class TailscaleNotInstalled(TailscaleError):
    """``tailscale`` binary not on PATH."""


class TailscaleDaemonUnreachable(TailscaleError):
    """``tailscaled`` not running or socket missing."""


class TailscaleNotLoggedIn(TailscaleError):
    """Tailscale daemon is up but the operator hasn't authenticated."""


class TailscaleFunnelDisabledInTailnet(TailscaleError):
    """Funnel is not enabled in the operator's tailnet ACLs.

    The remediation is at the URL in the message.
    """


class TailscalePermissionDenied(TailscaleError):
    """The user can't run ``tailscale funnel`` without elevated rights.

    On Linux this typically means the user isn't in the ``tailscale``
    group. The remediation is in the message.
    """


class TailscaleSubprocessFailure(TailscaleError):
    """The CLI returned a non-zero exit and the error wasn't classified
    above. Carries stdout + stderr for the operator to inspect."""


class TailscaleNoVarRootInUserspace(TailscaleError):
    """Daemon runs in single-file ``--state=<file>`` mode → no cert
    storage path → Funnel TLS can't provision Let's Encrypt certs.

    The fix is :func:`migrate_userspace_to_statedir`, which moves the
    state file into a directory and restarts tailscaled with
    ``--statedir=<dir>``.
    """


class TailscaleMigrationFailed(TailscaleError):
    """``migrate_userspace_to_statedir`` could not complete safely.

    The caller is expected to surface this verbatim — the operator
    needs to know if their tailscaled state was preserved or whether
    they have to re-authenticate. The exception message always
    documents the recovery state.
    """


# ── Status data class ─────────────────────────────────────────────


@dataclass(frozen=True)
class TailscaleStatus:
    installed: bool
    running: bool
    logged_in: bool
    dns_name: str = ""
    ipv4: str = ""
    ipv6: str = ""
    tailnet_name: str = ""
    funnel_url: str = ""
    funnel_active: bool = False
    error: str = ""


# ── Internals ─────────────────────────────────────────────────────


def _socket_arg() -> list[str]:
    """If userspace tailscaled is running with the FERAL desktop's
    socket, prefer that socket; otherwise rely on the system default."""
    candidate = "/tmp/tailscaled-userspace.sock"
    if os.path.exists(candidate):
        return ["--socket", candidate]
    return []


def _run(args: list[str], *, timeout: float = 8.0) -> subprocess.CompletedProcess:
    """Run a tailscale CLI command and return the completed process.

    Does not raise on non-zero exit — callers classify the failure.
    """
    if not shutil.which("tailscale"):
        raise TailscaleNotInstalled(
            "`tailscale` binary not on PATH. Install with "
            "`brew install --cask tailscale` (macOS) or "
            "`curl -fsSL https://tailscale.com/install.sh | sh` (Linux), "
            "then re-run."
        )
    cmd = ["tailscale", *_socket_arg(), *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            # Closed, not inherited. `tailscale funnel` prompts for
            # confirmation when Funnel is not yet enabled on the tailnet:
            # it prints an enable URL and waits. With stdin inherited
            # from a daemon or an API request there is nothing to read,
            # so it blocked until the timeout and the operator was told
            # only that something "timed out". With stdin closed the CLI
            # gets EOF and exits, and its message reaches us.
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise TailscaleNotInstalled(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired carries whatever the process wrote before we
        # gave up. Discarding it is how a user with Tailscale installed,
        # running and logged in was told "timed out after 20.0s" while
        # the actual answer, usually an enable-Funnel URL, sat unread in
        # this exception.
        partial_out = _as_text(exc.stdout)
        partial_err = _as_text(exc.stderr)
        classified = _classify_stderr(partial_err) or _classify_stderr(partial_out)
        if classified is not None:
            raise classified from exc
        detail = (partial_err or partial_out).strip()
        raise TailscaleSubprocessFailure(
            f"tailscale {' '.join(args)} timed out after {timeout}s"
            + (f", and said: {detail[:400]}" if detail else
               ". It produced no output, which usually means it was waiting "
               "on input that never came.")
        ) from exc


def _as_text(raw) -> str:
    """Decode subprocess output that may be bytes or str, never raising."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _classify_stderr(stderr: str) -> Optional[TailscaleError]:
    """Map common stderr patterns to typed exceptions."""
    s = (stderr or "").strip()
    if not s:
        return None
    low = s.lower()
    if (
        "no such file or directory" in low
        and ("tailscaled.sock" in low or "/var/run/tailscale" in low or "tailscaled" in low)
    ):
        return TailscaleDaemonUnreachable(s)
    if "logged out" in low or "not logged in" in low or "not authenticated" in low:
        return TailscaleNotLoggedIn(s)
    # Tailscale 1.66+ emits this exact phrasing with the per-node enable URL:
    #     "Funnel is not enabled on your tailnet.
    #      To enable, visit: https://login.tailscale.com/f/funnel?node=…"
    # We surface the per-node URL when present (it's a one-click enable
    # specific to this tailnet); otherwise we fall back to the admin
    # settings page.
    if "funnel is not enabled" in low or (
        "funnel" in low
        and ("disable" in low or "not enabled" in low or "not allowed" in low)
    ):
        # Try to extract the activation URL the CLI printed.
        import re
        m = re.search(r"https://login\.tailscale\.com/f/funnel\?node=\S+", s)
        if m:
            return TailscaleFunnelDisabledInTailnet(
                "Funnel is not enabled on your tailnet. "
                f"Click this one-time enable link: {m.group(0)} "
                "(it's free; takes 5 seconds), then retry."
            )
        return TailscaleFunnelDisabledInTailnet(
            "Funnel is not enabled on your tailnet. "
            "Run `tailscale funnel <port>` once interactively to get the "
            "per-tailnet enable URL, or visit "
            "https://login.tailscale.com/admin/settings/features and "
            "turn on Funnel."
        )
    if "permission denied" in low or "must be run as root" in low or (
        "not in" in low and "group" in low
    ):
        return TailscalePermissionDenied(s)
    return None


# ── Public API ────────────────────────────────────────────────────


def is_installed() -> bool:
    return shutil.which("tailscale") is not None


def status_json() -> dict:
    """Return ``tailscale status --json`` parsed.

    Raises ``TailscaleNotInstalled`` if the binary is absent and
    ``TailscaleDaemonUnreachable`` if the daemon socket is missing.
    """
    proc = _run(["status", "--json"])
    if proc.returncode != 0:
        classified = _classify_stderr(proc.stderr)
        if classified is not None:
            raise classified
        raise TailscaleSubprocessFailure(
            f"tailscale status --json failed: rc={proc.returncode} "
            f"stderr={proc.stderr.strip()[:300]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TailscaleSubprocessFailure(
            f"tailscale status --json returned non-JSON: {exc}"
        ) from exc


def status() -> TailscaleStatus:
    """Idempotent status snapshot used by ``feral access status`` and
    by the dashboard's Settings → Access panel.

    Returns a ``TailscaleStatus`` with ``error`` populated when the
    underlying state is partial (not-installed / not-running / not-
    logged-in). Never raises — callers can render the panel even when
    Tailscale is broken.
    """
    if not is_installed():
        return TailscaleStatus(
            installed=False,
            running=False,
            logged_in=False,
            error="tailscale_not_installed",
        )
    try:
        data = status_json()
    except TailscaleDaemonUnreachable:
        return TailscaleStatus(
            installed=True, running=False, logged_in=False,
            error="daemon_unreachable",
        )
    except TailscaleNotLoggedIn:
        return TailscaleStatus(
            installed=True, running=True, logged_in=False,
            error="not_logged_in",
        )
    except TailscaleError as exc:
        return TailscaleStatus(
            installed=True, running=False, logged_in=False,
            error=f"unknown:{exc.__class__.__name__}",
        )

    self_node = data.get("Self") or {}
    dns_name = (self_node.get("DNSName") or "").rstrip(".")
    ips = self_node.get("TailscaleIPs") or []
    ipv4 = next((ip for ip in ips if ":" not in ip), "")
    ipv6 = next((ip for ip in ips if ":" in ip), "")
    # CurrentTailnet is sometimes missing when only one tailnet is in
    # play; fall back to the last 2 dot-segments of the DNSName.
    tailnet_name = (data.get("CurrentTailnet") or {}).get("Name", "") or (
        ".".join(dns_name.split(".")[-3:-1]) if dns_name else ""
    )

    return TailscaleStatus(
        installed=True,
        running=True,
        logged_in=True,
        dns_name=dns_name,
        ipv4=ipv4,
        ipv6=ipv6,
        tailnet_name=tailnet_name,
    )


def funnel_url(port: int, *, dns_name: Optional[str] = None) -> str:
    """Compose the public Funnel URL for the given brain port.

    Tailscale Funnel terminates HTTPS at the tailnet edge and forwards
    to the local port. The URL is always ``https://<dns-name>``
    (Funnel doesn't advertise non-default ports — incoming requests
    on 443 forward to whatever ``tailscale funnel`` was last
    configured for).
    """
    if dns_name is None:
        snap = status()
        dns_name = snap.dns_name
    if not dns_name:
        return ""
    return f"https://{dns_name}"


def funnel_enable(port: int, *, auto_migrate: bool = True) -> dict:
    """Enable Tailscale Funnel for ``port`` (background, HTTPS on 443).

    Tailscale 1.66+ replaced the old ``funnel <port> on`` UX with
    ``funnel --bg <port>`` (background) and ``funnel reset`` (off).
    We try the modern form first and fall back to the legacy form
    for very old daemons (<1.50). If both fail with a classified
    error (e.g. Funnel-not-enabled-in-tailnet), we re-raise so the
    caller surfaces a remediation URL.

    ``auto_migrate=True`` (default): when the running tailscaled is in
    single-file ``--state=<file>`` mode, certificate provisioning
    cannot work (no TailscaleVarRoot). Before enabling Funnel we
    detect this and migrate the daemon to ``--statedir=<dir>`` mode
    in-place (preserves auth). Set ``auto_migrate=False`` to skip
    this and surface :class:`TailscaleNoVarRootInUserspace` instead.

    Returns ``{enabled: bool, url: str, port: int, migrated?: dict}``.
    """
    if port <= 0 or port > 65535:
        raise ValueError(f"port must be 1..65535 (got {port})")

    # Pre-flight: detect the userspace single-file state mode that
    # would silently break TLS handshakes after Funnel goes "on".
    # We deliberately fail FAST here rather than after Funnel starts
    # accepting traffic, because partial state (Funnel on, certs
    # missing) is the worst UX — phones see ERR_SSL_PROTOCOL_ERROR.
    info = inspect_tailscaled_process()
    migration_record: Optional[dict] = None
    if info.needs_migration:
        if not auto_migrate:
            raise TailscaleNoVarRootInUserspace(
                "tailscaled is running in single-file state mode "
                f"(--state={info.state_file}); Funnel cert provisioning "
                "would fail. Re-run with auto_migrate=True or migrate "
                "manually with `migrate_userspace_to_statedir()`."
            )
        logger.info(
            "tailscale: pre-Funnel migration triggered "
            "(userspace state-file mode detected)"
        )
        migration_record = migrate_userspace_to_statedir(info=info)

    # Modern syntax (1.66+): `tailscale funnel --bg <port>`. The
    # `--bg` flag returns immediately after persisting the serve
    # config; without it the CLI blocks foreground forever (which is
    # what trapped the live test).
    proc = _run(["funnel", "--bg", str(port)], timeout=20.0)
    if proc.returncode != 0:
        classified = _classify_stderr(proc.stderr)
        if classified is not None:
            raise classified
        # Legacy fallback for <1.50 daemons that still accept
        # `funnel <port> on`. Newer CLIs reject this syntax with a
        # message about "the CLI for serve and funnel has changed",
        # which we'll classify back to a clean error.
        proc2 = _run(["funnel", str(port), "on"], timeout=15.0)
        if proc2.returncode != 0:
            classified2 = _classify_stderr(proc2.stderr)
            if classified2 is not None:
                raise classified2
            raise TailscaleSubprocessFailure(
                f"tailscale funnel could not be enabled on port {port}.\n"
                f"  modern syntax (`funnel --bg {port}`) stderr: "
                f"{proc.stderr.strip()[:300]}\n"
                f"  legacy syntax (`funnel {port} on`) stderr: "
                f"{proc2.stderr.strip()[:300]}\n"
                f"Run `tailscale funnel --help` to inspect your CLI's "
                f"current syntax."
            )

    snap = status()
    result = {
        "enabled": True,
        "url": funnel_url(port, dns_name=snap.dns_name),
        "port": port,
        "tailnet": snap.tailnet_name,
        "dns_name": snap.dns_name,
    }
    if migration_record is not None:
        result["migrated"] = migration_record
    return result


def funnel_disable(port: int) -> dict:
    """Disable Funnel.

    Tailscale 1.66+ uses ``funnel reset`` (clears ALL forwards) — there
    is no per-port off in the modern CLI. We accept ``port`` as an
    argument for symmetry with ``funnel_enable`` but it isn't passed
    through. For older daemons we fall back to ``funnel <port> off``.

    Idempotent: running this when Funnel is already off is a no-op.
    """
    if port <= 0 or port > 65535:
        raise ValueError(f"port must be 1..65535 (got {port})")

    proc = _run(["funnel", "reset"], timeout=10.0)
    if proc.returncode == 0:
        return {"enabled": False, "port": port}

    # Modern reset failed — try legacy "off" form for ancient daemons.
    classified = _classify_stderr(proc.stderr)
    proc2 = _run(["funnel", str(port), "off"], timeout=10.0)
    if proc2.returncode == 0:
        return {"enabled": False, "port": port}

    # Both failed. If either looks like "Funnel was already off" (no
    # serve config), treat as success — disabling something that's
    # already disabled is the desired post-condition.
    combined = (proc.stderr + " " + proc2.stderr).lower()
    if (
        "no serve config" in combined
        or ("no" in combined and "funnel" in combined and "active" in combined)
    ):
        return {"enabled": False, "port": port}

    if classified is not None:
        raise classified
    classified2 = _classify_stderr(proc2.stderr)
    if classified2 is not None:
        raise classified2
    raise TailscaleSubprocessFailure(
        f"tailscale funnel could not be disabled.\n"
        f"  modern syntax (`funnel reset`) stderr: "
        f"{proc.stderr.strip()[:300]}\n"
        f"  legacy syntax (`funnel {port} off`) stderr: "
        f"{proc2.stderr.strip()[:300]}"
    )


def funnel_status() -> dict:
    """Read ``tailscale funnel status --json`` and parse active forwards.

    Returns ``{"active": bool, "ports": [int, ...]}``.

    Tailscale 1.66+ supports ``--json`` so we don't have to parse the
    prose output (which has changed shape several times). The JSON
    shape is::

        {
          "TCP": {"443": {"HTTPS": true}},
          "Web": {"<host>:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9090"}}}},
          "AllowFunnel": {"<host>:443": true}
        }

    We extract the local proxy port from the ``Web.<host>.Handlers``
    map. Empty ``{}`` JSON means "no serve config" → not active.
    """
    proc = _run(["funnel", "status", "--json"])
    if proc.returncode != 0:
        classified = _classify_stderr(proc.stderr)
        if classified is not None:
            raise classified
        return {"active": False, "ports": []}
    out = (proc.stdout or "").strip()
    if not out:
        return {"active": False, "ports": []}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"active": False, "ports": []}
    if not data:
        return {"active": False, "ports": []}

    ports: set[int] = set()
    web = data.get("Web") or {}
    for _host, conf in web.items():
        handlers = (conf or {}).get("Handlers") or {}
        for _path, h in handlers.items():
            proxy = (h or {}).get("Proxy") or ""
            # Proxy looks like "http://127.0.0.1:9090" or
            # "http+insecure://localhost:8443".
            if ":" in proxy:
                tail = proxy.rsplit(":", 1)[-1].split("/")[0]
                digits = "".join(ch for ch in tail if ch.isdigit())
                if digits:
                    try:
                        ports.add(int(digits))
                    except ValueError:
                        pass
    return {"active": bool(ports), "ports": sorted(ports)}


# ── Userspace-tailscaled migration (cert storage gap) ────────────


@dataclass(frozen=True)
class TailscaledProcessInfo:
    """What we know about the currently-running tailscaled.

    Used to detect the "userspace-with-only-a-state-file" pattern that
    Funnel cert provisioning trips on (``no TailscaleVarRoot``). When
    ``is_userspace`` is True AND ``state_dir`` is None AND ``state_file``
    is set, the daemon needs migration to ``--statedir`` mode.
    """
    running: bool
    pid: int = 0
    binary: str = ""
    args: tuple[str, ...] = ()
    socket_path: str = ""
    state_file: str = ""        # value of --state=
    state_dir: str = ""         # value of --statedir=
    tun_mode: str = ""          # value of --tun=
    parent_pid: int = 0

    @property
    def is_userspace(self) -> bool:
        return self.tun_mode == "userspace-networking"

    @property
    def needs_migration(self) -> bool:
        """True iff this process WILL fail Funnel cert provisioning."""
        return (
            self.running
            and self.is_userspace
            and bool(self.state_file)
            and not self.state_dir
        )


def inspect_tailscaled_process() -> TailscaledProcessInfo:
    """Locate the running ``tailscaled`` and return its launch flags.

    Reads ``ps -axo pid,ppid,command`` and picks the first row whose
    command starts with ``tailscaled`` (or ends with that basename).
    Returns ``running=False`` when no such process exists.
    """
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return TailscaledProcessInfo(running=False)

    target_pid = 0
    target_ppid = 0
    target_args: list[str] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid_i = int(parts[0])
            ppid_i = int(parts[1])
        except ValueError:
            continue
        cmd = parts[2]
        # The first token is the binary path; we accept either a path
        # ending in /tailscaled or the bare name.
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            continue
        if not tokens:
            continue
        binary = tokens[0]
        basename = os.path.basename(binary)
        if basename != "tailscaled":
            continue
        target_pid = pid_i
        target_ppid = ppid_i
        target_args = tokens
        break

    if not target_args:
        return TailscaledProcessInfo(running=False)

    state_file = ""
    state_dir = ""
    socket_path = ""
    tun_mode = ""
    for arg in target_args[1:]:
        if arg.startswith("--state="):
            state_file = arg[len("--state="):]
        elif arg.startswith("--statedir="):
            state_dir = arg[len("--statedir="):]
        elif arg.startswith("--socket="):
            socket_path = arg[len("--socket="):]
        elif arg.startswith("--tun="):
            tun_mode = arg[len("--tun="):]

    return TailscaledProcessInfo(
        running=True,
        pid=target_pid,
        binary=target_args[0],
        args=tuple(target_args),
        socket_path=socket_path,
        state_file=state_file,
        state_dir=state_dir,
        tun_mode=tun_mode,
        parent_pid=target_ppid,
    )


def _wait_for_socket_gone(path: str, *, timeout: float = 10.0) -> bool:
    """Wait until ``path`` no longer exists. Returns True on success."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.exists(path):
            return True
        time.sleep(0.2)
    return not os.path.exists(path)


def _wait_for_socket_present(path: str, *, timeout: float = 15.0) -> bool:
    """Wait until ``path`` exists and is a socket. Returns True on success."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.2)
    return os.path.exists(path)


def _extract_listener_ports(args: tuple[str, ...]) -> list[int]:
    """Pull localhost listener ports from tailscaled launch flags.

    The userspace daemon binds extra TCP listeners that DON'T live in
    the unix socket (SOCKS5 + HTTP proxy). After SIGTERM these ports
    take a few seconds to release from TIME_WAIT — if we restart the
    daemon before then, it crashes with::

        proxy listener: listen tcp 127.0.0.1:1055: bind: address already in use

    This helper extracts those ports so the restart loop can wait
    for them to be reclaimable. Recognised flags:
      --socks5-server=<host>:<port>
      --outbound-http-proxy-listen=<host>:<port>
    """
    ports: list[int] = []
    for arg in args:
        for prefix in (
            "--socks5-server=",
            "--outbound-http-proxy-listen=",
        ):
            if arg.startswith(prefix):
                tail = arg[len(prefix):]
                if ":" in tail:
                    port_str = tail.rsplit(":", 1)[-1]
                    try:
                        ports.append(int(port_str))
                    except ValueError:
                        pass
    return ports


def _wait_for_tcp_port_free(port: int, *, timeout: float = 10.0) -> bool:
    """Block until ``localhost:<port>`` can be bound by a fresh listener.

    Returns True when the port is reclaimable, False on timeout. We
    test this the way tailscaled itself will: open a SOCK_STREAM,
    set SO_REUSEADDR, bind to 127.0.0.1:port. If bind succeeds the
    port is genuinely free (not in TIME_WAIT).
    """
    import socket as _socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
        except OSError:
            time.sleep(0.25)
            continue
        finally:
            s.close()
        return True
    return False


def migrate_userspace_to_statedir(
    *,
    info: Optional[TailscaledProcessInfo] = None,
    dry_run: bool = False,
) -> dict:
    """Migrate a userspace tailscaled from ``--state=<file>`` to
    ``--statedir=<dir>`` mode IN-PLACE, preserving auth.

    Without this, Funnel cert provisioning fails with::

        500 Internal Server Error: no TailscaleVarRoot

    because tailscaled has no directory to persist Let's Encrypt
    certs into. The migration:

      1. Inspects the running tailscaled (must be userspace + state-file mode).
      2. Computes a sibling directory next to the state file:
         ``<state_dir>/tailscaled.d/`` is the new home.
      3. Sends SIGTERM to the daemon, waits up to 10s for the socket
         to disappear (then SIGKILL as last resort).
      4. ``mv <state_file> <new_dir>/tailscaled.state`` — Tailscale's
         expected filename inside a statedir.
      5. Restarts tailscaled with the same flags BUT replaces
         ``--state=<file>`` with ``--statedir=<new_dir>`` and detaches
         (Popen, no parent dependency, stdout/stderr to a log file).
      6. Waits up to 15s for the socket to come back.
      7. Verifies ``tailscale status`` reports logged_in (auth must
         have survived the move; if not, raises).

    On any failure the function raises :class:`TailscaleMigrationFailed`
    with a message documenting the current state of the user's data
    so they can recover manually.

    ``dry_run=True`` returns the planned commands without executing.
    """
    if info is None:
        info = inspect_tailscaled_process()
    if not info.running:
        raise TailscaleMigrationFailed(
            "No running tailscaled to migrate. State preserved (no changes made)."
        )
    if not info.needs_migration:
        return {
            "migrated": False,
            "reason": "already_in_statedir_mode_or_not_userspace",
            "info": {
                "is_userspace": info.is_userspace,
                "state_dir": info.state_dir,
                "state_file": info.state_file,
            },
        }

    state_path = pathlib.Path(info.state_file)
    if not state_path.exists():
        raise TailscaleMigrationFailed(
            f"--state path {state_path} doesn't exist on disk; refusing to "
            "migrate (state preserved, no changes made)."
        )

    new_dir = state_path.parent / "tailscaled.d"
    new_state_file = new_dir / "tailscaled.state"

    # Plan the new ProgramArguments. Replace --state with --statedir.
    new_args: list[str] = [info.binary]
    for arg in info.args[1:]:
        if arg.startswith("--state="):
            continue  # replaced below
        new_args.append(arg)
    new_args.append(f"--statedir={new_dir}")

    plan = {
        "stop_pid": info.pid,
        "socket_path": info.socket_path,
        "old_state_file": str(state_path),
        "new_state_dir": str(new_dir),
        "new_state_file": str(new_state_file),
        "restart_argv": new_args,
    }
    if dry_run:
        plan["migrated"] = False
        plan["dry_run"] = True
        return plan

    # ── Step 1: stop the daemon ──────────────────────────────
    logger.info(
        "tailscale: migrating userspace daemon (pid=%s) to statedir mode "
        "(state_file=%s → state_dir=%s)",
        info.pid, state_path, new_dir,
    )
    try:
        os.kill(info.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise TailscaleMigrationFailed(
            f"cannot signal pid {info.pid}: {exc}. "
            "Is tailscaled running as a different user? State preserved "
            "(no changes made)."
        ) from exc

    socket_path = info.socket_path or "/var/run/tailscale/tailscaled.sock"
    if not _wait_for_socket_gone(socket_path, timeout=10.0):
        # Escalate to SIGKILL.
        try:
            os.kill(info.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not _wait_for_socket_gone(socket_path, timeout=5.0):
            raise TailscaleMigrationFailed(
                f"tailscaled (pid {info.pid}) didn't release socket {socket_path} "
                f"after SIGKILL. State preserved at {state_path}; restart your "
                "Mac to clear the stale daemon, then retry."
            )

    # Beyond the unix socket, the userspace daemon binds TCP listeners
    # (SOCKS5 + HTTP proxy on localhost:1055 by default). The unix
    # socket is closed instantly on process exit, but TCP ports linger
    # in TIME_WAIT for a few seconds. Restarting too early crashes
    # the new daemon with `bind: address already in use`. We poll
    # each listener port until it can be re-bound (matches what the
    # restarted daemon will do).
    listener_ports = _extract_listener_ports(info.args)
    for p in listener_ports:
        if not _wait_for_tcp_port_free(p, timeout=15.0):
            raise TailscaleMigrationFailed(
                f"tailscaled (pid {info.pid}) is gone but localhost:{p} "
                f"is still bound after 15s (TIME_WAIT). State preserved "
                f"at {state_path}; wait ~30s and retry, or run "
                f"`lsof -i:{p}` to see the holder."
            )

    # ── Step 2: prepare new directory + move state ───────────
    try:
        new_dir.mkdir(parents=True, exist_ok=True)
        # Permissions match what tailscaled itself uses.
        os.chmod(new_dir, 0o700)
    except OSError as exc:
        raise TailscaleMigrationFailed(
            f"could not create statedir {new_dir}: {exc}. "
            f"State preserved at {state_path}; daemon stopped. "
            "Re-run when the directory is writable."
        ) from exc

    # If a previous failed migration left a partial file, back it up.
    if new_state_file.exists():
        backup = new_dir / f"tailscaled.state.bak.{int(time.time())}"
        try:
            new_state_file.rename(backup)
        except OSError:
            pass

    try:
        shutil.move(str(state_path), str(new_state_file))
    except OSError as exc:
        raise TailscaleMigrationFailed(
            f"could not move {state_path} → {new_state_file}: {exc}. "
            f"State preserved at {state_path}; daemon stopped. "
            "Restart tailscaled manually with the original args to recover."
        ) from exc

    # ── Step 3: restart with --statedir ──────────────────────
    log_path = new_dir / "tailscaled.log"
    try:
        log_fh = open(log_path, "ab", buffering=0)
        # Detached child; if our process exits it lives on under launchd.
        subprocess.Popen(
            new_args,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise TailscaleMigrationFailed(
            f"could not restart tailscaled with --statedir: {exc}. "
            f"State has been moved to {new_state_file}; restart manually with: "
            f"{shlex.join(new_args)}"
        ) from exc

    # ── Step 4: wait for new socket + verify auth ────────────
    if not _wait_for_socket_present(socket_path, timeout=15.0):
        raise TailscaleMigrationFailed(
            f"tailscaled didn't reopen socket {socket_path} within 15s. "
            f"State at {new_state_file}; check log at {log_path}."
        )

    # Status probe — verify auth survived. Allow up to 5s for the
    # daemon to finish initialising before we give up.
    deadline = time.time() + 5.0
    snap = None
    while time.time() < deadline:
        try:
            snap = status()
            if snap.logged_in:
                break
        except TailscaleError:
            pass
        time.sleep(0.5)

    if snap is None or not snap.logged_in:
        raise TailscaleMigrationFailed(
            f"tailscaled restarted but is not logged in. "
            f"State at {new_state_file}; you may need to run "
            f"`tailscale --socket={socket_path} login` to re-authenticate. "
            f"See log at {log_path}."
        )

    logger.info(
        "tailscale: migration complete; new statedir=%s, dns_name=%s",
        new_dir, snap.dns_name,
    )
    return {
        "migrated": True,
        "old_state_file": str(state_path),
        "new_state_dir": str(new_dir),
        "new_state_file": str(new_state_file),
        "restart_argv": new_args,
        "log": str(log_path),
        "post_migration_status": {
            "logged_in": snap.logged_in,
            "dns_name": snap.dns_name,
        },
    }


__all__ = [
    "TailscaleError",
    "TailscaleNotInstalled",
    "TailscaleDaemonUnreachable",
    "TailscaleNotLoggedIn",
    "TailscaleFunnelDisabledInTailnet",
    "TailscalePermissionDenied",
    "TailscaleSubprocessFailure",
    "TailscaleNoVarRootInUserspace",
    "TailscaleMigrationFailed",
    "TailscaleStatus",
    "TailscaledProcessInfo",
    "is_installed",
    "status",
    "status_json",
    "funnel_enable",
    "funnel_disable",
    "funnel_status",
    "funnel_url",
    "inspect_tailscaled_process",
    "migrate_userspace_to_statedir",
]
