"""Multi-key per-provider helper layered on top of :class:`BlindVault`.

Why this module exists
----------------------

``BlindVault`` (Wave 1) provides a single-credential-per-key model: one
``OPENAI_API_KEY`` entry, one ``ANTHROPIC_API_KEY`` entry, etc. Real
operators routinely run with **multiple keys per provider** — a prod
key vs. a dev key, an org key vs. a personal key, a key allocated to
a specific scheduled task — and need to switch between them without
re-typing the long sk-/skp- secret each time.

This module ships that without modifying ``vault.py`` core. The
labeled-keys feature is an additive overlay:

* labeled keys live in their own vault namespace
  (:data:`PROVIDER_KEYS_NAMESPACE`, ``"provider_keys"``) so they never
  collide with the legacy default-namespace ``OPENAI_API_KEY`` style
  entries and never invalidate Wave 1's smoke tests;

* the legacy default-namespace key continues to win when a runtime
  caller asks for "the" credential without specifying a label. That's
  the contract Wave 1's ``probe(provider).ok`` and the existing
  ``LLMProvider.__init__`` rely on, so we don't break them.

Lane 09 owns this file. Lane 03's ``vault.py`` is unchanged — see the
PR body and ``ASOS/AUDIT-r14/phase2/WORK_LOG.md`` for the
coordination decision.

Storage layout
--------------

Within the ``provider_keys`` namespace, each entry is keyed
``"<provider_id>:<label>"`` and the value is the raw API key /
secret. Namespace strings are case-sensitive; ``provider_id``
matches the LLMProvider runtime ids (``openai``, ``anthropic``,
``openrouter`` …) and the label is an operator-chosen tag like
``"prod"`` / ``"dev"`` / ``"team-a"``.

A separate metadata namespace
(:data:`PROVIDER_KEYS_META_NAMESPACE`, ``"provider_keys_meta"``)
keeps small JSON blobs per ``"<provider_id>:<label>"`` so the REST
surface can return human-readable info (``created_at``, ``last_used_at``,
``last_probe_at``, ``last_probe_ok``) without re-reading the secret.

Default labels
--------------

When the operator stores a key without specifying a label, we use
:data:`DEFAULT_LABEL` (``"default"``). The legacy default-namespace
``OPENAI_API_KEY`` style entry is still honoured; the labeled API is
strictly additive. ``set_active_label`` picks which labeled key the
runtime should treat as "the" provider key — that selection is what
:func:`get_active_key` consults on every LLM hot path entry (boot
hydration in ``api/state.py``, ``LLMProvider.__init__`` /
``_build_client`` / ``switch_provider`` / ``_get_provider_config``,
plus the Anthropic native stream). The resolution order is documented
on :func:`get_active_key`. Cross-cut #1 of v2026.5.42 wave wired this
in — prior to that release, ``LLMProvider`` never called any
``vault_keys`` helper and the active label was a no-op on chat.

REST contract
-------------

The REST surface in :mod:`api.routes.llm` exposes:

* ``POST   /api/llm/providers/{provider_id}/keys`` — add or replace
  a labeled key. Body: ``{label: str, api_key: str, set_active?: bool}``.
* ``GET    /api/llm/providers/{provider_id}/keys`` — list labels +
  metadata (no secrets).
* ``DELETE /api/llm/providers/{provider_id}/keys/{label}`` — remove
  a labeled key.
* ``POST   /api/llm/providers/{provider_id}/probe`` (with optional
  ``?label=...``) — probe the key behind the label.

Lane 07 (CLI) wraps this REST surface as ``feral key add/remove/list
--provider --label``. The contract is documented in the Lane 09 PR
body so Lane 07 can build to the spec.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("feral.security.vault_keys")


PROVIDER_KEYS_NAMESPACE = "provider_keys"
PROVIDER_KEYS_META_NAMESPACE = "provider_keys_meta"
PROVIDER_ACTIVE_LABEL_NAMESPACE = "provider_keys_active"
DEFAULT_LABEL = "default"


# ─────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────


_PROVIDER_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789-_")
_LABEL_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789-_.")


class InvalidProviderId(ValueError):
    """Raised when a ``provider_id`` contains characters that would
    poison the vault namespace key separator (``:``)."""


class InvalidLabel(ValueError):
    """Raised when a ``label`` violates the validator above."""


def _validate_provider_id(provider_id: str) -> str:
    if not isinstance(provider_id, str):
        raise InvalidProviderId(f"provider_id must be a string, got {type(provider_id).__name__}")
    pid = provider_id.strip().lower()
    if not pid:
        raise InvalidProviderId("provider_id must not be empty")
    if not set(pid) <= _PROVIDER_ID_ALLOWED:
        raise InvalidProviderId(
            f"provider_id {pid!r} contains characters outside [a-z0-9_-]; "
            "use the canonical LLMProvider runtime id"
        )
    return pid


def _validate_label(label: str) -> str:
    if not isinstance(label, str):
        raise InvalidLabel(f"label must be a string, got {type(label).__name__}")
    lbl = label.strip().lower()
    if not lbl:
        raise InvalidLabel("label must not be empty")
    if len(lbl) > 64:
        raise InvalidLabel("label must be ≤ 64 characters")
    if not set(lbl) <= _LABEL_ALLOWED:
        raise InvalidLabel(
            f"label {lbl!r} contains characters outside [a-z0-9_.-]; "
            "use a short, filesystem-safe tag like 'prod' / 'dev' / 'team-a'"
        )
    return lbl


def _composite_key(provider_id: str, label: str) -> str:
    return f"{provider_id}:{label}"


# ─────────────────────────────────────────────────────────────────────
# Result shape
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderKeyEntry:
    """Public-shape entry returned to REST callers. NEVER carries the secret."""

    provider_id: str
    label: str
    is_active: bool
    fingerprint: str
    created_at: float
    last_used_at: Optional[float]
    last_probe_at: Optional[float]
    last_probe_ok: Optional[bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "is_active": self.is_active,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "last_probe_at": self.last_probe_at,
            "last_probe_ok": self.last_probe_ok,
        }


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _vault_or_raise(vault: Any) -> Any:
    if vault is None:
        from security.vault import get_vault

        return get_vault()
    return vault


def _read_meta(vault: Any, provider_id: str, label: str) -> dict[str, Any]:
    raw = vault.get(PROVIDER_KEYS_META_NAMESPACE, _composite_key(provider_id, label))
    if not raw:
        return {}
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "vault_keys.meta_corrupt: %s/%s — resetting (existing secret unchanged)",
            provider_id,
            label,
        )
        return {}
    return blob if isinstance(blob, dict) else {}


def _write_meta(vault: Any, provider_id: str, label: str, blob: dict[str, Any]) -> None:
    vault.put(
        PROVIDER_KEYS_META_NAMESPACE,
        _composite_key(provider_id, label),
        json.dumps(blob, sort_keys=True, separators=(",", ":")),
        stored_by="vault_keys",
    )


def _fingerprint(vault: Any, secret: str) -> str:
    """Cheap fingerprint so the REST surface can show "different keys
    look different" without leaking the secret in full. We expose the
    first/last four characters (matching the standard "sk-...XYZW"
    convention every provider's dashboard uses) plus a short hash so
    two keys that share a prefix still fingerprint differently."""
    if not secret:
        return ""
    import hashlib

    digest = hashlib.sha256(secret.encode()).hexdigest()[:8]
    head = secret[:4] if len(secret) >= 8 else "***"
    tail = secret[-4:] if len(secret) >= 8 else "***"
    return f"{head}…{tail}({digest})"


def mask_key(secret: str) -> str:
    """Public ``sk-...XYZW`` display helper used by the CLI setup
    wizard + ``feral key`` surface.

    The provider/key step in the setup wizard needs to confirm "your
    key is already stored" without ever printing the secret. Every
    provider dashboard (OpenAI, Anthropic, Gemini, …) shows the
    leading 3–4 characters and the trailing 4 characters of an API
    key; we match that convention so the operator can confirm by eye
    that the displayed entry matches what they pasted in their
    dashboard.

    Returns ``""`` for empty / blank input so callers can branch on
    ``if mask_key(secret):`` without an extra emptiness check.
    Secrets shorter than 8 characters mask to ``***`` so we never
    leak more than ~half of a tiny placeholder string.
    """
    if not secret or not str(secret).strip():
        return ""
    s = str(secret).strip()
    if len(s) < 8:
        return "***"
    return f"{s[:3]}…{s[-4:]}"


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def add_provider_key(
    provider_id: str,
    label: str,
    api_key: str,
    *,
    set_active: bool = False,
    vault: Any = None,
) -> ProviderKeyEntry:
    """Store *api_key* under ``provider_id`` / ``label``. Replaces any
    existing entry with the same ``label`` (idempotent put — the
    operator's intent is "remember this credential under this name").

    Pass ``set_active=True`` to make this label the runtime's default
    selection (``set_active_label``).
    """
    pid = _validate_provider_id(provider_id)
    lbl = _validate_label(label)
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key must be a non-empty string")

    v = _vault_or_raise(vault)
    composite = _composite_key(pid, lbl)
    existing_meta = _read_meta(v, pid, lbl)
    now = time.time()
    meta = {
        "created_at": float(existing_meta.get("created_at") or now),
        "updated_at": now,
        "last_used_at": existing_meta.get("last_used_at"),
        "last_probe_at": existing_meta.get("last_probe_at"),
        "last_probe_ok": existing_meta.get("last_probe_ok"),
    }
    v.put(PROVIDER_KEYS_NAMESPACE, composite, api_key.strip(), stored_by="vault_keys")
    _write_meta(v, pid, lbl, meta)
    if set_active:
        set_active_label(pid, lbl, vault=v)

    active_label = get_active_label(pid, vault=v)
    return ProviderKeyEntry(
        provider_id=pid,
        label=lbl,
        is_active=(active_label == lbl),
        fingerprint=_fingerprint(v, api_key.strip()),
        created_at=float(meta["created_at"]),
        last_used_at=meta.get("last_used_at"),
        last_probe_at=meta.get("last_probe_at"),
        last_probe_ok=meta.get("last_probe_ok"),
    )


def remove_provider_key(
    provider_id: str,
    label: str,
    *,
    vault: Any = None,
) -> bool:
    """Remove the labeled key + its metadata. Returns ``True`` when
    something was removed, ``False`` when no such label existed.

    If the removed label was the active selection, the active pointer
    is cleared — the runtime falls back to the legacy default-namespace
    credential (i.e. the env-var-derived ``OPENAI_API_KEY`` style entry)
    until the operator picks a new active label.
    """
    pid = _validate_provider_id(provider_id)
    lbl = _validate_label(label)
    v = _vault_or_raise(vault)
    composite = _composite_key(pid, lbl)
    removed = v.remove_from(PROVIDER_KEYS_NAMESPACE, composite, removed_by="vault_keys")
    v.remove_from(PROVIDER_KEYS_META_NAMESPACE, composite, removed_by="vault_keys")
    if get_active_label(pid, vault=v) == lbl:
        v.remove_from(
            PROVIDER_ACTIVE_LABEL_NAMESPACE, pid, removed_by="vault_keys",
        )
    return bool(removed)


def list_provider_keys(
    provider_id: str,
    *,
    vault: Any = None,
) -> list[ProviderKeyEntry]:
    """Return labeled-key entries for *provider_id*. Never includes
    the secret value — only metadata + fingerprint."""
    pid = _validate_provider_id(provider_id)
    v = _vault_or_raise(vault)
    raw_keys = v.list_namespace(PROVIDER_KEYS_NAMESPACE)
    prefix = f"{pid}:"
    labels = sorted(k[len(prefix):] for k in raw_keys if k.startswith(prefix))
    active = get_active_label(pid, vault=v)
    out: list[ProviderKeyEntry] = []
    for lbl in labels:
        meta = _read_meta(v, pid, lbl)
        secret = v.get(PROVIDER_KEYS_NAMESPACE, _composite_key(pid, lbl)) or ""
        out.append(
            ProviderKeyEntry(
                provider_id=pid,
                label=lbl,
                is_active=(active == lbl),
                fingerprint=_fingerprint(v, secret),
                created_at=float(meta.get("created_at") or 0.0),
                last_used_at=meta.get("last_used_at"),
                last_probe_at=meta.get("last_probe_at"),
                last_probe_ok=meta.get("last_probe_ok"),
            )
        )
    return out


def get_provider_key(
    provider_id: str,
    label: str,
    *,
    vault: Any = None,
    record_use: bool = False,
) -> Optional[str]:
    """Return the raw secret for ``(provider_id, label)`` or ``None``.

    ``record_use=True`` updates ``last_used_at`` so the REST surface
    can render "Used 3 minutes ago" timestamps without exposing the
    secret. Callers that just want to inspect the key (e.g. probe)
    should leave ``record_use=False``.
    """
    pid = _validate_provider_id(provider_id)
    lbl = _validate_label(label)
    v = _vault_or_raise(vault)
    secret = v.get(PROVIDER_KEYS_NAMESPACE, _composite_key(pid, lbl))
    if secret and record_use:
        meta = _read_meta(v, pid, lbl)
        meta["last_used_at"] = time.time()
        _write_meta(v, pid, lbl, meta)
    return secret


def set_active_label(
    provider_id: str,
    label: str,
    *,
    vault: Any = None,
) -> str:
    """Select which labeled key is the runtime default for *provider_id*.

    Raises ``KeyError`` when the label has no stored secret — the
    operator must have already added the key first. Returns the label
    that is now active.
    """
    pid = _validate_provider_id(provider_id)
    lbl = _validate_label(label)
    v = _vault_or_raise(vault)
    if v.get(PROVIDER_KEYS_NAMESPACE, _composite_key(pid, lbl)) is None:
        raise KeyError(
            f"no labeled key stored for {pid}:{lbl}; add it first via add_provider_key"
        )
    v.put(PROVIDER_ACTIVE_LABEL_NAMESPACE, pid, lbl, stored_by="vault_keys")
    return lbl


def get_active_label(
    provider_id: str,
    *,
    vault: Any = None,
) -> Optional[str]:
    pid = _validate_provider_id(provider_id)
    v = _vault_or_raise(vault)
    return v.get(PROVIDER_ACTIVE_LABEL_NAMESPACE, pid)


def get_active_provider_key(
    provider_id: str,
    *,
    vault: Any = None,
    record_use: bool = False,
) -> Optional[str]:
    """Return the raw secret for whatever label is currently active.

    Falls back to ``DEFAULT_LABEL`` when no explicit active selection
    has been recorded. Returns ``None`` when no labeled key has been
    stored at all (caller should fall back to legacy env / default
    vault namespace).
    """
    pid = _validate_provider_id(provider_id)
    v = _vault_or_raise(vault)
    active = get_active_label(pid, vault=v)
    if active:
        secret = get_provider_key(pid, active, vault=v, record_use=record_use)
        if secret:
            return secret
    return get_provider_key(pid, DEFAULT_LABEL, vault=v, record_use=record_use)


# ─────────────────────────────────────────────────────────────────────
# LLM hot-path resolver (Cross-cut #1, v2026.5.42)
# ─────────────────────────────────────────────────────────────────────


# Per-provider env var fallback consulted by :func:`get_active_key`.
# Mirrors ``agents.llm_provider._PROVIDER_REGISTRY`` but lives here so
# the security layer never has to import the LLM runtime (would form a
# cycle). Keep in sync when a new runtime provider lands in
# ``_PROVIDER_REGISTRY``.
_PROVIDER_ENV_KEYS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
}


def get_active_key(provider_id: str, *, vault: Any = None) -> str:
    """Resolve the live API key for *provider_id* on the LLM hot path.

    Resolution order (first non-empty wins):

    1. The labeled secret pointed at by ``set_active_label`` (falling
       back to :data:`DEFAULT_LABEL` per
       :func:`get_active_provider_key`). ``record_use=True`` so the
       REST list endpoint can render "Used 3 minutes ago".
    2. The legacy default-namespace vault entry keyed by the
       provider's canonical env var name (``OPENAI_API_KEY``,
       ``ANTHROPIC_API_KEY``, …). This is what
       ``api/state._load_stored_credentials`` populates on boot and is
       the historical "unlabeled" credential.
    3. The process env var of the same name.
    4. ``""`` — signals "no key" so the caller can flip
       ``available=False`` instead of dialling an endpoint with an
       empty ``Authorization`` header.

    Never raises. Vault read failures degrade silently to env lookup so
    a corrupt / locked vault cannot break the chat path entirely.
    Cross-cut #1 of v2026.5.42 wave (see
    ``AUDIT-r14/round3/findings/lane4-vault-keys-hot-path.md``).
    """
    try:
        pid = _validate_provider_id(provider_id)
    except InvalidProviderId:
        return ""

    v: Any
    try:
        v = _vault_or_raise(vault)
    except Exception as exc:
        logger.debug("get_active_key(%s): vault unavailable (%s)", pid, exc)
        v = None

    if v is not None:
        try:
            # record_use=False: this resolver runs on every LLM failover
            # candidate build, dashboard heartbeat (~10s), and background
            # loop. Recording use here wrote provider_keys_meta + logged
            # "Credential stored" on every read — churning the vault and
            # flooding the log. Usage timestamps, if needed, should be
            # recorded once on a successful upstream call, not on resolve.
            labeled = get_active_provider_key(pid, vault=v, record_use=False)
        except Exception as exc:
            logger.debug("get_active_key(%s): labeled lookup failed (%s)", pid, exc)
            labeled = None
        if labeled:
            return labeled

    env_key = _PROVIDER_ENV_KEYS.get(pid, "")
    if v is not None and env_key:
        try:
            legacy = v.get_credential(env_key)
        except Exception as exc:
            logger.debug("get_active_key(%s): legacy vault read failed (%s)", pid, exc)
            legacy = None
        if legacy:
            return legacy

    if env_key:
        env_val = os.getenv(env_key, "") or ""
        if env_val:
            return env_val

    return ""


def record_probe_result(
    provider_id: str,
    label: str,
    *,
    ok: bool,
    vault: Any = None,
) -> None:
    """Update the per-label probe metadata after a probe round-trip.

    Caller (the REST probe endpoint) runs the actual probe via
    ``security.probe.probe`` and feeds the verdict back here so the
    list endpoint can render "Probe: ok 30s ago" / "Probe: 401 5m ago"
    without re-issuing.
    """
    pid = _validate_provider_id(provider_id)
    lbl = _validate_label(label)
    v = _vault_or_raise(vault)
    meta = _read_meta(v, pid, lbl)
    meta["last_probe_at"] = time.time()
    meta["last_probe_ok"] = bool(ok)
    _write_meta(v, pid, lbl, meta)
