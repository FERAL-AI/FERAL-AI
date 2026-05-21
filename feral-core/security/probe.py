"""
FERAL credential probe — validate provider keys with cheap HTTP calls.

Every integration that needs to know "is this credential actually valid?"
should call :func:`probe` or :func:`probe_all` instead of checking env-var
presence or token JSON shape alone.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import httpx

from config.runtime import ollama_base_url

logger = logging.getLogger("feral.probe")

PROBE_TIMEOUT_SECONDS = 5.0
PROBE_CACHE_TTL_SECONDS = 60.0

ProbeFn = Callable[..., Awaitable["ProbeResult"]]

_PROBE_REGISTRY: dict[str, ProbeFn] = {}
_PROBE_CACHE: dict[str, tuple[float, "ProbeResult"]] = {}


@dataclass(frozen=True)
class ProbeResult:
    provider: str
    ok: bool
    status_code: Optional[int]
    reason: str
    detail: str
    probed_at: float
    latency_ms: float

    def __str__(self) -> str:
        return (
            f"ProbeResult(provider={self.provider!r}, ok={self.ok}, "
            f"status_code={self.status_code}, reason={self.reason!r}, "
            f"detail={self.detail!r}, latency_ms={self.latency_ms:.1f})"
        )


def register_probe(provider_id: str) -> Callable[[ProbeFn], ProbeFn]:
    """Register a probe implementation for *provider_id*."""

    def decorator(fn: ProbeFn) -> ProbeFn:
        _PROBE_REGISTRY[provider_id] = fn
        return fn

    return decorator


def registered_probe_ids() -> list[str]:
    return sorted(_PROBE_REGISTRY.keys())


def clear_probe_cache() -> None:
    """Reset the in-process probe cache (tests)."""
    _PROBE_CACHE.clear()


def _resolve_env_or_vault(
    vault: Any,
    env_keys: tuple[str, ...],
) -> Optional[str]:
    for env_key in env_keys:
        env_val = os.environ.get(env_key)
        if env_val is not None and str(env_val).strip():
            return str(env_val).strip()
    if vault is None:
        return None
    get = getattr(vault, "get_credential", None)
    if not callable(get):
        return None
    for env_key in env_keys:
        val = get(env_key)
        if val:
            return str(val).strip()
    return None


def _resolve_oauth_access_token(
    vault: Any,
    provider_id: str,
    *,
    env_token_keys: tuple[str, ...] = (),
) -> Optional[str]:
    for env_key in env_token_keys:
        env_val = os.environ.get(env_key)
        if env_val is not None and str(env_val).strip():
            return str(env_val).strip()
    if vault is None:
        return None
    raw = vault.get_credential(f"oauth_{provider_id}")
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        return None
    token = blob.get("access_token")
    return str(token).strip() if token else None


def _lmstudio_base_url() -> str:
    base = (
        os.environ.get("FERAL_LLM_BASE_URL")
        or os.environ.get("LMSTUDIO_BASE_URL")
        or "http://localhost:1234/v1"
    )
    return base.rstrip("/")


async def _http_probe(
    provider_id: str,
    *,
    method: str,
    url: str,
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, str]] = None,
    ok_statuses: tuple[int, ...] = (200,),
    no_key_reason: str = "no_key",
    had_key: bool = True,
) -> ProbeResult:
    started = time.monotonic()
    probed_at = time.time()
    if not had_key:
        return ProbeResult(
            provider=provider_id,
            ok=False,
            status_code=None,
            reason=no_key_reason,
            detail="credential not configured",
            probed_at=probed_at,
            latency_ms=0.0,
        )

    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.request(method, url, headers=headers, params=params)
    except httpx.TimeoutException:
        latency_ms = (time.monotonic() - started) * 1000.0
        return ProbeResult(
            provider=provider_id,
            ok=False,
            status_code=None,
            reason="timeout",
            detail=f"request timed out after {PROBE_TIMEOUT_SECONDS}s",
            probed_at=probed_at,
            latency_ms=latency_ms,
        )
    except httpx.HTTPError as exc:
        latency_ms = (time.monotonic() - started) * 1000.0
        return ProbeResult(
            provider=provider_id,
            ok=False,
            status_code=None,
            reason="network_error",
            detail=str(exc),
            probed_at=probed_at,
            latency_ms=latency_ms,
        )

    latency_ms = (time.monotonic() - started) * 1000.0
    status = response.status_code
    ok = status in ok_statuses
    if ok:
        reason = "ok"
        detail = response.reason_phrase or "ok"
    elif status == 401:
        reason = "unauthorized"
        detail = response.text[:200] if response.text else "401 Unauthorized"
    elif status == 403:
        reason = "forbidden"
        detail = response.text[:200] if response.text else "403 Forbidden"
    else:
        reason = "http_error"
        detail = response.text[:200] if response.text else (response.reason_phrase or "")

    return ProbeResult(
        provider=provider_id,
        ok=ok,
        status_code=status,
        reason=reason,
        detail=detail,
        probed_at=probed_at,
        latency_ms=latency_ms,
    )


@register_probe("openai")
async def _probe_openai(*, vault=None, **_kwargs: Any) -> ProbeResult:
    api_key = _resolve_env_or_vault(vault, ("OPENAI_API_KEY",))
    return await _http_probe(
        "openai",
        method="GET",
        url="https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"} if api_key else None,
        had_key=bool(api_key),
    )


@register_probe("anthropic")
async def _probe_anthropic(*, vault=None, **_kwargs: Any) -> ProbeResult:
    api_key = _resolve_env_or_vault(vault, ("ANTHROPIC_API_KEY",))
    headers = None
    if api_key:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    return await _http_probe(
        "anthropic",
        method="GET",
        url="https://api.anthropic.com/v1/models",
        headers=headers,
        had_key=bool(api_key),
    )


@register_probe("gemini")
async def _probe_gemini(*, vault=None, **_kwargs: Any) -> ProbeResult:
    api_key = _resolve_env_or_vault(vault, ("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    return await _http_probe(
        "gemini",
        method="GET",
        url="https://generativelanguage.googleapis.com/v1/models",
        params={"key": api_key} if api_key else None,
        had_key=bool(api_key),
    )


@register_probe("openrouter")
async def _probe_openrouter(*, vault=None, **_kwargs: Any) -> ProbeResult:
    api_key = _resolve_env_or_vault(vault, ("OPENROUTER_API_KEY",))
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    return await _http_probe(
        "openrouter",
        method="GET",
        url="https://openrouter.ai/api/v1/models",
        headers=headers,
        had_key=bool(api_key),
    )


@register_probe("deepseek")
async def _probe_deepseek(*, vault=None, **_kwargs: Any) -> ProbeResult:
    api_key = _resolve_env_or_vault(vault, ("DEEPSEEK_API_KEY",))
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    return await _http_probe(
        "deepseek",
        method="GET",
        url="https://api.deepseek.com/v1/models",
        headers=headers,
        had_key=bool(api_key),
    )


@register_probe("groq")
async def _probe_groq(*, vault=None, **_kwargs: Any) -> ProbeResult:
    api_key = _resolve_env_or_vault(vault, ("GROQ_API_KEY",))
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    return await _http_probe(
        "groq",
        method="GET",
        url="https://api.groq.com/openai/v1/models",
        headers=headers,
        had_key=bool(api_key),
    )


@register_probe("ollama")
async def _probe_ollama(*, vault=None, **_kwargs: Any) -> ProbeResult:
    base = ollama_base_url().rstrip("/")
    return await _http_probe(
        "ollama",
        method="GET",
        url=f"{base}/api/tags",
        had_key=True,
    )


@register_probe("lmstudio")
async def _probe_lmstudio(*, vault=None, **_kwargs: Any) -> ProbeResult:
    base = _lmstudio_base_url()
    return await _http_probe(
        "lmstudio",
        method="GET",
        url=f"{base}/models",
        had_key=True,
    )


@register_probe("bedrock")
async def _probe_bedrock(*, vault=None, **_kwargs: Any) -> ProbeResult:
    access_key = _resolve_env_or_vault(vault, ("AWS_ACCESS_KEY_ID",))
    secret_key = _resolve_env_or_vault(vault, ("AWS_SECRET_ACCESS_KEY",))
    session_token = _resolve_env_or_vault(vault, ("AWS_SESSION_TOKEN",))
    if not access_key or not secret_key:
        return ProbeResult(
            provider="bedrock",
            ok=False,
            status_code=None,
            reason="no_key",
            detail="AWS credentials not configured",
            probed_at=time.time(),
            latency_ms=0.0,
        )

    started = time.monotonic()
    probed_at = time.time()
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        session_kwargs: dict[str, str] = {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
        }
        if session_token:
            session_kwargs["aws_session_token"] = session_token
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        sts = boto3.client("sts", region_name=region, **session_kwargs)
        sts.get_caller_identity()
    except ImportError:
        return ProbeResult(
            provider="bedrock",
            ok=False,
            status_code=None,
            reason="dependency_missing",
            detail="boto3 is not installed",
            probed_at=probed_at,
            latency_ms=(time.monotonic() - started) * 1000.0,
        )
    except (BotoCoreError, ClientError) as exc:
        latency_ms = (time.monotonic() - started) * 1000.0
        code = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        return ProbeResult(
            provider="bedrock",
            ok=False,
            status_code=code,
            reason="unauthorized" if code == 403 else "aws_error",
            detail=str(exc),
            probed_at=probed_at,
            latency_ms=latency_ms,
        )
    except Exception as exc:
        latency_ms = (time.monotonic() - started) * 1000.0
        return ProbeResult(
            provider="bedrock",
            ok=False,
            status_code=None,
            reason="aws_error",
            detail=str(exc),
            probed_at=probed_at,
            latency_ms=latency_ms,
        )

    latency_ms = (time.monotonic() - started) * 1000.0
    return ProbeResult(
        provider="bedrock",
        ok=True,
        status_code=200,
        reason="ok",
        detail="sts GetCallerIdentity succeeded",
        probed_at=probed_at,
        latency_ms=latency_ms,
    )


@register_probe("google")
async def _probe_google(*, vault=None, **_kwargs: Any) -> ProbeResult:
    token = _resolve_oauth_access_token(vault, "google")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return await _http_probe(
        "google",
        method="GET",
        url="https://www.googleapis.com/oauth2/v3/userinfo",
        headers=headers,
        had_key=bool(token),
    )


@register_probe("notion")
async def _probe_notion(*, vault=None, **_kwargs: Any) -> ProbeResult:
    token = _resolve_oauth_access_token(
        vault,
        "notion",
        env_token_keys=("NOTION_TOKEN",),
    ) or _resolve_env_or_vault(vault, ("NOTION_TOKEN",))
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
    } if token else None
    return await _http_probe(
        "notion",
        method="GET",
        url="https://api.notion.com/v1/users/me",
        headers=headers,
        had_key=bool(token),
    )


@register_probe("spotify")
async def _probe_spotify(*, vault=None, **_kwargs: Any) -> ProbeResult:
    token = _resolve_oauth_access_token(vault, "spotify")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return await _http_probe(
        "spotify",
        method="GET",
        url="https://api.spotify.com/v1/me",
        headers=headers,
        had_key=bool(token),
    )


@register_probe("whoop")
async def _probe_whoop(*, vault=None, **_kwargs: Any) -> ProbeResult:
    token = _resolve_oauth_access_token(
        vault,
        "whoop",
        env_token_keys=("FERAL_WHOOP_TOKEN",),
    )
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return await _http_probe(
        "whoop",
        method="GET",
        url="https://api.prod.whoop.com/developer/v1/user/profile/basic",
        headers=headers,
        had_key=bool(token),
    )


@register_probe("oura")
async def _probe_oura(*, vault=None, **_kwargs: Any) -> ProbeResult:
    token = _resolve_oauth_access_token(
        vault,
        "oura",
        env_token_keys=("FERAL_OURA_TOKEN",),
    )
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return await _http_probe(
        "oura",
        method="GET",
        url="https://api.ouraring.com/v2/usercollection/personal_info",
        headers=headers,
        had_key=bool(token),
    )


async def probe(
    provider_id: str,
    *,
    vault: Any = None,
    force: bool = False,
) -> ProbeResult:
    """Probe a single provider's credential validity."""
    provider_id = provider_id.lower().strip()
    fn = _PROBE_REGISTRY.get(provider_id)
    if fn is None:
        now = time.time()
        return ProbeResult(
            provider=provider_id,
            ok=False,
            status_code=None,
            reason="unknown_provider",
            detail=f"no probe registered for {provider_id!r}",
            probed_at=now,
            latency_ms=0.0,
        )

    if not force:
        cached = _PROBE_CACHE.get(provider_id)
        if cached is not None:
            cached_at, cached_result = cached
            if (time.time() - cached_at) < PROBE_CACHE_TTL_SECONDS:
                return cached_result

    if vault is None:
        try:
            from security.vault import get_vault

            vault = get_vault()
        except Exception as exc:
            logger.debug("probe: vault unavailable for %s: %s", provider_id, exc)

    result = await fn(vault=vault)
    _PROBE_CACHE[provider_id] = (time.time(), result)
    return result


async def probe_all(
    vault: Any = None,
    *,
    force: bool = False,
) -> dict[str, ProbeResult]:
    """Run every registered probe (doctor / status surfaces)."""
    results: dict[str, ProbeResult] = {}
    for provider_id in registered_probe_ids():
        results[provider_id] = await probe(provider_id, vault=vault, force=force)
    return results
