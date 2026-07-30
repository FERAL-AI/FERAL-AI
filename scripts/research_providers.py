#!/usr/bin/env python3
"""Refresh ``feral-core/providers/model_catalog.json`` from every
provider's public model-discovery endpoint.

Runs daily via ``.github/workflows/provider-research.yml``. Writes the
updated catalog in place. The workflow then opens a PR iff the file
changed.

This is deliberately a small, dependency-free script:

* Only uses ``urllib`` (std-lib) so it works in bare GitHub Actions.
* Requires ``*_API_KEY`` env vars for providers that gate model
  discovery behind auth.
* Never removes a provider entry; only updates ``models`` / ``pricing``
  / ``capabilities`` + the ``last_fetched`` timestamp.

Fail-loud contract
------------------
Between 2026-04 and 2026-07 this job was green every single day while
doing nothing: the workflow referenced eight ``PROVIDER_RESEARCH_*_KEY``
secrets that were never created, every provider refresher returned
``None`` on the missing key, ``changes`` came back empty, and the job
printed "no provider model lists changed" and exited 0. The catalog
sat at ``last_fetched: 2026-04-26`` for three months while
``cost/pricing.py`` — the single source of truth for every budget cap
in the system — served April prices.

``--require-keys`` (which CI now passes) makes that failure mode
impossible: a missing key or a failed fetch for any pollable provider
is a hard, non-zero exit with the offending env var names printed.
``tests/test_provider_catalog_freshness.py`` is the second half of the
guard — it turns a stale ``last_fetched`` into a red build even if the
workflow itself stops running.

Usage
-----
    python scripts/research_providers.py                   # rewrite in place
    python scripts/research_providers.py --dry-run         # print, don't write
    python scripts/research_providers.py --require-keys    # fail on missing keys
    python scripts/research_providers.py --only openrouter # single provider
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "feral-core" / "providers" / "model_catalog.json"


# Providers whose model-discovery endpoint speaks OpenAI's
# ``{"data": [{"id": ...}]}`` shape. Value = name of the env var that
# holds the API key.
#
# ``qwen`` was missing here until 2026-07-30 despite being a runtime
# provider in ``agents/llm_provider.py::_PROVIDER_REGISTRY`` — its model
# list could only ever go stale. ``mistral`` was added at the same time
# (``https://api.mistral.ai/v1/models`` is OpenAI-shaped).
OPENAI_SHAPE: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "together": "TOGETHER_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# Endpoints that serve the model list (and, for OpenRouter, the full
# per-route price sheet) without an Authorization header. A missing key
# for these is NOT a ``--require-keys`` violation.
AUTH_OPTIONAL: frozenset[str] = frozenset({"openrouter"})

# Providers with no machine-readable discovery endpoint at all. Their
# ``models`` arrays are hand-curated and this script must not touch
# them. Recorded here (rather than by omission) so the reason is
# reviewable:
#
#   xai      — https://api.x.ai/v1 is OpenAI-compatible for chat but
#              does not serve a usable public model index.
#   minimax  — no public model-list endpoint.
#   zai      — /api/paas/v4 is not an OpenAI-shaped model index.
#   ollama   — per-host, fetched at runtime via /api/tags.
#   lmstudio — per-host, fetched at runtime via /v1/models.
#   bedrock  — inventory lives in providers/bedrock_models.json.
#   fireworks— account-scoped model index; no shared truth to poll.
CURATED_ONLY: frozenset[str] = frozenset(
    {"xai", "minimax", "zai", "ollama", "lmstudio", "bedrock", "fireworks"}
)

_ANTHROPIC_VERSION = "2023-06-01"


class RefreshError(RuntimeError):
    """A pollable provider could not be refreshed."""


def _fetch(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_or_raise(pid: str, url: str, headers: dict, *, strict: bool) -> Optional[dict]:
    """Fetch *url*; return ``None`` on failure unless *strict*.

    In strict mode (``--require-keys``) a transport / HTTP failure is
    raised as :class:`RefreshError` so the CI job goes red instead of
    silently reporting "nothing changed".
    """
    try:
        return _fetch(url, headers)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        if strict:
            raise RefreshError(f"{pid}: fetch failed for {url}: {exc}") from exc
        print(f"  [{pid}] fetch failed: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────
# Per-shape refreshers. Each returns (models, pricing, capabilities);
# any element may be ``None`` meaning "this provider exposes nothing
# for that facet, leave the curated value alone".
# ─────────────────────────────────────────────────────────────────────


def _refresh_openai_shape(
    pid: str, env_key: str, entry: dict, *, strict: bool
) -> tuple[Optional[list[str]], Optional[dict], Optional[dict]]:
    endpoint = entry.get("endpoint")
    token = os.environ.get(env_key)
    if not endpoint:
        return None, None, None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if not token and pid not in AUTH_OPTIONAL:
        return None, None, None
    data = _fetch_or_raise(pid, endpoint, headers, strict=strict)
    if data is None:
        return None, None, None
    rows = data.get("data") or []
    ids = sorted({m.get("id") for m in rows if m.get("id")})
    pricing = _extract_openrouter_pricing(rows) if pid == "openrouter" else None
    return (ids or None), pricing, None


def _to_per_1k(per_token: Any) -> Optional[float]:
    """Convert a provider's per-token price string to $/1k tokens.

    ``cost/pricing.py`` stores every rate as dollars per 1,000 tokens.
    OpenRouter quotes dollars per single token as a decimal string
    (``"0.000003"``). Returns ``None`` for unusable values so a bad row
    is skipped rather than silently priced at $0 — a 0/0 rate makes the
    budget gate think every call is free, which is the exact opposite
    of safe.
    """
    try:
        value = float(per_token)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return round(value * 1000.0, 10)


def _extract_openrouter_pricing(rows: list[dict]) -> dict[str, dict[str, float]]:
    """Build a ``{route_id: {input, output}}`` blob from /v1/models.

    OpenRouter is the one provider that publishes a machine-readable
    price sheet alongside its model index, and it does so without auth.
    Before 2026-07-30 the catalog carried 355 OpenRouter routes and
    ZERO prices, so every OpenRouter turn fell through to
    ``cost.pricing._FALLBACK_PER_1K`` and cost tracking was blind.
    """
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        mid = row.get("id")
        pricing = row.get("pricing")
        if not mid or not isinstance(pricing, dict):
            continue
        inp = _to_per_1k(pricing.get("prompt"))
        outp = _to_per_1k(pricing.get("completion"))
        if inp is None or outp is None:
            continue
        rates: dict[str, float] = {"input": inp, "output": outp}
        cache_read = _to_per_1k(pricing.get("input_cache_read"))
        if cache_read is not None and cache_read > 0:
            rates["cache_read"] = cache_read
        out[mid] = rates
    return out


def _refresh_gemini(
    entry: dict, *, strict: bool
) -> tuple[Optional[list[str]], Optional[dict], Optional[dict]]:
    endpoint = entry.get("endpoint")
    token = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not endpoint or not token:
        return None, None, None
    data = _fetch_or_raise("gemini", f"{endpoint}?key={token}", {}, strict=strict)
    if data is None:
        return None, None, None
    models = data.get("models") or []
    ids = sorted({m["name"].split("/")[-1] for m in models if m.get("name")})
    caps: dict[str, dict[str, Any]] = {}
    for m in models:
        name = (m.get("name") or "").split("/")[-1]
        if not name:
            continue
        entry_caps: dict[str, Any] = {}
        if isinstance(m.get("inputTokenLimit"), int):
            entry_caps["max_input_tokens"] = m["inputTokenLimit"]
        if isinstance(m.get("outputTokenLimit"), int):
            entry_caps["max_tokens"] = m["outputTokenLimit"]
        if entry_caps:
            caps[name] = entry_caps
    return (ids or None), None, (caps or None)


def _cap_flag(node: Any) -> bool:
    """Read a capability leaf that may be a bool or ``{"supported": bool}``."""
    if isinstance(node, dict):
        return bool(node.get("supported"))
    return bool(node)


def _refresh_anthropic(
    entry: dict, *, strict: bool
) -> tuple[Optional[list[str]], Optional[dict], Optional[dict]]:
    """Poll ``GET https://api.anthropic.com/v1/models``.

    Until 2026-07-30 this script carried the comment "Anthropic has no
    public /v1/models. Leave the hand-curated list alone." That was out
    of date. The endpoint exists, is documented, and is the *richest*
    catalog endpoint of any provider we poll: beyond ``id`` /
    ``display_name`` / ``created_at`` it returns ``max_input_tokens``,
    ``max_tokens``, and a ``capabilities`` object carrying booleans for
    image_input / pdf_input / structured_outputs / code_execution /
    citations / batch, the supported ``effort`` levels
    (low/medium/high/xhigh/max) and ``thinking.types``
    (adaptive/enabled).

    That capability payload is load-bearing, not decoration:
    ``providers/anthropic_provider.py`` derives its adaptive- vs
    extended-thinking split (and therefore whether it may send
    ``temperature``) from it. Auth is ``X-Api-Key`` plus the
    ``anthropic-version`` header — NOT a bearer token.
    """
    endpoint = entry.get("endpoint")
    token = os.environ.get("ANTHROPIC_API_KEY")
    if not endpoint or not token:
        return None, None, None
    headers = {"X-Api-Key": token, "anthropic-version": _ANTHROPIC_VERSION}
    ids: list[str] = []
    caps: dict[str, dict[str, Any]] = {}
    cursor: Optional[str] = None
    for _ in range(20):  # safety cap: 20 pages of 100
        url = f"{endpoint}?limit=100" + (f"&after_id={cursor}" if cursor else "")
        body = _fetch_or_raise("anthropic", url, headers, strict=strict)
        if body is None:
            return None, None, None
        for row in body.get("data") or []:
            mid = row.get("id")
            if not mid:
                continue
            ids.append(mid)
            caps[mid] = _anthropic_capabilities(row)
        if not body.get("has_more"):
            break
        cursor = body.get("last_id")
        if not cursor:
            break
    return (ids or None), None, (caps or None)


def _anthropic_capabilities(row: dict) -> dict[str, Any]:
    """Project one ``/v1/models`` row onto the catalog capability shape."""
    raw = row.get("capabilities") or {}
    thinking = raw.get("thinking") or {}
    types = thinking.get("types") or {}
    effort = raw.get("effort") or {}
    out: dict[str, Any] = {
        "thinking": {
            "adaptive": _cap_flag(types.get("adaptive")),
            "enabled": _cap_flag(types.get("enabled")),
        },
        "image_input": _cap_flag(raw.get("image_input")),
        "pdf_input": _cap_flag(raw.get("pdf_input")),
        "structured_outputs": _cap_flag(raw.get("structured_outputs")),
        "code_execution": _cap_flag(raw.get("code_execution")),
        "citations": _cap_flag(raw.get("citations")),
        "batch": _cap_flag(raw.get("batch")),
    }
    levels = [lv for lv in ("low", "medium", "high", "xhigh", "max") if _cap_flag(effort.get(lv))]
    if levels:
        out["effort"] = levels
    if isinstance(row.get("max_input_tokens"), int):
        out["max_input_tokens"] = row["max_input_tokens"]
    if isinstance(row.get("max_tokens"), int):
        out["max_tokens"] = row["max_tokens"]
    # Sampling params (temperature / top_p / top_k) are rejected with a
    # 400 on every adaptive-thinking Claude (4.7 and later). The models
    # endpoint does not carry an explicit flag, so derive it: adaptive
    # thinking is the observable proxy the adapter already keys on.
    out["sampling_params"] = not out["thinking"]["adaptive"]
    return out


def missing_keys() -> list[tuple[str, str]]:
    """Return ``[(provider_id, env_var)]`` for every pollable provider
    whose key is absent from the environment."""
    missing: list[tuple[str, str]] = []
    for pid, env_key in OPENAI_SHAPE.items():
        if pid in AUTH_OPTIONAL:
            continue
        if not os.environ.get(env_key):
            missing.append((pid, env_key))
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        missing.append(("gemini", "GEMINI_API_KEY"))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        missing.append(("anthropic", "ANTHROPIC_API_KEY"))
    return sorted(missing)


def _apply(
    entry: dict,
    pid: str,
    models: Optional[list[str]],
    pricing: Optional[dict],
    capabilities: Optional[dict],
    changes: list[str],
) -> None:
    """Merge one provider's refreshed facets into its catalog entry."""
    if models and models != entry.get("models", []):
        entry["models"] = models
        changes.append(f"{pid}: {len(models)} models")
    if pricing:
        current = entry.setdefault("pricing", {})
        # Preserve ``_pricing_meta`` and any hand-curated entry the live
        # sheet does not cover; overwrite everything the provider
        # actually quotes.
        updated = {k: v for k, v in current.items() if k.startswith("_")}
        updated.update({k: v for k, v in current.items() if not k.startswith("_")})
        updated.update(pricing)
        if updated != current:
            entry["pricing"] = updated
            changes.append(f"{pid}: {len(pricing)} priced models")
    if capabilities:
        current_caps = entry.get("capabilities") or {}
        merged = dict(current_caps)
        merged.update(capabilities)
        if merged != current_caps:
            entry["capabilities"] = merged
            changes.append(f"{pid}: {len(capabilities)} capability records")


def refresh(
    catalog: dict, *, strict: bool = False, only: Optional[str] = None
) -> tuple[dict, list[str]]:
    """Return ``(new_catalog, list_of_changes_by_provider)``."""
    providers = catalog.setdefault("providers", {})
    changes: list[str] = []

    def wanted(pid: str) -> bool:
        return only is None or only == pid

    for pid, env_key in OPENAI_SHAPE.items():
        if not wanted(pid):
            continue
        entry = providers.setdefault(pid, {"models": [], "pricing": {}})
        _apply(entry, pid, *_refresh_openai_shape(pid, env_key, entry, strict=strict), changes)

    for pid, fn in (
        ("gemini", _refresh_gemini),
        ("anthropic", _refresh_anthropic),
    ):
        if not wanted(pid):
            continue
        entry = providers.setdefault(pid, {"models": [], "pricing": {}})
        _apply(entry, pid, *fn(entry, strict=strict), changes)  # type: ignore[operator]

    if changes:
        catalog["last_fetched"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    return catalog, changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print changes but don't write")
    ap.add_argument(
        "--require-keys",
        action="store_true",
        help=(
            "fail loudly (exit 1) when a pollable provider has no API key in "
            "the environment, or when its fetch fails. CI passes this so a "
            "silently-unconfigured job can never report success."
        ),
    )
    ap.add_argument("--only", default=None, help="refresh a single provider id")
    args = ap.parse_args()

    if not CATALOG_PATH.exists():
        print(f"! {CATALOG_PATH} missing; run once to bootstrap.")
        return 1

    if args.require_keys:
        missing = [m for m in missing_keys() if args.only is None or m[0] == args.only]
        if missing:
            print("! provider research cannot run: missing API keys", file=sys.stderr)
            for pid, env_key in missing:
                print(f"    {pid:<12} needs ${env_key}", file=sys.stderr)
            print(
                "\n  These are supplied by the GitHub Actions secrets named in\n"
                "  .github/workflows/provider-research.yml. If this fires in CI,\n"
                "  the repository secrets do not exist — create them. Exiting 1\n"
                "  rather than reporting a green no-op run.",
                file=sys.stderr,
            )
            return 1

    catalog = json.loads(CATALOG_PATH.read_text())
    try:
        new_catalog, changes = refresh(catalog, strict=args.require_keys, only=args.only)
    except RefreshError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 1

    if not changes:
        print("no provider model lists changed")
        return 0

    print("changes:")
    for change in changes:
        print(f"  + {change}")

    if args.dry_run:
        return 0

    CATALOG_PATH.write_text(json.dumps(new_catalog, indent=2) + "\n")
    print(f"wrote {CATALOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
