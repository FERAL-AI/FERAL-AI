"""``feral install`` has to say when the hot-reload did not happen.

``cli/install.py::_maybe_reload_skill`` POSTed ``/api/skills/reload`` and
then discarded the answer: the whole body was

    if resp.status_code < 400:
        return

inside a ``try`` whose ``except`` was ``pass``, and the function fell off
the end on every other path as well. A refused reload, a 500, an
unreachable brain and a successful reload were all the same ``None``, so
``cmd_install`` printed "Installed <name>. Ready to use." over a brain
that had not loaded the skill and would not until a restart. The
installer knew and could not say.

The 200-with-``ok: false`` case is tested separately because it is the
one a status check cannot catch: a brain that predates the reload-status
fix answers a reload that did nothing with HTTP 200 and no ``error``
key, and ``feral install`` talks to whatever brain is running, not to
the one in this checkout.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cli import install as install_mod


class FakeResponse:
    def __init__(self, status_code: int, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text or ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHttpx:
    """Just enough of ``httpx`` for ``_maybe_reload_skill``."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls: list[tuple] = []

    def post(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        if self.raises:
            raise self.raises
        return self.response


@pytest.fixture
def no_auth():
    """The auth header lookup touches ``~/.feral``; keep it out of the way."""
    with patch.object(install_mod, "_brain_auth_headers", return_value={}):
        yield


def _reload(fake, skill_id="weather_current"):
    with patch.object(install_mod, "httpx", fake), \
         patch.object(install_mod, "_brain_base_url", return_value="http://127.0.0.1:8765"):
        return install_mod._maybe_reload_skill(skill_id)


def test_confirms_a_reload_the_brain_confirmed(no_auth, capsys):
    ok = _reload(FakeHttpx(FakeResponse(200, {"ok": True, "skill_id": "weather_current"})))
    assert ok is True
    assert capsys.readouterr().out == "", "a reload that worked has nothing to report"


def test_reports_a_reload_the_brain_refused(no_auth, capsys):
    fake = FakeHttpx(FakeResponse(409, {
        "ok": False,
        "skill_id": "weather_current",
        "code": "no_source",
        "error": "nothing on disk to reload for 'weather_current'",
    }))
    ok = _reload(fake)

    assert ok is False, "a refused reload reported as success leaves the operator with a stale brain"
    out = capsys.readouterr().out
    assert "nothing on disk to reload" in out, "the brain's reason has to reach the operator"
    assert "Restart the brain" in out


def test_reports_a_200_that_says_ok_false(no_auth, capsys):
    """The shape a pre-fix brain sends. A status check alone cannot see it."""
    fake = FakeHttpx(FakeResponse(200, {"ok": False, "skill_id": "weather_current"}))
    ok = _reload(fake)

    assert ok is False
    out = capsys.readouterr().out
    assert "did not hot-reload weather_current" in out
    assert "Restart the brain" in out


def test_reports_a_brain_that_never_answered(no_auth, capsys):
    ok = _reload(FakeHttpx(raises=OSError("connection refused")))
    assert ok is False
    out = capsys.readouterr().out
    assert "No running brain answered" in out
    assert "next brain start" in out


def test_reports_a_500_with_no_json_body(no_auth, capsys):
    ok = _reload(FakeHttpx(FakeResponse(500, None, text="Internal Server Error")))
    assert ok is False
    out = capsys.readouterr().out
    assert "did not hot-reload" in out


def test_dispatch_install_hands_the_reload_outcome_to_the_caller(tmp_path, no_auth):
    """``cmd_install`` prints "Ready to use." from this return value, so a
    failed hot-reload has to travel all the way back up."""
    tarball = _one_file_tarball(tmp_path)

    fake_bad = FakeHttpx(FakeResponse(409, {"ok": False, "error": "no source"}))
    with patch.object(install_mod, "httpx", fake_bad), \
         patch.object(install_mod, "_brain_base_url", return_value="http://127.0.0.1:8765"):
        live = install_mod.dispatch_install(
            "skill", {"skill_id": "demo_skill"}, tarball, "demo_skill", tmp_path / "home",
        )
    assert live is False

    fake_ok = FakeHttpx(FakeResponse(200, {"ok": True}))
    with patch.object(install_mod, "httpx", fake_ok), \
         patch.object(install_mod, "_brain_base_url", return_value="http://127.0.0.1:8765"):
        live = install_mod.dispatch_install(
            "skill", {"skill_id": "demo_skill"}, tarball, "demo_skill", tmp_path / "home",
        )
    assert live is True


def _one_file_tarball(tmp_path: Path) -> Path:
    import tarfile

    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "manifest.json").write_text('{"skill_id": "demo_skill"}')
    tarball = tmp_path / "bundle.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(src / "manifest.json", arcname="manifest.json")
    return tarball
