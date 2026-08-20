"""A manifest-declared default must reach the skill that runs.

The defect this guards
----------------------
``SkillRegistry._manifest_to_tools`` copies ``param.default`` into the
JSON tool schema the model reads. A model reading a schema that already
states the right default correctly omits the parameter. Nothing then put
that default into the args, so the skill was invoked with the parameter
absent.

Measured on the shipped catalog before the fix: 89 optional params
across 19 skills declared a default, and all 89 arrived missing at the
implementation. The reachable case was
``desktop_control__list_running_apps``, whose ``script`` param is
optional and carries a working AppleScript default; called with the
empty args its own schema invites, it returned::

    {"success": false, "status_code": 400,
     "error": "No AppleScript provided in `script`."}

``ToolDispatchValidator.validate`` already computed the filled args, but
only ``ToolRunner`` calls the validator; the executor's other production
callers (multi-agent, direct execution, MCP server, both voice realtime
proxies) reach ``SkillExecutor.execute`` directly. The fill therefore
lives at the executor chokepoint, and this file is the guard that it
covers every manifest and every dispatch lane.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agents.tool_dispatch_validator import JSON_TYPE_CHECKS, _coerce_default
from models.skill_manifest import SkillEndpoint, SkillManifest
from skills.executor import SkillExecutor
from skills.registry import SkillRegistry

MANIFEST_DIR = Path(__file__).resolve().parent.parent / "skills" / "manifests"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def registry() -> SkillRegistry:
    """Every shipped manifest, loaded through the real registry.

    ``load_from_directory`` rather than ``load_builtin_skills`` so the
    walk covers exactly the files in this repo and never a skill the
    developer happens to have installed under ``~/.feral/skills``.
    """
    reg = SkillRegistry()
    reg.load_from_directory(MANIFEST_DIR)
    return reg


def _defaulted_endpoints(reg: SkillRegistry):
    """``(skill, endpoint, {param_name: expected_value})`` for every
    endpoint in the catalog that declares at least one default."""
    for skill_id in sorted(reg.skills):
        manifest = reg.skills[skill_id]
        for endpoint in manifest.endpoints:
            expected = {
                p.name: _coerce_default(p)
                for p in endpoint.params
                if p.default is not None
            }
            if expected:
                yield manifest, endpoint, expected


class _Spy:
    """Stands in for a skill's Python backing class and records args."""

    def __init__(self) -> None:
        self.seen: dict | None = None

    async def execute(self, endpoint_id, args, vault):
        self.seen = dict(args)
        return {"success": True, "status_code": 200, "data": {}}


@pytest.fixture
def spy_impl(monkeypatch):
    """Install a spy as the backing implementation for any skill id.

    Patches the module-level ``SKILL_IMPLEMENTATIONS`` dict rather than
    calling ``register_instance``, so the real implementations are
    restored when the test ends and nothing leaks into the rest of the
    suite.
    """
    import skills.impl as impl_mod

    patched = dict(impl_mod.SKILL_IMPLEMENTATIONS)
    monkeypatch.setattr(impl_mod, "SKILL_IMPLEMENTATIONS", patched)

    def _install(skill_id: str) -> _Spy:
        spy = _Spy()
        patched[skill_id] = spy
        return spy

    return _install


# ---------------------------------------------------------------------------
# 1. the catalog itself has to be coherent
# ---------------------------------------------------------------------------

def test_every_shipped_default_matches_its_declared_type():
    """``EndpointParam.default`` is ``Optional[str]``, so every default
    in every manifest is a *string* on disk even when the param declares
    ``integer`` or ``boolean``. The executor coerces before injecting; a
    default that cannot be coerced is dropped with a warning rather than
    passed through wrong-typed. Nothing in this repo may rely on that
    branch, so it is an error here."""
    bad = []
    for path in sorted(MANIFEST_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        manifest = SkillManifest(**data)
        for endpoint in manifest.endpoints:
            for param in endpoint.params:
                if param.default is None:
                    continue
                checker = JSON_TYPE_CHECKS.get(param.type)
                if checker and not checker(_coerce_default(param)):
                    bad.append(
                        f"{manifest.skill_id}__{endpoint.id}.{param.name}: "
                        f"default {param.default!r} is not a valid '{param.type}'"
                    )
    assert not bad, "manifest defaults that cannot be coerced:\n" + "\n".join(bad)


def test_the_catalog_still_declares_defaults():
    """A guard against the guard: if a refactor emptied every default,
    the walk below would pass vacuously."""
    reg = SkillRegistry()
    reg.load_from_directory(MANIFEST_DIR)
    total = sum(len(exp) for _, _, exp in _defaulted_endpoints(reg))
    assert total >= 80, f"only {total} defaulted params found; the walk is near-vacuous"


# ---------------------------------------------------------------------------
# 2. the walk: every manifest, every defaulted endpoint
# ---------------------------------------------------------------------------

def test_every_declared_default_reaches_the_implementation(registry, spy_impl, monkeypatch):
    """The durable guard. For every endpoint in every shipped manifest
    that declares a default, dispatching with empty args must deliver
    that default (coerced to the declared type) to the backing skill."""
    executor = SkillExecutor()
    # Sandbox-gated skills refuse before reaching any implementation.
    # This test is about argument plumbing, not about the sandbox.
    monkeypatch.setattr(SkillExecutor, "_is_sandbox_required", staticmethod(lambda s, e: False))

    missing: list[str] = []
    wrong: list[str] = []
    checked = 0

    async def _run():
        nonlocal checked
        for manifest, endpoint, expected in _defaulted_endpoints(registry):
            spy = spy_impl(manifest.skill_id)
            await executor.execute(
                f"{manifest.skill_id}__{endpoint.id}", {}, manifest, endpoint,
            )
            assert spy.seen is not None, f"{manifest.skill_id}__{endpoint.id} never dispatched"
            for name, value in expected.items():
                checked += 1
                if name not in spy.seen:
                    missing.append(f"{manifest.skill_id}__{endpoint.id}.{name}")
                elif spy.seen[name] != value or type(spy.seen[name]) is not type(value):
                    wrong.append(
                        f"{manifest.skill_id}__{endpoint.id}.{name}: "
                        f"got {spy.seen[name]!r} ({type(spy.seen[name]).__name__}), "
                        f"want {value!r} ({type(value).__name__})"
                    )

    asyncio.run(_run())
    assert checked >= 80, f"walk only checked {checked} params"
    assert not missing, "defaults that never reached the skill:\n" + "\n".join(missing)
    assert not wrong, "defaults that arrived wrong:\n" + "\n".join(wrong)


# ---------------------------------------------------------------------------
# 3. never overwrite what the caller said, including falsy values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "param_type, default, supplied",
    [
        ("string", "fallback", ""),
        ("integer", "10", 0),
        ("number", "1.5", 0.0),
        ("boolean", "true", False),
        ("string", "fallback", "explicit"),
        ("integer", "10", 42),
    ],
)
def test_a_supplied_value_is_never_replaced(param_type, default, supplied, spy_impl, monkeypatch):
    """``""``, ``0``, ``0.0`` and ``False`` are things a caller can mean.
    A truthiness test would silently replace all four with the manifest's
    default, which is a different bug in the same family."""
    manifest, endpoint = _synthetic("python_lane", param_type, default)
    spy = spy_impl(manifest.skill_id)
    executor = SkillExecutor()
    monkeypatch.setattr(SkillExecutor, "_is_sandbox_required", staticmethod(lambda s, e: False))

    asyncio.run(executor.execute(
        "defaults_probe__probe", {"opt": supplied}, manifest, endpoint,
    ))
    assert spy.seen["opt"] == supplied
    assert type(spy.seen["opt"]) is type(supplied)


def test_an_explicit_none_is_left_alone(spy_impl, monkeypatch):
    """``{"opt": None}`` is the caller saying "no value", which is not
    the same as not mentioning the parameter at all."""
    manifest, endpoint = _synthetic("python_lane", "string", "fallback")
    spy = spy_impl(manifest.skill_id)
    executor = SkillExecutor()
    monkeypatch.setattr(SkillExecutor, "_is_sandbox_required", staticmethod(lambda s, e: False))

    asyncio.run(executor.execute(
        "defaults_probe__probe", {"opt": None}, manifest, endpoint,
    ))
    assert spy.seen["opt"] is None


def test_a_param_without_a_default_stays_absent(spy_impl, monkeypatch):
    manifest, endpoint = _synthetic("python_lane", "string", None)
    spy = spy_impl(manifest.skill_id)
    executor = SkillExecutor()
    monkeypatch.setattr(SkillExecutor, "_is_sandbox_required", staticmethod(lambda s, e: False))

    asyncio.run(executor.execute("defaults_probe__probe", {}, manifest, endpoint))
    assert "opt" not in spy.seen


def test_an_uncoercible_default_is_dropped_not_passed_wrong_typed(spy_impl, monkeypatch, caplog):
    """A manifest bug must not become a wrong-typed argument. No shipped
    manifest reaches this branch (see the coherence test above); a
    third-party skill can."""
    manifest, endpoint = _synthetic("python_lane", "integer", "not-a-number")
    spy = spy_impl(manifest.skill_id)
    executor = SkillExecutor()
    monkeypatch.setattr(SkillExecutor, "_is_sandbox_required", staticmethod(lambda s, e: False))

    with caplog.at_level("WARNING", logger="feral.executor"):
        asyncio.run(executor.execute("defaults_probe__probe", {}, manifest, endpoint))
    assert "opt" not in spy.seen
    assert any("not a valid 'integer'" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 4. every dispatch lane, not just the Python one
# ---------------------------------------------------------------------------

def _synthetic(lane: str, param_type: str = "string", default: str | None = "fallback"):
    """A one-endpoint manifest whose URL/method selects a given lane."""
    url, method = {
        "python_lane": ("", "PYTHON"),
        "daemon": ("daemon://local/applescript", "PYTHON"),
        "ws": ("", "WS_EXECUTE"),
        "http": ("https://example.invalid/probe", "POST"),
        "wasm": ("", "PYTHON"),
    }[lane]
    param = {
        "name": "opt", "type": param_type, "required": False,
        "description": "probe param",
    }
    if default is not None:
        param["default"] = default
    manifest = SkillManifest(
        skill_id="defaults_probe",
        version="1.0.0",
        brand={"name": "Defaults Probe"},
        description="probe",
        auth={"type": "none"},
        endpoints=[SkillEndpoint(
            id="probe", method=method, url=url,
            description="probe", params=[param],
        )],
    )
    return manifest, manifest.endpoints[0]


def test_daemon_lane_gets_the_default(monkeypatch):
    """``daemon://`` reads ``args["script"]`` / ``args["command"]``
    directly. This is the lane ``desktop_control__list_running_apps``
    runs on."""
    manifest, endpoint = _synthetic("daemon")
    endpoint.params[0].name = "script"
    endpoint.params[0].default = "tell application \"System Events\" to get name"
    seen = {}

    async def _fake_daemon(path, command):
        seen["path"], seen["command"] = path, command
        return {"success": True, "status_code": 200, "data": {}, "error": None}

    executor = SkillExecutor()
    monkeypatch.setattr(SkillExecutor, "_execute_local_daemon", staticmethod(_fake_daemon))
    asyncio.run(executor.execute("defaults_probe__probe", {}, manifest, endpoint))
    assert seen["command"] == "tell application \"System Events\" to get name"


def test_ws_execute_lane_gets_the_default():
    """WS_EXECUTE ships ``args`` verbatim to the daemon in the payload."""
    manifest, endpoint = _synthetic("ws")
    sent = {}

    class _WS:
        async def send_json(self, msg):
            sent.update(msg)
            fut = executor._pending_results[msg["msg_id"]]
            fut.set_result({"status": "success", "stdout": "ok"})

    executor = SkillExecutor(daemons={"robot-1": _WS()})
    executor.register_daemon_type("robot-1", "robot")
    asyncio.run(executor.execute("defaults_probe__probe", {}, manifest, endpoint))
    assert sent["payload"]["args"] == {"opt": "fallback"}


def test_http_lane_gets_the_default(monkeypatch):
    """The generic HTTP runner sends ``args`` as the JSON body."""
    manifest, endpoint = _synthetic("http")
    sent = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    async def _request(method, url, json=None, headers=None):
        sent["json"] = json
        return _Resp()

    executor = SkillExecutor()
    monkeypatch.setattr(executor.client, "request", _request)
    monkeypatch.setattr(SkillExecutor, "_domain_gate", lambda self, sid, url: None)
    asyncio.run(executor.execute("defaults_probe__probe", {}, manifest, endpoint))
    assert sent["json"] == {"opt": "fallback"}


def test_wasm_lane_gets_the_default(monkeypatch, tmp_path):
    """The WASM lane keys off ``skill.runtime``/``endpoint.runtime``.

    Neither ``SkillManifest`` nor ``SkillEndpoint`` declares a
    ``runtime`` field, so a pydantic-validated manifest cannot express
    it and this branch is unreachable from the shipped catalog today
    (reported separately; the model file is not this lane's to change).
    A duck-typed stand-in is used so the argument plumbing is still
    covered if that field is ever added.
    """
    real, endpoint = _synthetic("wasm")
    seen = {}

    class _WasmManifest:
        skill_id = real.skill_id
        runtime = "wasm"
        auth = real.auth
        endpoints = real.endpoints

    manifest = _WasmManifest()

    class _Sandbox:
        available = True

        async def execute(self, wasm_path, params, entry_point):
            seen.update(params)
            return {"success": True, "data": {}}

    skill_dir = tmp_path / "skills" / "defaults_probe"
    skill_dir.mkdir(parents=True)
    (skill_dir / "probe.wasm").write_bytes(b"\0asm")
    # Patch the executor's own reference rather than FERAL_HOME, so the
    # suite's environment-leak guard has nothing to report.
    monkeypatch.setattr("skills.executor.feral_home", lambda: tmp_path)

    executor = SkillExecutor()
    executor.set_wasm_sandbox(_Sandbox())
    monkeypatch.setattr(SkillExecutor, "_is_sandbox_required", staticmethod(lambda s, e: False))
    asyncio.run(executor.execute("defaults_probe__probe", {}, manifest, endpoint))
    assert seen == {"opt": "fallback"}


# ---------------------------------------------------------------------------
# 5. the schema the model reads has to state the same thing
# ---------------------------------------------------------------------------

def test_a_falsy_default_still_appears_in_the_tool_schema(registry):
    """``if param.default:`` dropped ``""``, ``0`` and ``false``
    entirely. Eight shipped params were affected; the model saw an
    optional parameter with no stated default at all."""
    falsy = []
    for skill_id in sorted(registry.skills):
        manifest = registry.skills[skill_id]
        tools = {t["function"]["name"]: t for t in registry._tool_cache[skill_id]}
        for endpoint in manifest.endpoints:
            props = tools[f"{skill_id}__{endpoint.id}"]["function"]["parameters"]["properties"]
            for param in endpoint.params:
                if param.default is None:
                    continue
                assert "default" in props[param.name], (
                    f"{skill_id}__{endpoint.id}.{param.name} declares "
                    f"default {param.default!r} but the tool schema omits it"
                )
                if not _coerce_default(param):
                    falsy.append(f"{skill_id}__{endpoint.id}.{param.name}")
    assert falsy, "expected at least one falsy default in the catalog to guard"


def test_the_tool_schema_default_is_typed_like_the_param(registry):
    """A ``boolean`` param advertising the string ``"false"`` is worse
    than advertising nothing: the string is truthy everywhere."""
    for skill_id in sorted(registry.skills):
        manifest = registry.skills[skill_id]
        tools = {t["function"]["name"]: t for t in registry._tool_cache[skill_id]}
        for endpoint in manifest.endpoints:
            props = tools[f"{skill_id}__{endpoint.id}"]["function"]["parameters"]["properties"]
            for param in endpoint.params:
                if param.default is None:
                    continue
                checker = JSON_TYPE_CHECKS.get(param.type)
                if checker is None:
                    continue
                assert checker(props[param.name]["default"]), (
                    f"{skill_id}__{endpoint.id}.{param.name} advertises default "
                    f"{props[param.name]['default']!r} for declared type "
                    f"'{param.type}'"
                )


def test_the_schema_default_and_the_injected_default_agree(registry, spy_impl, monkeypatch):
    """The whole point: what the model is told and what the skill gets
    must be the same value. This is the assertion that would have caught
    the original defect from either side."""
    executor = SkillExecutor()
    monkeypatch.setattr(SkillExecutor, "_is_sandbox_required", staticmethod(lambda s, e: False))

    async def _run():
        for manifest, endpoint, expected in _defaulted_endpoints(registry):
            tools = {t["function"]["name"]: t for t in registry._tool_cache[manifest.skill_id]}
            props = tools[f"{manifest.skill_id}__{endpoint.id}"]["function"]["parameters"]["properties"]
            spy = spy_impl(manifest.skill_id)
            await executor.execute(
                f"{manifest.skill_id}__{endpoint.id}", {}, manifest, endpoint,
            )
            for name in expected:
                assert props[name]["default"] == spy.seen[name], (
                    f"{manifest.skill_id}__{endpoint.id}.{name}: schema says "
                    f"{props[name]['default']!r}, skill got {spy.seen[name]!r}"
                )

    asyncio.run(_run())
