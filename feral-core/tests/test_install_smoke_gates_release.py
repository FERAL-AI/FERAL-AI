"""F-09 — the install smoke test could not fail, and ran after publishing.

Three separate defects in `.github/workflows/install-smoke.yml`:

1. Every verification command ended in ``|| true``. A wheel whose ``feral``
   entry point raises on import passed the job. Demonstrated with a stub
   ``feral`` that exits 1::

       $ feral --help || true; echo $?
       ModuleNotFoundError: No module named 'mlx_lm'
       0

2. It installed ``feral-ai`` with no extras, while ``scripts/install.sh``
   installs ``feral-ai[all]``. The command users actually run was never the
   command CI ran.

3. It triggered on ``workflow_run: [Release], types: [completed]``, so it ran
   *after* the PyPI publish. A failure reported a bad release; it could not
   prevent one.

The audit describes the shape once. The file has two jobs, ``smoke-linux`` and
``smoke-macos``, and both carried all three defects, so it is four tolerated
commands and two bare installs.

This file asserts the structure of the workflows because a GitHub Actions run
cannot be executed here. It is a contract test, not a substitute for a real
release.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
INSTALL_SMOKE = WORKFLOWS / "install-smoke.yml"
RELEASE = WORKFLOWS / "publish.yml"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _load(path: Path) -> dict:
    # PyYAML parses the bare key `on:` as the boolean True (the "Norway
    # problem"), so triggers are read back under True, not "on".
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    return doc.get("on") or doc.get(True) or {}


def _code_lines(body: str) -> list[str]:
    """Executable lines of a `run:` body, with comments dropped.

    These workflows explain deliberate choices in shell comments (why the
    lockfile is *not* used, for instance), so a check that greps the raw body
    reads its own rationale as a violation.
    """
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _run_steps(doc: dict) -> list[tuple[str, str, str]]:
    """Every (job, step name, run body) in the workflow."""
    out = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            if "run" in step:
                out.append((job_name, step.get("name", "<unnamed>"), step["run"]))
    return out


def test_install_smoke_exists():
    assert INSTALL_SMOKE.exists(), f"missing {INSTALL_SMOKE}"


def test_no_verification_command_is_allowed_to_fail_silently():
    """`|| true` on a verification command makes the job unfalsifiable."""
    doc = _load(INSTALL_SMOKE)
    offenders = []
    for job_name, step_name, body in _run_steps(doc):
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.endswith("|| true") and not stripped.startswith("#"):
                offenders.append(f"{job_name} / {step_name}: {stripped}")
    # `wait || true` after killing a background server is reaping, not
    # verification, so it is allowed by name rather than by pattern.
    offenders = [o for o in offenders if not o.endswith("wait || true")]
    assert not offenders, (
        "these commands cannot fail the job:\n  " + "\n  ".join(offenders)
    )


def test_the_smoke_installs_the_same_thing_users_install():
    """`scripts/install.sh` installs `feral-ai[all]`; the smoke must too."""
    assert "feral-ai[all]" in INSTALL_SH.read_text(encoding="utf-8"), (
        "scripts/install.sh no longer installs [all]; re-derive what the "
        "smoke should install rather than deleting this test"
    )
    doc = _load(INSTALL_SMOKE)
    bare = []
    for job_name, step_name, body in _run_steps(doc):
        for line in _code_lines(body):
            # Only install commands. `feral-ai` also appears as a plain
            # distribution-name argument to the extras checker, which is not
            # an install and has no extras to carry.
            if "pip install" not in line:
                continue
            for match in re.finditer(r"feral-ai(\[[a-z0-9,\-]*\])?", line):
                extras = match.group(1)
                if extras is None or "all" not in extras:
                    bare.append(f"{job_name} / {step_name}: {line}")
    assert not bare, (
        "the smoke installs something users never install:\n  " + "\n  ".join(bare)
    )


def test_the_smoke_asserts_the_installed_version():
    """Printing a version is not checking it.

    The old step piped the version to stdout and tolerated failure, so a wheel
    that resolved to a stale cached release still passed.
    """
    doc = _load(INSTALL_SMOKE)
    bodies = "\n".join(body for _, _, body in _run_steps(doc))
    assert "expected_version" in bodies or "EXPECTED_VERSION" in bodies, (
        "no step compares the installed version against an expected one"
    )


def test_the_smoke_is_callable_as_a_gate():
    """A `workflow_run` trigger can only ever report; it cannot gate."""
    triggers = _triggers(_load(INSTALL_SMOKE))
    assert "workflow_call" in triggers, (
        "install-smoke.yml is not reusable, so no job can depend on it and it "
        "can only run after the release it is meant to gate"
    )


def test_the_release_runs_the_smoke_before_publishing():
    """The gate has to sit between the build and the PyPI upload."""
    doc = _load(RELEASE)
    jobs = doc.get("jobs") or {}

    callers = [
        name for name, job in jobs.items()
        if isinstance(job.get("uses"), str)
        and job["uses"].endswith("install-smoke.yml")
    ]
    assert callers, (
        "the Release workflow never runs the install smoke, so a wheel that "
        "cannot be installed with [all] on a supported Python still publishes"
    )

    publish = jobs.get("publish")
    assert publish is not None, "Release workflow has no `publish` job"
    needs = publish.get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    missing = [c for c in callers if c not in needs]
    assert not missing, (
        f"publish does not depend on {missing}, so the smoke runs alongside "
        "the upload rather than gating it"
    )


def test_the_smoke_keeps_covering_every_supported_python():
    """3.14 is the version 2026.8.3 shipped broken on. It must stay covered."""
    doc = _load(INSTALL_SMOKE)
    versions: set[str] = set()
    for job in (doc.get("jobs") or {}).values():
        matrix = ((job.get("strategy") or {}).get("matrix") or {})
        for value in matrix.values():
            if isinstance(value, list):
                versions.update(str(v) for v in value)
    for expected in ("3.11", "3.12", "3.14"):
        assert expected in versions, f"install smoke no longer covers {expected}"


def test_the_gating_matrix_is_not_pinned_by_the_lockfile():
    """`requirements.lock` is a 3.11 resolution and must not gate 3.14.

    It pins `pillow==11.3.0` while `fastembed` needs `pillow>=12` on 3.14, so
    constraining this matrix would reintroduce exactly the marker-dependent
    conflict that shipped 2026.8.3 broken, and would do it as a green run.
    """
    doc = _load(INSTALL_SMOKE)
    for job_name, step_name, body in _run_steps(doc):
        for line in _code_lines(body):
            assert "requirements.lock" not in line, (
                f"{job_name} / {step_name} constrains the unconstrained smoke, "
                f"which is the one thing it exists to be: {line}"
            )
