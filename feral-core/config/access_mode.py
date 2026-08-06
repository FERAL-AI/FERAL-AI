"""Access mode: the single source of truth for how a brain is reachable.

Before this module, ``access.pairing_mode`` and ``network.bind_host`` were
two independent settings that had to agree, and almost nothing kept them
in step. Only ``cli/setup/network.py`` wrote both. The web Settings
button, the web Setup card, ``POST /api/access/remote-up`` and
``feral access remote-up`` all wrote the mode alone and left the bind
host wherever it happened to be.

The reachable failure was concrete: clicking "Same WiFi" in the web UI
persisted ``pairing_mode: local`` while the brain stayed bound to
``127.0.0.1``. The pair URL then advertised ``http://<lan-ip>:9090``,
which nothing was listening on, and the phone spun forever. The UI
reported success.

So the bind host is no longer a thing anyone types. It is *derived* from
the mode, here, and :func:`apply_mode` is the only writer. A caller picks
an intent ("I want to pair over the LAN") and the derivation makes the
contradictory combination unrepresentable.

Persisted values are unchanged for the three legacy modes so existing
``settings.json`` files keep working without migration.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("feral.config.access_mode")

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost", "")


class AccessMode(Enum):
    """How the brain expects to be reached.

    The value is what lands in ``settings.json`` under
    ``access.pairing_mode``. ``LOCALHOST``/``LAN``/``TAILSCALE`` keep
    their historical strings ("localhost"/"local"/"remote") so this is a
    pure refactor for installs that already exist.
    """

    LOCALHOST = "localhost"
    LAN = "local"
    RELAY = "relay"
    TAILSCALE = "remote"

    @property
    def bind_host(self) -> str:
        """The only bind host that makes sense for this mode.

        ``RELAY`` binds loopback deliberately. The tunnel is an outbound
        connection and the relay hands us raw TLS on a local socket, so a
        relay-mode brain never needs an inbound listener on the network.
        That is strictly less exposure than the Tailscale path it
        replaces, not more.
        """
        return "0.0.0.0" if self is AccessMode.LAN else "127.0.0.1"

    @property
    def exposes_pairing(self) -> bool:
        """Whether a phone can be handed a working pair URL in this mode."""
        return self is not AccessMode.LOCALHOST

    @property
    def label(self) -> str:
        return {
            AccessMode.LOCALHOST: "This computer only",
            AccessMode.LAN: "Same WiFi",
            AccessMode.RELAY: "Any network",
            AccessMode.TAILSCALE: "Tailscale Funnel (advanced)",
        }[self]


def coerce(value: object) -> AccessMode:
    """Parse a persisted or user-supplied mode, falling back to LOCALHOST.

    Matches the long-standing forgiving behaviour of
    ``ConfigLoader.access_pairing_mode``: an unrecognised value degrades
    to the safest mode rather than raising, because a typo in
    ``settings.json`` must never stop a brain from booting.
    """
    if isinstance(value, AccessMode):
        return value
    try:
        return AccessMode(str(value or "").strip().lower())
    except ValueError:
        return AccessMode.LOCALHOST


def parse_strict(value: object) -> AccessMode:
    """Parse a mode, raising ``ValueError`` on anything unrecognised.

    Used on the write path. Silently coercing an operator's explicit
    request is how a "Same WiFi" click ends up meaning "localhost";
    callers that accept user input want the error.
    """
    if isinstance(value, AccessMode):
        return value
    try:
        return AccessMode(str(value or "").strip().lower())
    except ValueError:
        valid = ", ".join(m.value for m in AccessMode)
        raise ValueError(f"unknown access mode {value!r} (expected one of: {valid})")


@dataclass(frozen=True)
class AccessModeResult:
    """Outcome of an :func:`apply_mode` call."""

    mode: AccessMode
    bind_host: str
    changed: bool
    restart_required: bool
    previous_mode: AccessMode
    previous_bind_host: str

    def as_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "bind_host": self.bind_host,
            "changed": self.changed,
            "restart_required": self.restart_required,
            "previous_mode": self.previous_mode.value,
            "previous_bind_host": self.previous_bind_host,
        }


def current_mode(config) -> AccessMode:
    """Read the persisted mode off a ``ConfigLoader``."""
    return coerce(config.get("access", "pairing_mode", AccessMode.LOCALHOST.value))


def configured_bind_host(config) -> str:
    """Read the persisted bind host off a ``ConfigLoader``.

    This is what the *next* boot will bind, which is not necessarily what
    the running process bound. Compare against
    ``config.runtime.bound_host()`` for that.
    """
    return str(config.get("network", "bind_host", "") or "127.0.0.1")


def apply_mode(config, mode: object) -> AccessModeResult:
    """Set the access mode and the bind host it implies. The only writer.

    Both keys are written through ``ConfigLoader.update_settings`` so the
    live in-memory config and ``settings.json`` move together. The old
    CLI helper wrote the JSON file directly, which left a running brain
    answering with a stale mode while the file said something else.

    Mirrors the derived bind host into ``FERAL_BIND_HOST`` because
    ``brain_bind_host()`` ranks that variable above the settings file;
    see the comment at the write site. It is only a mirror of intent.
    What the running listener actually bound is answered by
    ``config.runtime.bound_host()``, recorded at bind time and unaffected
    by this call.
    """
    target = parse_strict(mode)
    previous_mode = current_mode(config)
    previous_bind = configured_bind_host(config)
    bind = target.bind_host

    # Both keys are written unconditionally, even when they already match
    # the computed values. Applying a mode is an explicit operator
    # action, and it must leave the choice *recorded* rather than
    # resting on a default that a later change of defaults could move
    # underneath them. `changed` reports whether anything actually moved;
    # it does not gate the write.
    changed = (target is not previous_mode) or (bind != previous_bind)
    config.update_settings("access", "pairing_mode", target.value)
    config.update_settings("network", "bind_host", bind)

    # Keep the env mirror in step. ``brain_bind_host()`` ranks
    # ``FERAL_BIND_HOST`` above the settings file, and
    # ``hydrate_brain_runtime_env`` seeds that variable at boot, so a
    # stale value would silently shadow the mode we just persisted in
    # any flow that applies a mode and then serves in the same process
    # (``feral setup`` into ``feral serve``). This is only a mirror:
    # "what did the live listener actually bind" is answered by
    # ``config.runtime.bound_host()``, which is recorded at bind time
    # and is not affected by this write.
    os.environ["FERAL_BIND_HOST"] = bind

    if changed:
        logger.info(
            "access mode %s -> %s (bind_host %s -> %s)",
            previous_mode.value, target.value, previous_bind, bind,
        )

    return AccessModeResult(
        mode=target,
        bind_host=bind,
        changed=changed,
        restart_required=_restart_required(bind),
        previous_mode=previous_mode,
        previous_bind_host=previous_bind,
    )


def _restart_required(target_bind: str) -> bool:
    """True when the running process is bound to something else.

    ``bind_host`` is read once, at bind time, so a change cannot take
    effect in a live process. Reporting this honestly is the difference
    between "applied" and "applied, and it will actually work".
    """
    from config.runtime import bound_host

    actual = bound_host()
    if actual is None:
        # Nothing is serving in this process, so there is nothing to
        # restart. The next `feral serve` picks up the new value.
        return False
    return actual != target_bind


def repair_contradiction(config) -> Optional[AccessModeResult]:
    """Fix a mode/bind pair that cannot both be true. Returns None if fine.

    Called once at boot. Installs created before the single-writer
    refactor can hold ``pairing_mode: local`` with ``bind_host:
    127.0.0.1`` (the web-button bug), which advertises a LAN URL nothing
    is listening on.

    **A repair never widens network exposure.** Where the two disagree
    and honouring the mode would open a listener that is currently on
    loopback, the *mode* is demoted instead. The contradiction being
    repaired is, by construction, the state the old "Same WiFi" button
    produced, so a large number of installs hold it while listening only
    on 127.0.0.1. Resolving it toward the mode would silently bind every
    interface on the next boot, on machines whose owners know this only
    as "the thing that didn't work" and who may well be on a café or
    hotel network. Widening exposure is an operator decision and it does
    not get made inside ``discover()``, which also runs from
    ``feral doctor``.

    Demoting does not resurrect the original bug. The pair resolver now
    compares intent against the live listener and refuses with a
    remediation, and ``feral doctor`` reports that pairing is disabled
    and how to enable it, so the operator is told rather than left with
    a QR that cannot work. Re-applying "Same WiFi" is one click and it
    writes both keys correctly through :func:`apply_mode`.
    """
    mode = current_mode(config)
    persisted_bind = configured_bind_host(config)
    expected = mode.bind_host

    if persisted_bind == expected:
        return None

    if persisted_bind in LOOPBACK_HOSTS and expected not in LOOPBACK_HOSTS:
        logger.warning(
            "access mode %r wants bind_host %s but this brain is persisted "
            "on %s. Demoting the mode to %r rather than opening the "
            "listener: widening exposure is not a repair. Re-apply the mode "
            "from Settings (or `feral access lan`) to pair over WiFi.",
            mode.value, expected, persisted_bind, AccessMode.LOCALHOST.value,
        )
        return apply_mode(config, AccessMode.LOCALHOST)

    logger.warning(
        "access mode %r requires bind_host %s but settings.json says %s. "
        "Repairing to %s. This install predates the single-writer access "
        "mode and was advertising an address nothing was listening on.",
        mode.value, expected, persisted_bind, expected,
    )
    return apply_mode(config, mode)
