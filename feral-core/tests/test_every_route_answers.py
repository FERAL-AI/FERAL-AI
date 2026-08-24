"""Every registered GET route must answer without exploding.

The sweep itself lives in ``scripts_audit/route_sweep.py`` and is run
here in a SUBPROCESS, deliberately. Two reasons, both learned the hard
way while writing this:

1. A wedged route would otherwise hang the entire pytest session. The
   first version of this file did exactly that and had to be killed.
2. The sweep boots the real app with its own FERAL_HOME. Doing that
   inside a session where ``conftest`` has already initialised
   ``api.state.state`` produced a deadlock on lifespan teardown that
   had nothing to do with any route.

A test that can hang the suite is worse than no test, so it gets its
own process and a wall-clock ceiling.

See the module docstring in route_sweep.py for why the sweep exists at
all: this is the class of defect that reaches an operator as a blank
panel, and the three suites that run on every commit are all
structurally blind to it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


FERAL_CORE = Path(__file__).resolve().parents[1]
SWEEP = FERAL_CORE / "scripts_audit" / "route_sweep.py"

#: The sweep boots a brain and walks ~135 routes. Generous, because a
#: cold first boot builds an embedding queue and a vault; still bounded,
#: because the whole point is that it can never hang the suite.
SWEEP_TIMEOUT_S = 300


def test_the_sweep_script_is_present():
    """A test that shells out to a missing script passes vacuously."""
    assert SWEEP.is_file(), f"route sweep missing at {SWEEP}"


def test_no_registered_get_route_returns_5xx(tmp_path):
    """The whole point.

    A 5xx on an authenticated read is never correct. Every surface in
    this product is built out of these reads, so one that fails reaches
    the operator as an empty panel with no explanation.
    """
    env = dict(os.environ)
    env["FERAL_HOME"] = str(tmp_path / "sweep-home")
    os.makedirs(env["FERAL_HOME"], exist_ok=True)
    # Keep the sweep off any real provider; it must not need network.
    env.pop("OPENAI_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)

    try:
        proc = subprocess.run(
            [sys.executable, str(SWEEP)],
            cwd=str(FERAL_CORE),
            env=env,
            capture_output=True,
            text=True,
            timeout=SWEEP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"route sweep did not finish within {SWEEP_TIMEOUT_S}s. A route is "
            "wedged; run it by hand to see which:\n"
            f"    cd feral-core && FERAL_HOME=/tmp/sweep python {SWEEP.relative_to(FERAL_CORE)}"
        )

    if proc.returncode == 2:
        pytest.skip(f"sweep could not run in this environment:\n{proc.stdout}")

    assert proc.returncode == 0, (
        "one or more routes failed an authenticated GET:\n"
        + proc.stdout[-4000:]
        + ("\n--- stderr ---\n" + proc.stderr[-2000:] if proc.stderr else "")
    )


def test_exclusions_each_carry_a_reason():
    """Keeps the exclusion list from becoming somewhere to hide a
    failing route."""
    sys.path.insert(0, str(FERAL_CORE))
    from scripts_audit.route_sweep import EXCLUDED

    assert EXCLUDED
    for needle, reason in EXCLUDED.items():
        assert reason and len(reason) > 15, (
            f"exclusion {needle!r} needs a real justification, got {reason!r}"
        )
