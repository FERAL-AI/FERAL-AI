""" Phase 1 — bundled-manifest discovery + capability registry.

Phase 1 only walks the in-tree bundle path
(``feral-core/channels/<id>/feral-channel.manifest.json``). 3rd-party
discovery (entry points, plugin directories, marketplace) is deferred
to  — see ``docs/contributing-channels.md`` for the roadmap.

The capability registry is the read-only index the rest of FERAL
consults instead of importing channel implementations directly. Today
that's `messaging_providers()` etc.; the same shape will hold once the
voice/file/webhook channels migrate ().
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .manifest import (
    ChannelManifest,
    ManifestError,
    ManifestSchemaError,
    ManifestSignatureError,
    MANIFEST_FILENAME,
    assert_signature,
    load_manifest,
)


__all__ = [
    "BUNDLED_CHANNELS_DIR",
    "CapabilityRegistry",
    "discover_bundled",
    "load_with_verification",
]


BUNDLED_CHANNELS_DIR = Path(__file__).parent

# Capability flag → "is X provider" naming. Centralising these strings
# keeps the registry aligned with the schema's `capabilities` keys; a
# typo here would silently make a channel invisible to the registry.
_CAPABILITY_TO_KIND = {
    "messagingProvider": "messaging",
    "voiceProvider": "voice",
    "fileProvider": "file",
    "webhookProvider": "webhook",
    "videoProvider": "video",
    "presenceProvider": "presence",
}


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------


def _iter_bundled_manifest_paths(root: Path) -> Iterator[Path]:
    """Yield candidate manifest paths under ``root``.

    Sorted by directory name so discovery is deterministic — important
    for prompt-cache stability and for tests that assert "first
    messaging provider is X."
    """
    if not root.exists() or not root.is_dir():
        return
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        candidate = child / MANIFEST_FILENAME
        if candidate.is_file():
            yield candidate


def discover_bundled(
    root: Optional[Path] = None,
) -> list[ChannelManifest]:
    """Walk the bundled channels directory and return validated manifests.

    Schema validation runs eagerly — a malformed manifest aborts the
    discovery (we do NOT silently skip it). Signature verification is
    *not* performed here so that ``discover_bundled`` can be used by
    tooling that wants to inspect unsigned drafts; for the
    verification-required path use :func:`load_with_verification`.
    """
    where = Path(root) if root is not None else BUNDLED_CHANNELS_DIR
    return [load_manifest(p) for p in _iter_bundled_manifest_paths(where)]


# ----------------------------------------------------------------------
# Capability registry
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityRegistry:
    """Read-only index over a set of validated manifests.

    Frozen + lookup-only: the registry is built once per discovery and
    handed out to consumers (the messaging skill, the voice planner,
    the webhook router). The class deliberately exposes neither
    ``add()`` nor ``remove()`` — mutation requires rebuilding from the
    manifest list, which keeps the registry consistent with the on-disk
    truth.
    """

    manifests: tuple[ChannelManifest, ...]
    _by_id: dict[str, ChannelManifest] = field(default_factory=dict)
    _by_capability: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _providers_by_capability: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def build_from(cls, manifests: Iterable[ChannelManifest]) -> "CapabilityRegistry":
        ms = tuple(manifests)

        by_id: dict[str, ChannelManifest] = {}
        for m in ms:
            if m.id in by_id:
                raise ManifestSchemaError(
                    f"duplicate channel manifest id {m.id!r}",
                    path=str(m.source_path or m.id),
                )
            by_id[m.id] = m

        ids_by_cap: dict[str, list[str]] = defaultdict(list)
        providers_by_cap: dict[str, list[str]] = defaultdict(list)
        seen_providers_per_cap: dict[str, set[str]] = defaultdict(set)
        for m in ms:
            for cap, on in m.capabilities.items():
                if not on:
                    continue
                ids_by_cap[cap].append(m.id)
                for prov in m.providers:
                    if prov in seen_providers_per_cap[cap]:
                        continue
                    seen_providers_per_cap[cap].add(prov)
                    providers_by_cap[cap].append(prov)

        return cls(
            manifests=ms,
            _by_id=by_id,
            _by_capability={k: tuple(v) for k, v in ids_by_cap.items()},
            _providers_by_capability={k: tuple(v) for k, v in providers_by_cap.items()},
        )

    # --- accessors ---------------------------------------------------

    def get(self, channel_id: str) -> Optional[ChannelManifest]:
        return self._by_id.get(channel_id)

    def __contains__(self, channel_id: object) -> bool:
        return isinstance(channel_id, str) and channel_id in self._by_id

    def __iter__(self) -> Iterator[ChannelManifest]:
        return iter(self.manifests)

    def __len__(self) -> int:
        return len(self.manifests)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._by_id.keys())

    # --- capability-keyed views (the public contract) ----------------

    def channels_with_capability(self, capability: str) -> tuple[str, ...]:
        return self._by_capability.get(capability, ())

    def providers_with_capability(self, capability: str) -> tuple[str, ...]:
        return self._providers_by_capability.get(capability, ())

    def messaging_providers(self) -> tuple[str, ...]:
        return self.providers_with_capability("messagingProvider")

    def voice_providers(self) -> tuple[str, ...]:
        return self.providers_with_capability("voiceProvider")

    def file_providers(self) -> tuple[str, ...]:
        return self.providers_with_capability("fileProvider")

    def webhook_providers(self) -> tuple[str, ...]:
        return self.providers_with_capability("webhookProvider")

    def video_providers(self) -> tuple[str, ...]:
        return self.providers_with_capability("videoProvider")

    def presence_providers(self) -> tuple[str, ...]:
        return self.providers_with_capability("presenceProvider")

    # Round-trip helper for diagnostics endpoints (read-only, dict-of-lists).
    def as_capability_map(self) -> dict[str, list[str]]:
        return {
            _CAPABILITY_TO_KIND.get(cap, cap): list(provs)
            for cap, provs in self._providers_by_capability.items()
        }


# ----------------------------------------------------------------------
# Verification entry point
# ----------------------------------------------------------------------


def load_with_verification(
    root: Optional[Path] = None,
    *,
    allow_unsigned: bool = False,
    public_key_provider: Optional[Any] = None,
) -> CapabilityRegistry:
    """Discover bundled manifests, optionally verify signatures, return a registry.

    * ``allow_unsigned=False`` (the default, and the right setting for
      production installs) refuses any manifest without a signature
      envelope and any manifest whose signature does not verify.
    * ``allow_unsigned=True`` accepts unsigned manifests but STILL
      rejects a present-but-invalid signature — a tampered envelope is
      always an error, no matter the policy. This matches the 
      pattern where ``--allow-unsigned`` is a developer convenience,
      not a vulnerability.
    * ``public_key_provider`` is forwarded to
      :func:`feral_core.channels.manifest.verify_signature`; pass
      ``None`` to trust the embedded public key (Phase 1 default; the
      loader is itself the trust root since the manifest is in-tree).
    """
    manifests = discover_bundled(root=root)
    for m in manifests:
        if m.signature is None:
            if allow_unsigned:
                continue
            raise ManifestSignatureError(
                f"channel manifest {m.id!r} is unsigned and allow_unsigned=False",
                path=str(m.source_path or m.id),
            )
        # Tampered/malformed envelope is always fatal — even with
        # allow_unsigned=True. See the policy note on this function.
        assert_signature(m, public_key_provider=public_key_provider)

    return CapabilityRegistry.build_from(manifests)


# Re-exports kept short on purpose; consumers reach through this module
# rather than importing from `manifest.py` directly.
__all__ += ["ManifestError", "ManifestSchemaError", "ManifestSignatureError"]
