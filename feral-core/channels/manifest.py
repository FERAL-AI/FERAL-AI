"""W21 Phase 1 — channel manifest loader, validator, and signature glue.

Why this module exists
----------------------
A declarative per-extension manifest is the architectural rule that
makes a large extension surface sustainable. W21 brings that rule to
FERAL **channels**: a single declarative
``feral-channel.manifest.json`` per channel describing the providers it
speaks to, the env vars its auth needs, and the capabilities it
advertises (messaging / voice / file / webhook / ...).

This Phase-1 file ships only the **schema validator + loader + W8 sign
verification glue**. The bundled Telegram manifest beside the existing
adapter is the worked example. Migrating the other channels is W21.2;
the full extension SDK + 3rd-party discovery is W21.3 / W21.4.

Library notes
-------------
* We hand-roll the schema validation rather than pulling in
  ``jsonschema``. The constraint set is small (object shape, string
  patterns, enum, array uniqueness, integer/boolean types), and a hand
  validator gives us **specific error messages** keyed to the field
  path — exactly what manifest authors need. The on-disk schema is
  still draft-07 so 3rd-party tooling (IDE, CI lint) can use any
  off-the-shelf validator.
* Signature verification is a thin adapter over
  ``feral_core.genui.manifest_signing`` (W8). We deliberately do NOT
  reimplement Ed25519 here — same primitive, same canonical_json
  encoding, same `(ok, reason)` contract. The only differences are
  field names (``publicKeyId``/``signedAt``/``algo`` to match the
  manifest schema instead of the SignedManifest envelope used by
  GenUI) and the fact that the signature is *embedded inside* the
  manifest dict instead of wrapping it.

Error policy (no try/except: pass)
----------------------------------
Three distinct error types, each carries a structured `path` so the
caller can point at the offending field:

* :class:`ManifestSchemaError` — file is malformed JSON, fails the
  draft-07 shape, or violates a structural constraint.
* :class:`ManifestSignatureError` — signature envelope is present but
  malformed, or the signature does not verify against the manifest
  payload.
* :class:`ManifestUnknownError` — anything else the loader cannot
  classify (e.g. permission denied opening the file). Distinct from
  the above two so callers don't over-classify a transient I/O failure
  as a malicious manifest.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Reuse W8's ed25519 helpers verbatim — same library, same canonical
# JSON encoding, same key sizes. The W8 audit covered the primitive.
from genui.manifest_signing import (  # type: ignore[import-not-found]
    canonical_json,
    sign as _genui_sign,
    verify as _genui_verify,
    SignedManifest,
    SignatureFormatError,
    ALG_ED25519,
)


__all__ = [
    "ChannelManifest",
    "ManifestError",
    "ManifestSchemaError",
    "ManifestSignatureError",
    "ManifestUnknownError",
    "load_manifest",
    "load_manifest_dict",
    "verify_signature",
    "sign_manifest",
    "MANIFEST_FILENAME",
    "SCHEMA_PATH",
]


# Bundled manifests live next to their adapter as
# ``feral-core/channels/<channel-id>/feral-channel.manifest.json``.
MANIFEST_FILENAME = "feral-channel.manifest.json"

# Co-located so editors with $schema support pick up validation.
SCHEMA_PATH = Path(__file__).parent / "manifest_schema.json"


# ----------------------------------------------------------------------
# Error taxonomy
# ----------------------------------------------------------------------


class ManifestError(Exception):
    """Base class for every manifest-loader error.

    Carries a ``path`` (dotted JSON path of the offending field, or the
    file path for I/O errors) so callers can show users *which* part of
    *which* manifest broke without re-parsing the file.
    """

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.path = path

    def __str__(self) -> str:  # pragma: no cover — formatting only
        base = super().__str__()
        return f"{base} (at {self.path})" if self.path else base


class ManifestSchemaError(ManifestError):
    """The file isn't a valid manifest under the draft-07 schema."""


class ManifestSignatureError(ManifestError):
    """The signature envelope is malformed or doesn't verify."""


class ManifestUnknownError(ManifestError):
    """Anything the loader cannot classify (e.g. unreadable file)."""


# ----------------------------------------------------------------------
# Dataclass surface
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelManifest:
    """In-memory representation of a validated manifest.

    Frozen so the loader can hand instances out to long-lived
    registries without worrying about callers mutating shared state.
    The original validated dict is kept on ``raw`` for round-tripping
    and for signature verification (canonical_json runs over the dict
    with ``signature`` removed).
    """

    id: str
    providers: tuple[str, ...]
    provider_auth_env_vars: dict[str, tuple[str, ...]]
    capabilities: dict[str, bool]
    provider_auth_choices: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    model_support: Optional[dict[str, Any]] = None
    contracts: Optional[dict[str, Any]] = None
    signature: Optional[dict[str, Any]] = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    source_path: Optional[Path] = None

    @property
    def is_signed(self) -> bool:
        return self.signature is not None

    def capability(self, name: str) -> bool:
        """Return ``True`` iff capability ``name`` is advertised AND on."""
        return bool(self.capabilities.get(name, False))


# ----------------------------------------------------------------------
# Schema validation (hand-rolled — see module docstring)
# ----------------------------------------------------------------------


_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CLI_FLAG_RE = re.compile(r"^--[a-z][a-z0-9-]*$")
_AUTH_METHODS = frozenset({"oauth", "device-code", "api-key"})

_KNOWN_TOP_LEVEL = frozenset({
    "id",
    "providers",
    "providerAuthEnvVars",
    "capabilities",
    "providerAuthChoices",
    "modelSupport",
    "contracts",
    "signature",
})

_KNOWN_AUTH_CHOICE_KEYS = frozenset({
    "provider",
    "method",
    "choiceId",
    "choiceLabel",
    "choiceHint",
    "assistantPriority",
    "groupId",
    "groupLabel",
    "groupHint",
    "optionKey",
    "cliFlag",
    "cliOption",
    "cliDescription",
    "deprecatedChoiceIds",
})

_KNOWN_MODEL_SUPPORT_KEYS = frozenset({"modelPrefixes", "preferredModels"})

_KNOWN_SIGNATURE_KEYS = frozenset({
    "algo",
    "publicKeyId",
    "publicKey",
    "signature",
    "signedAt",
})


def _require(cond: bool, message: str, *, path: str) -> None:
    """Compact assertion that raises the right error type on failure.

    Centralised so every validation error in this module shares the
    same shape (`ManifestSchemaError(path=...)`). No silent swallowing
    of the failure — the doctrine forbids `try/except: pass`.
    """
    if not cond:
        raise ManifestSchemaError(message, path=path)


def _validate_top_level(data: Any) -> dict[str, Any]:
    _require(isinstance(data, dict), "manifest must be a JSON object", path="$")
    extras = set(data.keys()) - _KNOWN_TOP_LEVEL
    _require(
        not extras,
        f"unknown top-level keys: {sorted(extras)}",
        path="$",
    )
    for required in ("id", "providers", "providerAuthEnvVars", "capabilities"):
        _require(required in data, f"missing required field {required!r}", path="$")
    return data


def _validate_id(value: Any) -> str:
    _require(isinstance(value, str), "id must be a string", path="$.id")
    _require(0 < len(value) <= 64, "id length must be 1..64", path="$.id")
    _require(
        bool(_ID_RE.match(value)),
        "id must match ^[a-z][a-z0-9_-]*$",
        path="$.id",
    )
    return value


def _validate_providers(value: Any) -> tuple[str, ...]:
    _require(isinstance(value, list), "providers must be an array", path="$.providers")
    _require(len(value) >= 1, "providers must contain at least one entry", path="$.providers")
    seen: set[str] = set()
    for idx, item in enumerate(value):
        path = f"$.providers[{idx}]"
        _require(isinstance(item, str), "provider entry must be a string", path=path)
        _require(
            bool(_ID_RE.match(item)),
            "provider entry must match ^[a-z][a-z0-9_-]*$",
            path=path,
        )
        _require(item not in seen, f"duplicate provider {item!r}", path=path)
        seen.add(item)
    return tuple(value)


def _validate_provider_auth_env_vars(
    value: Any, providers: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    path_root = "$.providerAuthEnvVars"
    _require(isinstance(value, dict), "providerAuthEnvVars must be an object", path=path_root)
    _require(len(value) >= 1, "providerAuthEnvVars must declare at least one provider", path=path_root)
    out: dict[str, tuple[str, ...]] = {}
    for key, vars_list in value.items():
        path = f"{path_root}.{key}"
        _require(
            key in providers,
            f"providerAuthEnvVars key {key!r} is not in providers list",
            path=path,
        )
        _require(isinstance(vars_list, list), "value must be an array of env-var names", path=path)
        _require(len(vars_list) >= 1, "must list at least one env var", path=path)
        seen: set[str] = set()
        for idx, name in enumerate(vars_list):
            ipath = f"{path}[{idx}]"
            _require(isinstance(name, str), "env var name must be a string", path=ipath)
            _require(
                bool(_ENV_RE.match(name)),
                "env var name must match ^[A-Z][A-Z0-9_]*$",
                path=ipath,
            )
            _require(name not in seen, f"duplicate env var {name!r}", path=ipath)
            seen.add(name)
        out[key] = tuple(vars_list)
    return out


def _validate_capabilities(value: Any) -> dict[str, bool]:
    path = "$.capabilities"
    _require(isinstance(value, dict), "capabilities must be an object", path=path)
    _require(len(value) >= 1, "capabilities must declare at least one flag", path=path)
    has_true = False
    for key, flag in value.items():
        kpath = f"{path}.{key}"
        _require(isinstance(key, str) and key, "capability key must be a non-empty string", path=kpath)
        _require(isinstance(flag, bool), "capability value must be a boolean", path=kpath)
        if flag:
            has_true = True
    _require(has_true, "at least one capability must be true", path=path)
    return dict(value)


def _validate_provider_auth_choices(value: Any) -> tuple[dict[str, Any], ...]:
    path_root = "$.providerAuthChoices"
    _require(isinstance(value, list), "providerAuthChoices must be an array", path=path_root)
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(value):
        ipath = f"{path_root}[{idx}]"
        _require(isinstance(item, dict), "auth choice must be an object", path=ipath)
        extras = set(item.keys()) - _KNOWN_AUTH_CHOICE_KEYS
        _require(not extras, f"unknown keys in auth choice: {sorted(extras)}", path=ipath)
        for required in ("provider", "method", "choiceId"):
            _require(required in item, f"missing required field {required!r}", path=ipath)
        _require(isinstance(item["provider"], str) and _ID_RE.match(item["provider"]),
                 "provider must match ^[a-z][a-z0-9_-]*$", path=f"{ipath}.provider")
        _require(item["method"] in _AUTH_METHODS,
                 f"method must be one of {sorted(_AUTH_METHODS)}", path=f"{ipath}.method")
        _require(isinstance(item["choiceId"], str) and 0 < len(item["choiceId"]) <= 128,
                 "choiceId must be a 1..128 char string", path=f"{ipath}.choiceId")
        if "cliFlag" in item:
            _require(
                isinstance(item["cliFlag"], str) and bool(_CLI_FLAG_RE.match(item["cliFlag"])),
                "cliFlag must match ^--[a-z][a-z0-9-]*$",
                path=f"{ipath}.cliFlag",
            )
        if "assistantPriority" in item:
            ap = item["assistantPriority"]
            _require(isinstance(ap, int) and not isinstance(ap, bool),
                     "assistantPriority must be an integer", path=f"{ipath}.assistantPriority")
        out.append(dict(item))
    return tuple(out)


def _validate_model_support(value: Any) -> dict[str, Any]:
    path = "$.modelSupport"
    _require(isinstance(value, dict), "modelSupport must be an object", path=path)
    extras = set(value.keys()) - _KNOWN_MODEL_SUPPORT_KEYS
    _require(not extras, f"unknown keys in modelSupport: {sorted(extras)}", path=path)
    for key in ("modelPrefixes", "preferredModels"):
        if key in value:
            kpath = f"{path}.{key}"
            _require(isinstance(value[key], list), f"{key} must be an array", path=kpath)
            for idx, item in enumerate(value[key]):
                _require(isinstance(item, str) and item,
                         f"{key} entries must be non-empty strings",
                         path=f"{kpath}[{idx}]")
    return dict(value)


def _validate_contracts(value: Any) -> dict[str, Any]:
    path = "$.contracts"
    _require(isinstance(value, dict), "contracts must be an object", path=path)
    for key, val in value.items():
        kpath = f"{path}.{key}"
        if isinstance(val, str):
            _require(bool(val), "contract value cannot be empty string", path=kpath)
        elif isinstance(val, list):
            for idx, item in enumerate(val):
                _require(isinstance(item, str) and item,
                         "contract list entries must be non-empty strings",
                         path=f"{kpath}[{idx}]")
        else:
            _require(False, "contract value must be string or array of strings", path=kpath)
    return dict(value)


def _validate_signature_envelope(value: Any) -> dict[str, Any]:
    """Shape-check the signature envelope.

    NOTE: this only validates the **shape** of the envelope. Whether
    the signature actually verifies under the embedded public key is
    answered by :func:`verify_signature`, not here. We separate the
    two so a manifest can be loaded (and inspected) without having to
    succeed on verification — the loader's ``allow_unsigned`` /
    ``verify_signature`` flag in the registry decides whether
    verification is required for a particular call site.
    """
    path = "$.signature"
    _require(isinstance(value, dict), "signature must be an object", path=path)
    extras = set(value.keys()) - _KNOWN_SIGNATURE_KEYS
    _require(not extras, f"unknown keys in signature: {sorted(extras)}", path=path)
    for required in ("algo", "publicKeyId", "publicKey", "signature", "signedAt"):
        _require(required in value, f"missing required field {required!r}", path=path)
    _require(value["algo"] == ALG_ED25519,
             f"signature.algo must be {ALG_ED25519!r}", path=f"{path}.algo")
    for k in ("publicKeyId", "publicKey", "signature", "signedAt"):
        kpath = f"{path}.{k}"
        _require(isinstance(value[k], str) and value[k],
                 f"signature.{k} must be a non-empty string", path=kpath)
    return dict(value)


# ----------------------------------------------------------------------
# Public load/validate entry points
# ----------------------------------------------------------------------


def load_manifest_dict(data: Any, *, source_path: Optional[Path] = None) -> ChannelManifest:
    """Validate an already-parsed dict and return a :class:`ChannelManifest`.

    Useful for tests + for callers that read manifests from somewhere
    other than disk (a registry server, a config file with multiple
    embedded manifests, etc.).
    """
    validated = _validate_top_level(data)
    manifest_id = _validate_id(validated["id"])
    providers = _validate_providers(validated["providers"])
    auth_envs = _validate_provider_auth_env_vars(validated["providerAuthEnvVars"], providers)
    caps = _validate_capabilities(validated["capabilities"])

    auth_choices: tuple[dict[str, Any], ...] = ()
    if "providerAuthChoices" in validated:
        auth_choices = _validate_provider_auth_choices(validated["providerAuthChoices"])
        for idx, choice in enumerate(auth_choices):
            _require(
                choice["provider"] in providers,
                f"providerAuthChoices[{idx}].provider {choice['provider']!r} not in providers",
                path=f"$.providerAuthChoices[{idx}].provider",
            )

    model_support = (
        _validate_model_support(validated["modelSupport"])
        if "modelSupport" in validated else None
    )
    contracts = (
        _validate_contracts(validated["contracts"])
        if "contracts" in validated else None
    )
    signature = (
        _validate_signature_envelope(validated["signature"])
        if "signature" in validated else None
    )

    return ChannelManifest(
        id=manifest_id,
        providers=providers,
        provider_auth_env_vars=auth_envs,
        capabilities=caps,
        provider_auth_choices=auth_choices,
        model_support=model_support,
        contracts=contracts,
        signature=signature,
        raw=dict(validated),
        source_path=source_path,
    )


def load_manifest(path: Path | str) -> ChannelManifest:
    """Read + parse + validate a ``feral-channel.manifest.json`` file."""
    p = Path(path)
    if not p.exists():
        raise ManifestUnknownError(f"manifest file does not exist: {p}", path=str(p))
    if not p.is_file():
        raise ManifestUnknownError(f"manifest path is not a regular file: {p}", path=str(p))

    raw_text = p.read_text(encoding="utf-8")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ManifestSchemaError(
            f"manifest is not valid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})",
            path=str(p),
        ) from exc

    return load_manifest_dict(data, source_path=p)


# ----------------------------------------------------------------------
# Signature glue (reuses W8's PyNaCl helpers)
# ----------------------------------------------------------------------


def _payload_for_signing(manifest_dict: dict[str, Any]) -> dict[str, Any]:
    """Return the dict actually fed to ``canonical_json``.

    The signature envelope is excluded so that signing/verification
    operates on the *unsigned* manifest payload — otherwise the
    signature would have to predict its own bytes (impossible), and
    re-serialising for verification would have to know the exact
    field order the publisher used.
    """
    return {k: v for k, v in manifest_dict.items() if k != "signature"}


def sign_manifest(
    manifest_dict: dict[str, Any],
    private_key: bytes,
    *,
    public_key_id: str = "default",
    signed_at: Optional[_dt.datetime] = None,
) -> dict[str, Any]:
    """Embed a signature envelope into ``manifest_dict``.

    Returns a NEW dict — the input is not mutated. The signature is
    produced by W8's ``genui.manifest_signing.sign`` over the
    canonical JSON of the manifest with ``signature`` removed; we then
    reshape the W8 ``SignedManifest`` envelope into the
    manifest-schema field names (``algo``/``publicKeyId``/``signedAt``)
    so it round-trips through :func:`verify_signature` without any
    publisher-side glue.
    """
    payload = _payload_for_signing(manifest_dict)
    if not payload:
        raise ManifestSchemaError("cannot sign empty manifest", path="$")

    signed: SignedManifest = _genui_sign(
        payload,
        private_key,
        key_id=public_key_id,
        signed_at=signed_at,
    )

    out = dict(manifest_dict)
    out["signature"] = {
        "algo": signed.alg,
        "publicKeyId": signed.key_id,
        "publicKey": signed.public_key,
        "signature": signed.signature,
        "signedAt": signed.signed_at.isoformat(),
    }
    return out


def verify_signature(
    manifest: ChannelManifest,
    *,
    public_key_provider: Optional[Any] = None,
) -> tuple[bool, Optional[str]]:
    """Verify ``manifest`` against W8's Ed25519 verifier.

    * ``public_key_provider`` is either ``None`` (trust the embedded
      public key, which is appropriate for the bundled-manifest
      Phase-1 case where the loader is the trust root) or a callable
      taking ``publicKeyId`` and returning the expected base64 public
      key. This is the seam that lets a future installer pin to a
      vault-stored publisher key without changing the loader contract.

    Returns ``(ok, reason)`` mirroring ``genui.manifest_signing.verify``.
    Reasons start with ``"format_error:"`` for malformed envelopes and
    ``"signature_mismatch"`` / ``"key_mismatch"`` for the cryptographic
    failures — the same wire contract as W8.
    """
    if manifest.signature is None:
        return False, "unsigned"

    sig = manifest.signature
    if sig.get("algo") != ALG_ED25519:
        return False, f"unsupported_alg:{sig.get('algo')!r}"

    expected_pk: Optional[str] = None
    if public_key_provider is not None:
        expected_pk = public_key_provider(sig.get("publicKeyId", ""))
        if not isinstance(expected_pk, str) or not expected_pk:
            return False, "key_mismatch"

    try:
        signed_at = _dt.datetime.fromisoformat(sig["signedAt"])
    except (TypeError, ValueError) as exc:
        return False, f"format_error:signedAt_invalid:{exc}"

    try:
        envelope = SignedManifest(
            manifest=_payload_for_signing(manifest.raw),
            signature=sig["signature"],
            public_key=sig["publicKey"],
            key_id=sig["publicKeyId"],
            signed_at=signed_at,
            alg=ALG_ED25519,
        )
    except (SignatureFormatError, ValueError) as exc:
        return False, f"format_error:{exc}"

    return _genui_verify(envelope, expected_public_key_b64=expected_pk)


def assert_signature(
    manifest: ChannelManifest,
    *,
    public_key_provider: Optional[Any] = None,
) -> None:
    """Verify-or-raise wrapper. Raises :class:`ManifestSignatureError`."""
    ok, reason = verify_signature(manifest, public_key_provider=public_key_provider)
    if not ok:
        path = str(manifest.source_path or f"<manifest {manifest.id}>")
        raise ManifestSignatureError(
            f"channel manifest signature did not verify: {reason}",
            path=path,
        )


# Public re-export so callers don't have to know the signing helper
# lives in genui. Keeps the import surface scoped to `feral_core.channels`.
canonical_manifest_json = canonical_json
