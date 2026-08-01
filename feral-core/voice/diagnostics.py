"""Voice failure diagnosis: what is wrong, and what to do about it.

FERAL already had the honest-degradation plumbing --
``VoiceRouter._emit_voice_status``, ``emit_unavailable``, and a
``voice_status state=degraded`` frame. What none of it carried was an
EXPLANATION. A user whose voice stopped working got "Voice unavailable"
and a machine tag like ``openai_realtime_auth``, and had to read the
source to learn that their API key had expired.

This module supplies the missing half. It reuses
``security/probe.py``'s voice provider catalogue and probe registry
rather than standing up a parallel one, so a provider added there is
diagnosable here without a second edit.

Two rules it does not break
===========================

**Never fabricate a diagnosis.** Probe reasons map to causes through an
explicit table. Anything not in that table becomes
:data:`CAUSE_UNKNOWN`, and the summary says the cause could not be
determined. A plausible-sounding guess is worse than "I do not know",
because the user acts on it and loses an afternoon.

**Never report a silent cloud fallback as success.** Someone who chose
local engines chose them so their voice does not leave the machine.
Falling through to a cloud vendor is the precise outcome they were
preventing. That is reported as a failure with
``privacy_downgrade=True``, never as "degraded but working".

Engine honesty
==============
:data:`ENGINE_CAVEATS` records what has and has not actually been
verified to run in this repository. ``macos_say`` is verified working.
Piper, whisper.cpp and faster-whisper are not, and one of them has a
known defect that makes a fresh install fail every session. A "ready"
probe result for those engines means "importable and the weights are on
disk", which is a weaker claim than "works", and the caveat travels with
every finding so nothing downstream can quietly upgrade it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

logger = logging.getLogger("feral.voice.diagnostics")

# ---------------------------------------------------------------------
# Causes
# ---------------------------------------------------------------------

CAUSE_OK = "ok"
CAUSE_MUTED = "muted"
CAUSE_NO_API_KEY = "no_api_key"
CAUSE_KEY_REJECTED = "key_rejected"
CAUSE_QUOTA_EXHAUSTED = "quota_exhausted"
CAUSE_PROVIDER_UNREACHABLE = "provider_unreachable"
CAUSE_ENGINE_NOT_INSTALLED = "engine_not_installed"
CAUSE_MODEL_WEIGHTS_MISSING = "model_weights_missing"
CAUSE_ENGINE_BROKEN = "engine_broken"
CAUSE_NO_MIC_PERMISSION = "no_microphone_permission"
CAUSE_ALL_PROVIDERS_EXHAUSTED = "all_providers_exhausted"
CAUSE_PRIVACY_DOWNGRADE = "privacy_downgrade"
CAUSE_UNKNOWN = "unknown"

ALL_CAUSES: tuple[str, ...] = (
    CAUSE_OK,
    CAUSE_MUTED,
    CAUSE_NO_API_KEY,
    CAUSE_KEY_REJECTED,
    CAUSE_QUOTA_EXHAUSTED,
    CAUSE_PROVIDER_UNREACHABLE,
    CAUSE_ENGINE_NOT_INSTALLED,
    CAUSE_MODEL_WEIGHTS_MISSING,
    CAUSE_ENGINE_BROKEN,
    CAUSE_NO_MIC_PERMISSION,
    CAUSE_ALL_PROVIDERS_EXHAUSTED,
    CAUSE_PRIVACY_DOWNGRADE,
    CAUSE_UNKNOWN,
)

#: Client-reported fault codes we recognise. ``BrowserNode.startMic``
#: raises ``MIC_PERMISSION_DENIED`` / ``MIC_START_FAILED`` /
#: ``MIC_UNAVAILABLE``. Anything else is NOT translated into a
#: microphone diagnosis, because a fault we do not recognise tells us
#: nothing about the microphone.
_MIC_FAULT_CODES = frozenset({
    "MIC_PERMISSION_DENIED", "NotAllowedError", "PermissionDeniedError",
})
_MIC_HARDWARE_FAULT_CODES = frozenset({"MIC_UNAVAILABLE", "MIC_START_FAILED"})


# ---------------------------------------------------------------------
# Engine caveats -- verified vs merely importable
# ---------------------------------------------------------------------
#
# Every entry here is a statement about THIS repository, not about the
# upstream project. "Never verified" means no test or session in this
# tree has observed the engine produce output, so a green probe is
# evidence of installation, not of function. The diagnostic must not
# imply otherwise; see ``test_a_ready_probe_still_reports_the_caveat``.

ENGINE_CAVEATS: dict[str, str] = {
    "piper": (
        "Piper synthesis has never been verified in this repository. The "
        "macOS wheels abort on a hardcoded CI espeak data path, so a "
        "'ready' result here means the package imports and the voice "
        "weights are on disk, not that it can produce audio. Use "
        "macos_say on macOS, which is verified working."
    ),
    "whispercpp": (
        "whisper.cpp has never completed a real transcription in this "
        "repository. A 'ready' result means pywhispercpp imports and the "
        "model file is on disk; the first live utterance is still the "
        "first test."
    ),
    "faster_whisper": (
        "faster-whisper has never completed a real transcription in this "
        "repository, and it carries a known defect: the setup wizard "
        "prints \"faster-whisper downloads its model on first use\" "
        "(cli/setup/steps/voice_preflight.py), but nothing anywhere "
        "passes allow_download=True to FasterWhisperSTTProvider and there "
        "is no ensure_faster_whisper_model() to pre-fetch it. A fresh "
        "install that picks faster-whisper therefore raises out of the "
        "constructor on every session until the model is fetched by hand. "
        "Prefer whispercpp, or download the model yourself first."
    ),
}


# ---------------------------------------------------------------------
# Probe reason -> cause
# ---------------------------------------------------------------------

# Network probes (``security.probe._http_probe``). Keys are the exact
# ``ProbeResult.reason`` values that function produces.
_NETWORK_REASON_CAUSES: dict[str, str] = {
    "no_key": CAUSE_NO_API_KEY,
    "unauthorized": CAUSE_KEY_REJECTED,
    "forbidden": CAUSE_KEY_REJECTED,
    "timeout": CAUSE_PROVIDER_UNREACHABLE,
    "network_error": CAUSE_PROVIDER_UNREACHABLE,
    "dependency_missing": CAUSE_ENGINE_NOT_INSTALLED,
}

# Local engine probes report a single reason (``not_configured``) and
# put the real information in ``detail``, because that detail is the
# engine's own ``*_available()`` string. These are substring probes
# against those strings, in order.
_LOCAL_DETAIL_CAUSES: tuple[tuple[str, str], ...] = (
    ("not installed", CAUSE_ENGINE_NOT_INSTALLED),
    ("not downloaded", CAUSE_MODEL_WEIGHTS_MISSING),
    ("cannot synthesise", CAUSE_ENGINE_BROKEN),
    ("cannot synthesize", CAUSE_ENGINE_BROKEN),
    ("cannot use metal", CAUSE_ENGINE_BROKEN),
)

# Substrings that mean "the account is out of credit / rate limited"
# regardless of which HTTP status the vendor chose for it.
_QUOTA_MARKERS = (
    "insufficient_quota", "quota", "rate limit", "rate_limit",
    "too many requests", "billing",
)


def cause_for_probe(result: Any) -> str:
    """Machine cause tag for a :class:`security.probe.ProbeResult`.

    Returns ``""`` for a successful probe and :data:`CAUSE_UNKNOWN` when
    the reason is not one we have a mapping for. Guessing here is the
    one thing this module must not do.
    """
    if getattr(result, "ok", False):
        return ""
    reason = str(getattr(result, "reason", "") or "").strip().lower()
    detail = str(getattr(result, "detail", "") or "").lower()
    status = getattr(result, "status_code", None)

    if reason == "http_error":
        if status == 429 or any(m in detail for m in _QUOTA_MARKERS):
            return CAUSE_QUOTA_EXHAUSTED
        if status in (401, 403):
            return CAUSE_KEY_REJECTED
        return CAUSE_UNKNOWN

    if reason in _NETWORK_REASON_CAUSES:
        # A vendor can answer "no credit" with a 401-shaped body, so the
        # quota markers get a look in before the coarse mapping.
        if any(m in detail for m in _QUOTA_MARKERS):
            return CAUSE_QUOTA_EXHAUSTED
        return _NETWORK_REASON_CAUSES[reason]

    if reason == "not_configured":
        for needle, cause in _LOCAL_DETAIL_CAUSES:
            if needle in detail:
                return cause
        return CAUSE_UNKNOWN

    return CAUSE_UNKNOWN


# ---------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------

def _catalogue_entry(provider: str) -> dict:
    try:
        from security.probe import voice_provider_catalogue

        for entry in voice_provider_catalogue():
            if entry.get("id") == provider:
                return entry
    except Exception:
        logger.debug("voice catalogue lookup failed for %r", provider, exc_info=True)
    return {}


# A voice provider id is not always a vault provider id. OpenAI
# Realtime, OpenAI Whisper and OpenAI TTS are three selectable voice
# providers sharing ONE credential, stored under ``openai``. Telling an
# operator to run ``feral key add --provider openai_realtime`` would
# write a key nothing reads, so the advice has to name the credential,
# not the product.
_CREDENTIAL_FOR_VOICE_PROVIDER: dict[str, tuple[str, str]] = {
    # voice provider id -> (vault provider id, env var)
    "openai_realtime": ("openai", "OPENAI_API_KEY"),
    "openai_whisper": ("openai", "OPENAI_API_KEY"),
    "openai_tts": ("openai", "OPENAI_API_KEY"),
    "openai": ("openai", "OPENAI_API_KEY"),
    "gemini_live": ("gemini", "GEMINI_API_KEY"),
    "gemini": ("gemini", "GEMINI_API_KEY"),
    "deepgram": ("deepgram", "DEEPGRAM_API_KEY"),
    "elevenlabs": ("elevenlabs", "ELEVENLABS_API_KEY"),
    "cartesia": ("cartesia", "CARTESIA_API_KEY"),
    "groq_whisper": ("groq", "GROQ_API_KEY"),
}


def _env_var_hint(provider: str) -> str:
    """The env var an operator would set for ``provider``, or ``""``."""
    return _CREDENTIAL_FOR_VOICE_PROVIDER.get(provider, ("", ""))[1]


def _vault_provider_hint(provider: str) -> str:
    """The vault provider id ``feral key add --provider`` wants."""
    return _CREDENTIAL_FOR_VOICE_PROVIDER.get(provider, ("", ""))[0]


def recommendation_for(cause: str, provider: str = "") -> str:
    """A concrete next step for ``cause``. Never empty, never a guess."""
    name = provider or "the voice provider"
    env = _env_var_hint(provider)
    vault_id = _vault_provider_hint(provider)
    shared = (
        f" ({name} shares the {vault_id} credential with chat.)"
        if vault_id and vault_id != provider else ""
    )
    key_hint = (
        f"Set {env}, or run `feral key add --provider {vault_id} "
        f"--set-active`.{shared}"
        if env else
        f"Add a credential for {name} with `feral key add`."
    )
    return {
        CAUSE_OK: "",
        CAUSE_MUTED: (
            "The microphone is muted. Unmute it in the voice panel; nothing "
            "is reaching the brain until you do."
        ),
        CAUSE_NO_API_KEY: f"No API key is configured for {name}. {key_hint}",
        CAUSE_KEY_REJECTED: (
            f"{name} rejected the credential. The key is wrong, revoked or "
            f"expired. Issue a new one and replace it: {key_hint}"
        ),
        CAUSE_QUOTA_EXHAUSTED: (
            f"{name} accepted the key but refused the request for quota or "
            "rate-limit reasons. Top up or raise the limit on the vendor's "
            "billing page, or pick a different provider in Settings > Voice."
        ),
        CAUSE_PROVIDER_UNREACHABLE: (
            f"{name} could not be reached at all. Check this machine's "
            "network and DNS, and whether the vendor is having an outage. "
            "Nothing in FERAL's configuration will fix a dead route."
        ),
        CAUSE_ENGINE_NOT_INSTALLED: (
            f"The local engine {name} is not installed. Run `feral setup` "
            "and choose local voice, which installs the optional extra and "
            "downloads the weights."
        ),
        CAUSE_MODEL_WEIGHTS_MISSING: (
            f"{name} is installed but its model weights are not on disk. "
            "Run `feral setup` and choose local voice to download them. "
            "FERAL never downloads model weights as a side effect of a "
            "session, so this will not fix itself."
        ),
        CAUSE_ENGINE_BROKEN: (
            f"{name} is installed and its weights are present, but it "
            "cannot actually run on this machine. Read the detail below "
            "for the specific failure, and pick a different engine in "
            "Settings > Voice in the meantime."
        ),
        CAUSE_NO_MIC_PERMISSION: (
            "The client has no microphone permission, so no audio is being "
            "captured. Grant microphone access to this site or app and "
            "start the voice session again. No provider change will help."
        ),
        CAUSE_ALL_PROVIDERS_EXHAUSTED: (
            "Every provider in the fallback chain is down, each for its own "
            "reason. Fix whichever one is cheapest for you from the list "
            "above, or add a working local engine so the chain has an end "
            "that does not depend on a vendor."
        ),
        CAUSE_PRIVACY_DOWNGRADE: (
            "You chose local voice, and the only providers that can serve "
            "this session are cloud vendors. FERAL has stopped rather than "
            "send your audio off the machine. Fix the local engine, or "
            "change the voice setting deliberately if you accept the "
            "trade-off."
        ),
        CAUSE_UNKNOWN: (
            "The cause could not be determined from the available evidence, "
            "so FERAL is not going to guess at one. Check the brain logs "
            "around the failure (`feral logs`) and the detail below."
        ),
    }.get(cause, (
        "The cause could not be determined from the available evidence. "
        "Check the brain logs (`feral logs`) for the failure."
    ))


# ---------------------------------------------------------------------
# Findings and diagnoses
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderFinding:
    """What one probe told us about one provider."""

    provider: str
    kind: str
    local: bool
    ok: bool
    cause: str
    detail: str
    caveat: str

    def describe(self) -> str:
        if self.ok:
            base = f"{self.provider}: available"
            return f"{base} ({self.caveat})" if self.caveat else base
        head = f"{self.provider}: {self.cause}"
        return f"{head} ({self.detail})" if self.detail else head


def finding_for(result: Any) -> ProviderFinding:
    """Build a :class:`ProviderFinding` from a probe result.

    The caveat is attached whether the probe passed or failed. An
    unverified engine reporting "ready" is the case the caveat exists
    for: without it, "ready" reads as "works".
    """
    provider = str(getattr(result, "provider", "") or "")
    entry = _catalogue_entry(provider)
    return ProviderFinding(
        provider=provider,
        kind=str(entry.get("kind") or ""),
        local=bool(entry.get("local")),
        ok=bool(getattr(result, "ok", False)),
        cause=cause_for_probe(result),
        detail=str(getattr(result, "detail", "") or "")[:300],
        caveat=ENGINE_CAVEATS.get(provider, ""),
    )


@dataclass(frozen=True)
class VoiceDiagnosis:
    """A human-facing answer to "why is voice not working?"."""

    ok: bool
    cause: str
    summary: str
    recommendation: str
    findings: tuple[ProviderFinding, ...] = ()
    fallback_taken: str = ""
    privacy_downgrade: bool = False
    detail: str = ""

    def as_status_meta(self) -> dict:
        """Kwargs for ``models.protocol.VoiceStatusPayload``.

        ``state`` is derived, not stored: available when the first link
        served, degraded when a later one did, unavailable when nothing
        did. A privacy downgrade is never "degraded" -- it is a refusal,
        so it reports unavailable.
        """
        if not self.ok:
            state = "unavailable"
        elif self.fallback_taken:
            state = "degraded"
        else:
            state = "available"
        return {
            "state": state,
            "reason": self.cause,
            "provider": self.findings[0].provider if self.findings else "",
            "fallback_provider": self.fallback_taken,
            "detail": self.detail or "; ".join(f.describe() for f in self.findings),
            "cause": self.cause,
            "summary": self.summary,
            "recommendation": self.recommendation,
            "privacy_downgrade": self.privacy_downgrade,
        }


Prober = Callable[..., Awaitable[Any]]


async def _default_prober(provider_id: str, **kwargs: Any):
    from security.probe import probe

    return await probe(provider_id, **kwargs)


def _client_fault_cause(client_fault: Optional[dict]) -> str:
    """Cause implied by a client-reported fault, or ``""``.

    Only the codes we actually recognise translate. An unrecognised
    fault code is not evidence about the microphone, and turning it into
    a microphone diagnosis would send the user to fix the wrong thing.
    """
    if not client_fault:
        return ""
    code = str(client_fault.get("code") or "").strip()
    detail = str(client_fault.get("detail") or "")
    if code in _MIC_FAULT_CODES or any(c in detail for c in _MIC_FAULT_CODES):
        return CAUSE_NO_MIC_PERMISSION
    if code in _MIC_HARDWARE_FAULT_CODES:
        return CAUSE_ENGINE_BROKEN
    return ""


async def diagnose_chain(
    chain: Sequence[str],
    *,
    prober: Optional[Prober] = None,
    client_fault: Optional[dict] = None,
    chose_local: bool = False,
    force: bool = False,
) -> VoiceDiagnosis:
    """Probe each provider in ``chain`` in order and explain the result.

    ``chain`` holds ``security.probe`` provider ids (``openai_realtime``,
    ``whispercpp``, ...), best link first.

    A client-side fault outranks every provider probe: reporting "all
    providers healthy" while the browser is refusing microphone access
    is technically true and completely useless.
    """
    probe_fn: Prober = prober or _default_prober

    fault_cause = _client_fault_cause(client_fault)
    if fault_cause:
        detail = str((client_fault or {}).get("detail") or "")
        return VoiceDiagnosis(
            ok=False,
            cause=fault_cause,
            summary=(
                "The client is not capturing audio, so nothing is reaching "
                "the brain. Provider health is not the problem here."
            ),
            recommendation=recommendation_for(fault_cause),
            detail=detail[:300],
        )

    findings: list[ProviderFinding] = []
    served_by = ""
    for provider in chain:
        try:
            result = await probe_fn(provider, force=force)
        except Exception as exc:
            logger.debug("probe(%r) raised during diagnosis", provider, exc_info=True)
            findings.append(ProviderFinding(
                provider=provider, kind="", local=False, ok=False,
                cause=CAUSE_UNKNOWN, detail=f"{exc.__class__.__name__}: {exc}"[:300],
                caveat=ENGINE_CAVEATS.get(provider, ""),
            ))
            continue
        found = finding_for(result)
        findings.append(found)
        if found.ok:
            served_by = found.provider
            break

    if not findings:
        return VoiceDiagnosis(
            ok=False,
            cause=CAUSE_UNKNOWN,
            summary=(
                "No voice provider is configured at all, so there was "
                "nothing to check. The cause of the failure is unknown."
            ),
            recommendation=recommendation_for(CAUSE_UNKNOWN),
        )

    fallback_taken = served_by if (served_by and findings[0].provider != served_by) else ""

    if served_by:
        served = next(f for f in findings if f.provider == served_by)
        # A local-voice operator served by a cloud provider is a
        # privacy failure, not a successful fallback. Say so, and do not
        # dress it up as "degraded but working".
        if chose_local and not served.local:
            return VoiceDiagnosis(
                ok=False,
                cause=CAUSE_PRIVACY_DOWNGRADE,
                summary=(
                    f"You chose local voice, but the only provider that can "
                    f"serve this session is {served_by}, which is a cloud "
                    f"service. Using it would send your audio off this "
                    f"machine, so FERAL has not."
                ),
                recommendation=recommendation_for(CAUSE_PRIVACY_DOWNGRADE),
                findings=tuple(findings),
                fallback_taken=served_by,
                privacy_downgrade=True,
            )
        if fallback_taken:
            failed = "; ".join(f.describe() for f in findings if not f.ok)
            summary = (
                f"Voice is working on {served_by}, which is a fallback: "
                f"the providers ahead of it in the chain are down ({failed})."
            )
        else:
            summary = f"Voice is working on {served_by}, the first choice in the chain."
        if served.caveat:
            summary = f"{summary} Note: {served.caveat}"
        return VoiceDiagnosis(
            ok=True,
            cause=CAUSE_OK,
            summary=summary,
            recommendation=(
                recommendation_for(findings[0].cause, findings[0].provider)
                if fallback_taken else ""
            ),
            findings=tuple(findings),
            fallback_taken=fallback_taken,
        )

    # Nothing served.
    if len(findings) == 1:
        only = findings[0]
        return VoiceDiagnosis(
            ok=False,
            cause=only.cause,
            summary=(
                f"Voice is unavailable: {only.describe()}."
                if only.cause != CAUSE_UNKNOWN else
                f"Voice is unavailable and the cause could not be "
                f"determined from {only.provider}'s response "
                f"({only.detail or 'no detail'})."
            ),
            recommendation=recommendation_for(only.cause, only.provider),
            findings=tuple(findings),
        )

    lines = "; ".join(f.describe() for f in findings)
    return VoiceDiagnosis(
        ok=False,
        cause=CAUSE_ALL_PROVIDERS_EXHAUSTED,
        summary=(
            f"Every provider in the fallback chain is down, so there is no "
            f"path left for voice. {lines}."
        ),
        recommendation=(
            f"{recommendation_for(CAUSE_ALL_PROVIDERS_EXHAUSTED)} "
            f"Per provider: {lines}."
        ),
        findings=tuple(findings),
    )
