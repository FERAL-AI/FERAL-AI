from __future__ import annotations

import pytest

from skills.impl import get_implementation
from skills.impl.code_interpreter import CodeInterpreterSkill


def test_code_interpreter_registered() -> None:
    impl = get_implementation("code_interpreter")
    assert impl is not None


@pytest.mark.asyncio
async def test_code_interpreter_unknown_endpoint() -> None:
    impl = get_implementation("code_interpreter")
    assert impl is not None
    out = await impl.execute("unknown", {}, {})
    assert out["success"] is False
    assert out["status_code"] == 404


@pytest.mark.asyncio
async def test_code_interpreter_captures_csv_artifact(tmp_path, monkeypatch) -> None:
    # Force the host-subprocess path. On CI the Docker binary is on PATH but
    # running `--user nobody` against the tmp mount fails with a permission
    # error that has nothing to do with the skill's artifact-capture logic.
    monkeypatch.setattr(
        "skills.impl.code_interpreter.DOCKER_AVAILABLE", False, raising=False
    )
    monkeypatch.setenv("FERAL_ARTIFACTS_DIR", str(tmp_path))
    skill = CodeInterpreterSkill()
    code = (
        "from pathlib import Path\n"
        "Path('out.csv').write_text('col1,col2\\n1,2\\n')\n"
        "print('done')\n"
    )
    out = await skill.execute("run_python", {"code": code, "timeout": 20}, {})
    assert out["status_code"] == 200
    assert out["success"] is True
    artifacts = out["data"]["artifacts"]
    assert any(a["name"] == "out.csv" for a in artifacts)


@pytest.mark.asyncio
async def test_code_interpreter_strict_mode_refuses_unsandboxed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "skills.impl.code_interpreter.DOCKER_AVAILABLE", False, raising=False
    )
    monkeypatch.setattr(
        "skills.impl.code_interpreter.WASM_PYTHON_AVAILABLE", False, raising=False
    )
    skill = CodeInterpreterSkill()
    out = await skill.execute(
        "run_python",
        {"code": "print('hi')", "_feral_require_sandbox": True},
        {},
    )
    assert out["success"] is False
    assert out["status_code"] == 503
    assert "Sandbox required" in (out.get("error") or "")


# ── Lane 05 : layered sandbox + WASM fallback ───────────────────


@pytest.mark.asyncio
async def test_run_python_returns_sandbox_label(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Result data carries an authoritative ``sandbox`` tier label.

    Acceptance: Lane 05  — code_interpreter works without Docker
    (live ``print('hello')``). The host-rlimit tier is what runs on
    CI; Docker / WASM tiers light up when their prereqs are present.
    """
    monkeypatch.setattr(
        "skills.impl.code_interpreter.DOCKER_AVAILABLE", False, raising=False
    )
    monkeypatch.setattr(
        "skills.impl.code_interpreter.WASM_PYTHON_AVAILABLE", False, raising=False
    )
    monkeypatch.setenv("FERAL_ARTIFACTS_DIR", str(tmp_path))
    skill = CodeInterpreterSkill()
    out = await skill.execute(
        "run_python",
        {"code": "print('hello')", "timeout": 20},
        {},
    )
    assert out["success"] is True
    assert out["data"]["stdout"].strip() == "hello"
    assert out["data"]["sandbox"] == "host-rlimit"
    assert out["data"]["sandboxed"] is False  # back-compat boolean


@pytest.mark.asyncio
async def test_run_python_stdlib_arithmetic_through_host_fallback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stdlib arithmetic round-trips through the host-rlimit fallback.

    Pins the host-fallback half of Lane 05 : when Docker AND WASM are
    unavailable, the soft-sandbox host tier still runs simple stdlib code.

    Why stdlib (not numpy): host-fallback intentionally uses a minimal
    ``safe_env`` (no ``PYTHONPATH``, narrow ``PATH``) so the subprocess
    can't see the brain's site-packages — that's a security feature, not
    a bug. The numpy-import requirement is satisfied by the Docker tier
    (image ships numpy) and the WASM/pyodide tier (bundle ships numpy);
    those are covered by their own tier-specific tests.
    """
    monkeypatch.setattr(
        "skills.impl.code_interpreter.DOCKER_AVAILABLE", False, raising=False
    )
    monkeypatch.setattr(
        "skills.impl.code_interpreter.WASM_PYTHON_AVAILABLE", False, raising=False
    )
    monkeypatch.setenv("FERAL_ARTIFACTS_DIR", str(tmp_path))
    skill = CodeInterpreterSkill()
    code = "print(sum(range(10)))\n"
    out = await skill.execute("run_python", {"code": code, "timeout": 30}, {})
    assert out["success"] is True, out
    assert out["data"]["stdout"].strip() == "45"
    assert out["data"]["sandbox"] == "host-rlimit"


@pytest.mark.asyncio
async def test_wasm_python_runner_routes_when_available(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When WASM_PYTHON_AVAILABLE is True the cascade routes to it
    BEFORE falling through to the host. We stub `_run_wasm_python`
    to return a synthetic success result and verify the dispatcher
    surfaces ``sandbox: 'wasm'`` without escalating.
    """
    monkeypatch.setattr(
        "skills.impl.code_interpreter.DOCKER_AVAILABLE", False, raising=False
    )
    monkeypatch.setattr(
        "skills.impl.code_interpreter.WASM_PYTHON_AVAILABLE", True, raising=False
    )

    async def _fake_wasm(code, work_dir, timeout):  # type: ignore[no-redef]
        return {
            "exit_code": 0,
            "stdout": "wasm hello\n",
            "stderr": "",
            "sandbox": "wasm",
        }

    monkeypatch.setattr(
        "skills.impl.code_interpreter._run_wasm_python", _fake_wasm
    )
    monkeypatch.setenv("FERAL_ARTIFACTS_DIR", str(tmp_path))
    skill = CodeInterpreterSkill()
    out = await skill.execute(
        "run_python",
        {"code": "print('hello')", "timeout": 5},
        {},
    )
    assert out["success"] is True
    assert out["data"]["stdout"].strip() == "wasm hello"
    assert out["data"]["sandbox"] == "wasm"
    assert out["data"]["sandboxed"] is True


@pytest.mark.asyncio
async def test_wasm_failure_falls_through_to_host(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If WASM execution reports ``sandbox: 'unavailable'`` (e.g.
    wasmtime CLI missing), the cascade keeps escalating to the host
    tier instead of erroring out."""
    monkeypatch.setattr(
        "skills.impl.code_interpreter.DOCKER_AVAILABLE", False, raising=False
    )
    monkeypatch.setattr(
        "skills.impl.code_interpreter.WASM_PYTHON_AVAILABLE", True, raising=False
    )

    async def _wasm_unavailable(code, work_dir, timeout):  # type: ignore[no-redef]
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": "wasmtime CLI not on PATH",
            "sandbox": "unavailable",
        }

    monkeypatch.setattr(
        "skills.impl.code_interpreter._run_wasm_python", _wasm_unavailable
    )
    monkeypatch.setenv("FERAL_ARTIFACTS_DIR", str(tmp_path))
    skill = CodeInterpreterSkill()
    out = await skill.execute(
        "run_python",
        {"code": "print('hello')", "timeout": 10},
        {},
    )
    assert out["success"] is True
    assert out["data"]["sandbox"] == "host-rlimit"
    assert out["data"]["stdout"].strip() == "hello"
