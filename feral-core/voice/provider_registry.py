"""One import-safe entry point for registering voice providers.

Why
---
``api/state.py`` registers chained-pipeline providers by importing
their modules for the decorator side effect, six unguarded imports in
a row inside the ``ChainedVoicePipeline`` boot block. That was fine
while every provider was a thin ``httpx`` wrapper with no dependency
of its own.

Local engines break the assumption. ``pywhispercpp``, ``faster-whisper``
and ``piper-tts`` are optional extras, and every one of them is a
native wheel that can fail to import for reasons that have nothing to
do with FERAL: an ABI mismatch, a missing ``libstdc++``, a Rosetta
install. An unguarded ``import voice.tts_providers.piper`` at boot
would turn "the operator did not install the optional TTS extra" into
"the brain does not start".

So registration goes through here instead. Each provider module is
imported in isolation, failures are recorded and logged rather than
raised, and the caller gets a report it can put in the boot summary.

The provider modules are themselves written to import cleanly without
their optional dependency present - the heavy import happens inside
the constructor, not at module scope - so in practice nothing here
should fail. This exists because "should" is not a guarantee to hang a
brain's startup on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("feral.voice.registry")

# Cloud providers. Always present, no optional dependency, but routed
# through the same guard so one broken module cannot take the rest of
# the voice stack with it.
CLOUD_PROVIDER_MODULES: tuple[str, ...] = (
    "voice.stt_providers.deepgram",
    "voice.stt_providers.openai_whisper",
    "voice.stt_providers.groq_whisper",
    "voice.tts_providers.openai_tts",
    "voice.tts_providers.elevenlabs",
    "voice.tts_providers.cartesia",
)

# Local engines. Each may be absent; absence is normal, not an error.
LOCAL_PROVIDER_MODULES: tuple[str, ...] = (
    "voice.stt_providers.whispercpp",
    "voice.stt_providers.faster_whisper_local",
    "voice.tts_providers.macos_say",
    "voice.tts_providers.piper",
)


@dataclass
class RegistrationReport:
    registered: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        parts = [f"{len(self.registered)} provider modules registered"]
        if self.failed:
            parts.append(
                "unavailable: "
                + ", ".join(
                    f"{name.rsplit('.', 1)[-1]} ({reason})"
                    for name, reason in sorted(self.failed.items())
                )
            )
        return "; ".join(parts)


def _import_guarded(module: str, report: RegistrationReport) -> None:
    import importlib

    try:
        importlib.import_module(module)
        report.registered.append(module)
    except Exception as exc:
        report.failed[module] = f"{exc.__class__.__name__}: {exc}"
        logger.info(
            "Voice provider module %s unavailable: %s", module, exc
        )


def register_voice_providers(
    *, include_local: bool = True, include_cloud: bool = True
) -> RegistrationReport:
    """Import every provider module. Never raises.

    Importing is what registers: each module carries a
    ``@register_stt_provider`` / ``@register_tts_provider`` decorator
    that populates the lookup tables ``get_stt_provider`` and
    ``get_tts_provider`` read at session-open time.
    """
    report = RegistrationReport()
    if include_cloud:
        for module in CLOUD_PROVIDER_MODULES:
            _import_guarded(module, report)
    if include_local:
        for module in LOCAL_PROVIDER_MODULES:
            _import_guarded(module, report)
    return report


def registered_provider_names() -> dict[str, list[str]]:
    """What is actually selectable right now, by kind."""
    from voice.stt_providers import _PROVIDER_REGISTRY as stt_registry
    from voice.tts_providers import _PROVIDER_REGISTRY as tts_registry

    return {
        "stt": sorted(stt_registry),
        "tts": sorted(tts_registry),
    }


def is_local_provider(kind: str, name: str) -> bool:
    """True when *name* names an engine that runs on this machine.

    Reads the class's own ``is_local`` flag rather than a name list, so
    a community provider that sets the flag is treated correctly
    without a change here. Falls back to the pre-import name sets below,
    which is the answer the router needs before any provider class has
    been constructed.

    The name is canonicalised first: the registry keys are
    ``faster_whisper`` while the spelling an operator is told to use is
    ``faster-whisper``, and a raw ``registry.get(name)`` misses on that.
    """
    canon = canonical_provider(kind, name)
    if kind == "stt":
        from voice.stt_providers import _PROVIDER_REGISTRY as registry
        fallback = LOCAL_STT_PROVIDERS
    elif kind == "tts":
        from voice.tts_providers import _PROVIDER_REGISTRY as registry
        fallback = LOCAL_TTS_PROVIDERS
    else:
        return False
    cls = registry.get(canon)
    if cls is not None:
        return bool(getattr(cls, "is_local", False))
    return canon in fallback


#: Names that are local engines even before their module is imported.
#: The router needs this to decide whether a credential is required at
#: all, and that decision happens before any provider is constructed.
LOCAL_STT_PROVIDERS = frozenset({"whispercpp", "faster_whisper"})
LOCAL_TTS_PROVIDERS = frozenset({"macos_say", "piper"})

# One engine, several spellings, and they were not all understood in the
# same place. This module spells it `faster_whisper`;
# `perception/audio_pipeline.py` spells it `faster-whisper`, and so does
# the documented `FERAL_STT_PROVIDER` value that an operator actually
# types. `requires_credential` compared the raw string, so the spelling
# a user is told to use answered "needs an API key", which made
# `voice.router._is_local_provider('stt', 'faster-whisper')` False and
# turned off the privacy refusal that exists to stop a local-only
# session falling back to a cloud service. Someone who explicitly chose
# local STT could have their audio sent to a cloud provider with no
# warning.
#
# Aliases resolve to the canonical id; hyphens and underscores are the
# same character for this purpose.
_PROVIDER_ALIASES = {
    "stt": {
        "local": "faster_whisper",
        "whisper_local": "faster_whisper",
        "faster_whisper": "faster_whisper",
        "whispercpp": "whispercpp",
        "whisper_cpp": "whispercpp",
    },
    "tts": {
        "local": "piper",
        "piper": "piper",
        "macos_say": "macos_say",
        "say": "macos_say",
    },
}


def canonical_provider(kind: str, name: str) -> str:
    """Resolve a provider spelling to the id this module uses.

    Unknown names come back normalised but otherwise untouched, so a
    cloud provider is never accidentally rewritten into a local one.
    """
    normalised = (name or "").strip().lower().replace("-", "_")
    return _PROVIDER_ALIASES.get(kind, {}).get(normalised, normalised)


def requires_credential(kind: str, name: str) -> bool:
    """Does selecting *name* mean the operator needs an API key?

    ``False`` for every local engine. The router used to answer this
    with a dict lookup that fell through to Deepgram for anything it
    did not recognise, so choosing a local STT aborted the session
    demanding a ``DEEPGRAM_API_KEY`` that nothing would ever use.
    """
    if kind in ("stt", "tts"):
        return not is_local_provider(kind, name)
    return True
