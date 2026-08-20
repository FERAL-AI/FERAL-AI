"""Every shipped manifest must promise only calls the runtime will run.

The incident this exists for: ``desktop_control__open_app``'s description
told the model to write AppleScript and then enumerated the phrases that
get rejected -- ``do shell script``, ``do script``, ``run script``,
``use framework``. The natural script for "open a YouTube song on Chrome"
uses ``open location``, which ``SandboxPolicy.validate_applescript``
*also* rejects and which the description never named. The model was handed
a list of what is forbidden, wrote something that was not on the list, and
was refused anyway. A permitted path existed the whole time
(``open -a "Google Chrome" <url>`` on the shell allowlist) and no
description pointed at it.

The general shape: **a manifest describes a capability, and a policy, an
allowlist, a validator, or a missing endpoint means the described call
cannot succeed.** The model has no way to discover that except by failing,
and the user experiences it as "FERAL cannot do this".

These tests close that loop in CI. They read the shipped manifests as data,
work out which gate each endpoint actually passes through, and then run the
real gate -- ``SandboxPolicy.validate_applescript``,
``SandboxPolicy.validate_shell_command``,
``SkillExecutor._domain_gate``, ``safety_resolver.resolve_policy`` -- against
the call the description tells the model to write. Nothing here is a
reimplementation of a rule; every assertion calls the production checker.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from models.skill_manifest import SkillManifest
from security.sandbox_policy import SandboxPolicy

MANIFEST_DIR = Path(__file__).resolve().parent.parent / "skills" / "manifests"
REPO_ROOT = MANIFEST_DIR.parent.parent

# Directories whose source can legitimately name an endpoint id. ``tests``
# is excluded on purpose: a test naming an endpoint proves nothing about
# whether the runtime can dispatch it.
IMPL_SOURCE_DIRS = (
    "skills",
    "integrations",
    "agents",
    "api",
    "hardware",
    "memory",
    "workflows",
    "channels",
    "perception",
    "services",
)


def _manifest_paths() -> list[Path]:
    return sorted(MANIFEST_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _all_manifests() -> list[tuple[str, dict]]:
    return [(p.name, _load(p)) for p in _manifest_paths()]


def _manifest_ids() -> list[str]:
    return [p.name for p in _manifest_paths()]


@pytest.fixture(scope="module")
def policy() -> SandboxPolicy:
    """The shipped default policy, i.e. what a fresh install enforces."""
    return SandboxPolicy()


# ─────────────────────────────────────────────────────────────
# Harvesting the calls a description tells the model to write
# ─────────────────────────────────────────────────────────────

# A description teaches by example. These are the phrases the shipped
# manifests use to introduce one, in the forms they actually appear in.
_EXAMPLE_LEAD = re.compile(
    r"(?:examples?|e\.g\.|for example|must use format|such as)\s*[:.]?\s*",
    re.IGNORECASE,
)

# An example ends at the end of the sentence that introduced it. Splitting
# on ", " or "; " inside that span separates sibling examples
# ("Examples: open -a Safari; pbpaste; sw_vers").
_EXAMPLE_TERMINATORS = re.compile(r"(?:\.\s+[A-Z])|(?:\.$)|\n")

# A manifest sometimes annotates one example with what it is for
# ("osascript -e '…' to read the current level"). The annotation is prose,
# not part of the call, and the model will not copy it into the argument.
# Trim a trailing connector followed by at least three plain lowercase
# words. The three-word floor is what keeps AppleScript intact: "tell
# application \"Music\" to activate" ends in a two-word clause and stays
# whole, which matters because "to activate" IS part of that call.
_TRAILING_PROSE = re.compile(
    r"\s+(?:to|for|which|and|so|then)\s+(?:[a-z]+\s+){2,}[a-z]+\.?$"
)


def _example_span(text: str, start: int) -> str:
    tail = text[start:]
    end = _EXAMPLE_TERMINATORS.search(tail)
    return tail[: end.start()] if end else tail


def _split_examples(span: str) -> list[str]:
    """Split one example clause into the individual calls it names."""
    parts: list[str] = []
    depth = 0
    current = ""
    in_quote = ""
    for ch in span:
        if in_quote:
            current += ch
            if ch == in_quote:
                in_quote = ""
            continue
        if ch in "\"'":
            in_quote = ch
            current += ch
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch in ",;" and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += ch
    parts.append(current)
    cleaned = []
    for part in parts:
        candidate = part.strip().strip(".").strip()
        if not candidate:
            continue
        cleaned.append(_TRAILING_PROSE.sub("", candidate).strip())
    return [c for c in cleaned if c]


def _harvest_examples(text: str) -> list[str]:
    """Every concrete call an endpoint's prose tells the model to write."""
    out: list[str] = []
    for match in _EXAMPLE_LEAD.finditer(text or ""):
        span = _example_span(text, match.end())
        out.extend(_split_examples(span))
    return out


def _looks_like_applescript(candidate: str) -> bool:
    head = candidate.lower().lstrip()
    return head.startswith(("tell ", "set volume", "activate", "open ", "say "))


def _looks_like_shell(candidate: str, allowlist: list[str]) -> bool:
    token = candidate.split(" ", 1)[0].strip().lower()
    return token in allowlist


def _daemon_kind(url: str) -> str:
    """``"applescript"``, ``"shell"`` or ``""`` for a manifest endpoint url."""
    if not url.startswith("daemon://local/"):
        return ""
    return url[len("daemon://local/"):].split("/")[0]


# ─────────────────────────────────────────────────────────────
# 1. Every example call in a description survives its own gate
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,data", _all_manifests(), ids=_manifest_ids())
def test_description_examples_pass_the_daemon_validator(name, data, policy):
    """Run the validator on the exact call the description hands the model.

    This is the check that would have caught ``open location``: the
    description's own example is fed to the real validator instead of being
    reasoned about.
    """
    failures: list[str] = []
    for endpoint in data.get("endpoints", []):
        kind = _daemon_kind(endpoint.get("url", "") or "")
        if not kind:
            continue

        candidates: list[str] = []
        texts = [endpoint.get("description", "") or ""]
        for param in endpoint.get("params", []) or []:
            texts.append(param.get("description", "") or "")
            default = param.get("default")
            if isinstance(default, str) and default.strip():
                # A default is not an example, it is the literal call the
                # runtime makes when the model omits the parameter.
                candidates.append(default.strip())
        for text in texts:
            candidates.extend(_harvest_examples(text))

        allowlist = policy.daemon_shell_allowlist()
        for candidate in candidates:
            if kind == "applescript":
                if not _looks_like_applescript(candidate):
                    continue
                ok, reason = policy.validate_applescript(candidate)
            elif kind == "shell":
                if not _looks_like_shell(candidate, allowlist):
                    continue
                ok, reason = policy.validate_shell_command(candidate)
            else:
                continue
            if not ok:
                failures.append(
                    f"{data.get('skill_id')}__{endpoint.get('id')} tells the "
                    f"model to write {candidate!r}, which daemon://local/{kind} "
                    f"refuses: {reason}"
                )

    assert not failures, "\n".join(failures)


def _checked_calls_for(endpoint: dict, kind: str, allowlist: list[str]) -> list[str]:
    """The calls the test above actually feeds to a validator."""
    candidates: list[str] = []
    texts = [endpoint.get("description", "") or ""]
    for param in endpoint.get("params", []) or []:
        texts.append(param.get("description", "") or "")
        default = param.get("default")
        if isinstance(default, str) and default.strip():
            candidates.append(default.strip())
    for text in texts:
        candidates.extend(_harvest_examples(text))
    if kind == "applescript":
        return [c for c in candidates if _looks_like_applescript(c)]
    return [c for c in candidates if _looks_like_shell(c, allowlist)]


def test_every_daemon_endpoint_contributes_a_checked_call(policy):
    """The harvester must never go quiet.

    ``test_description_examples_pass_the_daemon_validator`` passes trivially
    if the prose stops matching the harvest patterns, which is exactly what a
    reworded description would do. Pin that every daemon-gated endpoint still
    yields at least one call that a validator sees.
    """
    allowlist = policy.daemon_shell_allowlist()
    silent: list[str] = []
    checked = 0
    for _name, data in _all_manifests():
        for endpoint in data.get("endpoints", []):
            kind = _daemon_kind(endpoint.get("url", "") or "")
            if not kind:
                continue
            calls = _checked_calls_for(endpoint, kind, allowlist)
            if not calls:
                silent.append(f"{data.get('skill_id')}__{endpoint.get('id')} ({kind})")
            checked += len(calls)
    assert not silent, (
        "these daemon-gated endpoints no longer show the model a call this "
        f"test can validate: {silent}"
    )
    assert checked, "no daemon-gated endpoint was found at all"


# ─────────────────────────────────────────────────────────────
# 2. A partial list of what is rejected is worse than no list
# ─────────────────────────────────────────────────────────────

def _manifest_prose(data: dict) -> str:
    """Every string in a manifest the model is shown, joined."""
    chunks = [data.get("description", "") or ""]
    for endpoint in data.get("endpoints", []):
        chunks.append(endpoint.get("description", "") or "")
        chunks.append(endpoint.get("returns_description", "") or "")
        for param in endpoint.get("params", []) or []:
            chunks.append(param.get("description", "") or "")
    return "\n".join(chunks)


@pytest.mark.parametrize("name,data", _all_manifests(), ids=_manifest_ids())
def test_applescript_rejection_lists_are_complete(name, data, policy):
    """Naming some denied AppleScript phrases means naming all of them.

    A description that lists four of the ten phrases
    ``SandboxPolicy.validate_applescript`` rejects reads as exhaustive. The
    model writes something that is not on the list and is refused anyway.
    Either name every rejection that applies, or do not imply a list.
    """
    prose = _manifest_prose(data).lower()
    denied = policy.applescript_denied_phrases()
    named = [phrase for phrase in denied if phrase in prose]
    if not named:
        return
    missing = [phrase for phrase in denied if phrase not in prose]
    assert not missing, (
        f"{name} names {len(named)} of the {len(denied)} AppleScript phrases "
        f"SandboxPolicy.validate_applescript rejects, which reads as a "
        f"complete list. Missing: {missing}. Name all of them, or drop the "
        f"enumeration and point at the permitted path instead."
    )


@pytest.mark.parametrize("name,data", _all_manifests(), ids=_manifest_ids())
def test_shell_allowlist_enumerations_are_complete(name, data, policy):
    """A manifest that enumerates the daemon shell allowlist must match it."""
    prose = _manifest_prose(data).lower()
    allowlist = policy.daemon_shell_allowlist()
    uses_daemon_shell = any(
        _daemon_kind(e.get("url", "") or "") == "shell"
        for e in data.get("endpoints", [])
    )
    if not uses_daemon_shell:
        return
    # Only manifests that present a list are held to it. The marker is the
    # word "allowlist" next to at least three program names.
    named = [p for p in allowlist if re.search(rf"\b{re.escape(p)}\b", prose)]
    if "allowlist" not in prose or len(named) < 3:
        return
    missing = [p for p in allowlist if p not in named]
    assert not missing, (
        f"{name} enumerates the daemon shell allowlist but omits {missing}. "
        f"The live allowlist is {allowlist}."
    )


# ─────────────────────────────────────────────────────────────
# 3. Every advertised endpoint has something that can serve it
# ─────────────────────────────────────────────────────────────

def _source_corpus() -> str:
    chunks: list[str] = []
    for rel in IMPL_SOURCE_DIRS:
        base = REPO_ROOT / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts or "build" in path.parts:
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    return "\n".join(chunks)


_CORPUS: str | None = None


def _corpus() -> str:
    global _CORPUS
    if _CORPUS is None:
        _CORPUS = _source_corpus()
    return _CORPUS


@pytest.mark.parametrize("name,data", _all_manifests(), ids=_manifest_ids())
def test_impl_backed_endpoints_are_routable(name, data):
    """An endpoint the model is offered must have a dispatcher that knows it.

    The missing-endpoint case is the same failure from the other side: the
    model is told a capability exists, calls it, and gets
    ``Unknown endpoint: <id>``. Only endpoints that resolve through a Python
    backing class are checked -- an ``http(s)://`` endpoint is served by the
    generic runner and a ``WS_EXECUTE`` one by a remote daemon, neither of
    which names the endpoint id in this repository.
    """
    corpus = _corpus()
    missing: list[str] = []
    for endpoint in data.get("endpoints", []):
        url = endpoint.get("url", "") or ""
        if url.startswith(("http://", "https://", "daemon://")):
            continue
        if endpoint.get("method") == "WS_EXECUTE":
            continue
        eid = endpoint.get("id", "")
        if f'"{eid}"' not in corpus and f"'{eid}'" not in corpus:
            missing.append(eid)
    assert not missing, (
        f"{name} advertises {missing} but no dispatcher in "
        f"{list(IMPL_SOURCE_DIRS)} names those endpoint ids, so the call "
        f"returns 'Unknown endpoint'."
    )


# ─────────────────────────────────────────────────────────────
# 4. HTTP endpoints survive the domain gate the executor applies
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,data", _all_manifests(), ids=_manifest_ids())
def test_http_endpoints_pass_the_domain_gate(name, data):
    """``SkillExecutor._domain_gate`` must not refuse a shipped manifest."""
    from skills.executor import SkillExecutor

    executor = SkillExecutor()
    skill_id = data.get("skill_id", "")
    refusals: list[str] = []
    for endpoint in data.get("endpoints", []):
        url = endpoint.get("url", "") or ""
        if not url.startswith(("http://", "https://")):
            continue
        # Substitute path templates with a placeholder so the host survives.
        probe = re.sub(r"\{[^}]+\}", "x", url)
        refusal = executor._domain_gate(skill_id, probe)
        if refusal is not None:
            refusals.append(f"{skill_id}__{endpoint.get('id')}: {refusal['error']}")
    assert not refusals, "\n".join(refusals)


# ─────────────────────────────────────────────────────────────
# 5. Sandbox-only skills must say the sandbox is mandatory
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,data", _all_manifests(), ids=_manifest_ids())
def test_sandbox_only_skills_do_not_promise_a_host_fallback(name, data):
    """A skill the executor refuses without Docker must say so, once.

    ``SkillExecutor._is_sandbox_required`` returns True for every endpoint of
    a skill in ``SANDBOX_REQUIRED_SKILL_IDS``, and
    ``_sandbox_requirement_status`` then returns HTTP 503 before the backing
    implementation is reached. A description that offers a host fallback is
    describing a branch the executor never lets the call get to.
    """
    from skills.executor import SANDBOX_REQUIRED_SKILL_IDS

    skill_id = data.get("skill_id", "")
    sandbox_only = skill_id in SANDBOX_REQUIRED_SKILL_IDS or bool(
        data.get("requires_sandbox")
    )
    if not sandbox_only:
        return

    description = (data.get("description", "") or "").lower()
    assert "docker" in description, (
        f"{name} is refused with HTTP 503 on every endpoint when the Docker "
        f"sandbox is unavailable (the default on macOS), but its description "
        f"never says the sandbox is required."
    )
    forbidden = ("or host", "on host", "host fallback", "falls back to the host")
    offending = [phrase for phrase in forbidden if phrase in description]
    assert not offending, (
        f"{name} promises a host execution path ({offending}) that "
        f"SkillExecutor refuses before the implementation is reached."
    )


# ─────────────────────────────────────────────────────────────
# 6. A declared parameter range must survive the safety resolver
# ─────────────────────────────────────────────────────────────

_RANGE_RE = re.compile(r"(-?\d+)\s*(?:to|-|–|\.\.)\s*(-?\d+)")


def _declared_upper_bound(text: str) -> int | None:
    match = _RANGE_RE.search(text or "")
    if not match:
        return None
    try:
        return max(int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None


@pytest.mark.parametrize("name,data", _all_manifests(), ids=_manifest_ids())
def test_declared_motion_ranges_are_not_denied_by_policy(name, data):
    """The top of a declared speed range must not be a refusal.

    ``safety_resolver._cutebot_drive_speed_deny`` refuses a drive command
    outright above 80. A parameter description that says "-100 to 100"
    without saying where the refusal is invites the model to write a call
    that is denied at dispatch.
    """
    from security.safety_resolver import LEVEL_DENY, resolve_policy

    skill_id = data.get("skill_id", "")
    motion_params = {"speed", "left", "right"}
    failures: list[str] = []
    for endpoint in data.get("endpoints", []):
        tool_name = f"{skill_id}__{endpoint.get('id')}"
        args: dict[str, int] = {}
        described: dict[str, int] = {}
        for param in endpoint.get("params", []) or []:
            pname = param.get("name", "")
            if pname not in motion_params:
                continue
            upper = _declared_upper_bound(param.get("description", "") or "")
            if upper is None:
                continue
            args[pname] = upper
            described[pname] = upper
        if not args:
            continue
        decision = resolve_policy(tool_name, args, surface="websocket")
        if decision.level == LEVEL_DENY:
            failures.append(
                f"{tool_name} documents {described} but resolve_policy denies "
                f"it: {decision.deny_reason}"
            )
    assert not failures, "\n".join(failures)


# ─────────────────────────────────────────────────────────────
# 7. The manifests still load, and say only what the model reads
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,data", _all_manifests(), ids=_manifest_ids())
def test_every_endpoint_and_param_tells_the_model_something(name, data):
    """No blank guidance anywhere the model reads.

    ``EndpointParam.description`` defaults to ``""`` and
    ``SkillRegistry._manifest_to_tools`` copies it straight into the tool
    schema, so a missing description ships an argument the model has to
    guess the meaning of. ``returns_description`` is the same problem after
    the call: the model has to guess what came back.
    """
    blanks: list[str] = []
    for endpoint in data.get("endpoints", []):
        eid = endpoint.get("id", "?")
        if not (endpoint.get("description") or "").strip():
            blanks.append(f"{eid}: endpoint description")
        if not (endpoint.get("returns_description") or "").strip():
            blanks.append(f"{eid}: returns_description")
        for param in endpoint.get("params", []) or []:
            if not (param.get("description") or "").strip():
                blanks.append(f"{eid}.{param.get('name', '?')}: param description")
    assert not blanks, f"{name} ships blank guidance: {blanks}"


@pytest.mark.parametrize("name,data", _all_manifests(), ids=_manifest_ids())
def test_manifest_loads_and_keeps_every_auth_field(name, data):
    """A dropped auth key is a promise nothing carries.

    ``AuthConfig`` has a closed field set; pydantic drops anything else
    silently, so ``{"type": "api_key", "key_name": "OPENAI_API_KEY"}``
    parses into an ``AuthConfig`` that names no key at all.
    """
    manifest = SkillManifest(**data)
    assert manifest.skill_id
    declared = data.get("auth") or {}
    if not isinstance(declared, dict):
        return
    known = set(type(manifest.auth).model_fields)
    dropped = sorted(set(declared) - known)
    assert not dropped, (
        f"{name} declares auth fields {dropped} that AuthConfig does not "
        f"model, so they are dropped at load and nothing reads them."
    )
