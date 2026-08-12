"""F-12 — the `[wake]` extra cannot be installed on Linux above Python 3.11.

`[wake]` pulls `openwakeword`, whose metadata carries::

    tflite-runtime <3,>=2.8.0 ; platform_system == "Linux"

`tflite-runtime` publishes wheels for cp38 through cp311 and **no sdist at
all**, so pip cannot even attempt a build. Measured from this tree::

    $ pip download "tflite-runtime>=2.8.0,<3" --no-deps --only-binary=:all: \\
        --python-version 3.11 --platform manylinux2014_x86_64
    Saved tflite_runtime-2.14.0-cp311-cp311-manylinux2014_x86_64.whl

    $ ... --python-version 3.12 ...
    ERROR: Could not find a version that satisfies the requirement
           tflite-runtime<3,>=2.8.0 (from versions: none)

    $ ... --python-version 3.14 ...   # same error

**The audit is imprecise in both directions and both matter.**

Narrower than stated: the marker is `platform_system == "Linux"`, so macOS
never pulls `tflite-runtime` and `[wake]` installs cleanly on macOS 3.12+.
macOS is the flagship platform, which is why nobody hit this.

Wider than stated: 2.14.0 is the newest release, from 2023, and the ceiling is
cp311 for 3.12, 3.13 *and* 3.14. There is no version of this that a `pip`
upgrade fixes.

"Nothing gates it" is the part with teeth. `openwakeword` was removed from
`[all]` in 2026.4.11 for exactly this reason, and nothing has stopped anyone
putting it back. `[all]` is what `scripts/install.sh` runs and what the
pre-publish install smoke now installs, so a regression there breaks every
Linux install on a modern Python.
"""

from __future__ import annotations

from pathlib import Path

import pytest


tomllib = pytest.importorskip("tomllib")

FERAL_CORE = Path(__file__).resolve().parents[1]
PYPROJECT = FERAL_CORE / "pyproject.toml"

# The newest CPython for which tflite-runtime publishes a Linux wheel.
TFLITE_LAST_SUPPORTED_MINOR = 11


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _extras() -> dict[str, list[str]]:
    return _pyproject()["project"]["optional-dependencies"]


def test_openwakeword_stays_out_of_the_all_extra():
    """The 2026.4.11 regression, which nothing currently guards.

    `pip install feral-ai[all]` must keep working on Linux 3.12+, and it only
    does so as long as nothing in `[all]` reaches tflite-runtime.
    """
    all_extra = _extras()["all"]
    offenders = [r for r in all_extra if "openwakeword" in r or "tflite" in r]
    assert not offenders, (
        "[all] pulls a package that requires tflite-runtime on Linux, which "
        f"has no wheel above CPython 3.{TFLITE_LAST_SUPPORTED_MINOR} and no "
        f"sdist: {offenders}. This broke `pip install feral-ai[all]` once "
        "already (2026.4.11)."
    )


def test_openwakeword_stays_out_of_the_base_dependencies():
    """Same reasoning, one level worse: this would break a plain install."""
    base = _pyproject()["project"]["dependencies"]
    offenders = [r for r in base if "openwakeword" in r or "tflite" in r]
    assert not offenders, (
        f"a base dependency reaches tflite-runtime: {offenders}. "
        "`pip install feral-ai` would fail outright on Linux 3.12+."
    )


def test_the_wake_extra_documents_its_platform_ceiling():
    """The repo's convention is a written reason at every bound.

    `[wake]` had no comment at all, so the constraint lived only in a
    CHANGELOG entry from four months earlier and in whatever pip printed.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    wake_index = text.index("\nwake = [")
    # The comment block immediately above the extra.
    preceding = text[max(0, wake_index - 2000):wake_index]
    block = preceding.rsplit("]\n", 1)[-1]
    assert "tflite" in block.lower(), (
        "the [wake] extra does not say that it reaches tflite-runtime, so the "
        "next person to add it to [all] has nothing to read"
    )
    assert "3.11" in block, (
        "the [wake] extra does not state the CPython ceiling"
    )
    assert "linux" in block.lower(), (
        "the [wake] extra does not say the ceiling is Linux-only; without that "
        "someone will 'fix' it by removing the extra macOS users rely on"
    )


def test_the_cli_hint_does_not_send_unsupported_users_to_a_dead_command():
    """`feral wake-test` printed `pip install 'feral-ai[wake]'` unconditionally.

    On Linux 3.12+ that command cannot succeed, so the user is told to run
    something that will fail, with no explanation of why.
    """
    source = (FERAL_CORE / "cli" / "main.py").read_text(encoding="utf-8")
    start = source.index("def cmd_wake_test(")
    body = source[start:start + 2000]
    assert "feral-ai[wake]" in body, (
        "cmd_wake_test no longer offers the extra; re-derive this test"
    )
    assert "tflite" in body.lower(), (
        "cmd_wake_test tells every user to install [wake] without mentioning "
        "that it cannot resolve on Linux above CPython "
        f"3.{TFLITE_LAST_SUPPORTED_MINOR}"
    )
