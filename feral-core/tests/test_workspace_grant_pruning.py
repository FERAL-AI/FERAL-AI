"""Dead workspace grants, and the three places that stop them piling up.

``~/.feral/workspace_grants.json`` is the list of every folder the brain
may read and write. Nothing ever removed a row. Measured on a live
install: 876 grants, 174,897 bytes, of which 870 were pytest sandboxes
under ``/private/var/.../T/pytest-of-<user>/`` that pytest deleted long
ago, 4 were other temp directories, and 2 were folders the operator had
actually granted. The page that lists them rendered 2,665 lines, so the
security surface it exists to show could not be read.

Three things are pinned here:

* the pruning rule, which is narrow on purpose (a missing path that is
  not ephemeral is KEPT, because an unplugged drive looks the same as a
  deleted folder and losing an operator's grant is the worse failure)
* the one-time migration that cleans an existing file
* the conftest guard that fails the run if the suite writes to the real
  file again
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from security import sandbox_policy as sp
from security.sandbox_policy import (
    SandboxPolicy,
    is_ephemeral_grant_path,
    prune_dead_grants,
    prune_grants_file,
)

# A path that does not exist and never lived in temp storage. This is the
# case the rule must NOT touch: it is what an unmounted network share or
# an unplugged external drive looks like from here.
ABSENT_REAL = "/Volumes/BackupDrive/Projects/feral"


def _grant(mode: str = "readwrite") -> dict:
    return {"mode": mode, "granted_at": 1787284841.227888}


class TestTheRule:
    def test_a_pytest_sandbox_is_ephemeral(self):
        assert is_ephemeral_grant_path(
            "/private/var/folders/bn/x/T/pytest-of-mahmoudomar/pytest-184/"
            "test_a_running_background_job_0/work"
        )

    def test_the_system_temp_root_is_ephemeral(self, tmp_path):
        # tmp_path lives under tempfile.gettempdir() by construction.
        assert is_ephemeral_grant_path(str(tmp_path))

    def test_a_home_folder_is_not_ephemeral(self):
        assert not is_ephemeral_grant_path("/Users/mahmoudomar/Desktop")
        assert not is_ephemeral_grant_path(ABSENT_REAL)

    def test_a_dead_ephemeral_grant_is_dropped(self, tmp_path):
        gone = tmp_path / "pytest-of-someone" / "pytest-1" / "work"
        grants = {str(gone): _grant()}
        kept, dropped = prune_dead_grants(grants)
        assert kept == {}
        assert dropped == [str(gone)]

    def test_a_live_ephemeral_grant_is_kept(self, tmp_path):
        live = tmp_path / "work"
        live.mkdir()
        grants = {str(live): _grant()}
        kept, dropped = prune_dead_grants(grants)
        assert dropped == []
        assert kept == grants

    def test_a_missing_path_that_is_not_ephemeral_is_kept(self):
        """The conservative half of the rule, and the reason for it.

        An external drive that is unplugged and a network share that is
        not mounted are indistinguishable from a deleted folder. Dropping
        a grant the operator deliberately made is worse than keeping a
        dead row, so absence alone is never enough.
        """
        assert not Path(ABSENT_REAL).exists()
        grants = {ABSENT_REAL: _grant("read")}
        kept, dropped = prune_dead_grants(grants)
        assert dropped == []
        assert kept == grants

    def test_it_is_idempotent(self, tmp_path):
        live = tmp_path / "work"
        live.mkdir()
        grants = {
            str(tmp_path / "pytest-of-x" / "gone"): _grant(),
            str(live): _grant(),
            ABSENT_REAL: _grant("read"),
        }
        kept, dropped = prune_dead_grants(grants)
        assert len(dropped) == 1
        again, dropped_again = prune_dead_grants(kept)
        assert dropped_again == []
        assert again == kept

    def test_an_unreadable_path_counts_as_present(self, tmp_path, monkeypatch):
        """An I/O error is not evidence the folder is gone."""
        def explode(self):
            raise OSError("host is down")

        monkeypatch.setattr(Path, "exists", explode)
        target = str(tmp_path / "pytest-of-x" / "somewhere")
        kept, dropped = prune_dead_grants({target: _grant()})
        assert dropped == []
        assert target in kept

    def test_the_real_shape_collapses_to_the_two_real_grants(self, tmp_path):
        """The measured file, in miniature: 870 dead, 2 real."""
        grants = {}
        for i in range(870):
            grants[str(tmp_path / "pytest-of-me" / f"pytest-{i}" / "work")] = _grant()
        grants["/Users/mahmoudomar/Desktop"] = _grant()
        grants["/Users/mahmoudomar"] = _grant()
        kept, dropped = prune_dead_grants(grants)
        assert len(dropped) == 870
        assert sorted(kept) == ["/Users/mahmoudomar", "/Users/mahmoudomar/Desktop"]


class TestTheFile:
    def test_it_rewrites_only_when_something_left(self, tmp_path):
        gf = tmp_path / "workspace_grants.json"
        live = tmp_path / "live"
        live.mkdir()
        gf.write_text(json.dumps({
            str(tmp_path / "pytest-of-x" / "dead"): _grant(),
            str(live): _grant(),
        }))

        dropped, remaining = prune_grants_file(gf)
        assert len(dropped) == 1
        assert remaining == 1

        before = gf.stat().st_mtime_ns
        dropped, remaining = prune_grants_file(gf)
        assert dropped == []
        assert remaining == 1
        assert gf.stat().st_mtime_ns == before, "a clean file was rewritten anyway"

    def test_a_missing_or_broken_file_is_a_no_op(self, tmp_path):
        assert prune_grants_file(tmp_path / "nope.json") == ([], 0)
        broken = tmp_path / "broken.json"
        broken.write_text("{not json")
        assert prune_grants_file(broken) == ([], 0)
        assert broken.read_text() == "{not json"

    def test_a_json_list_is_left_alone(self, tmp_path):
        wrong = tmp_path / "wrong.json"
        wrong.write_text("[1, 2]")
        assert prune_grants_file(wrong) == ([], 0)


class TestThePolicy:
    def test_saving_prunes_and_says_so_once(self, tmp_path, monkeypatch, caplog):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(sp, "feral_home", lambda: home)
        live = tmp_path / "live"
        live.mkdir()

        policy = SandboxPolicy()
        with caplog.at_level(logging.INFO, logger="feral.sandbox_policy"):
            policy._save_grants({
                str(tmp_path / "pytest-of-x" / "dead-a"): _grant(),
                str(tmp_path / "pytest-of-x" / "dead-b"): _grant(),
                str(live): _grant(),
            })

        on_disk = json.loads((home / "workspace_grants.json").read_text())
        assert list(on_disk) == [str(live)]
        lines = [r for r in caplog.records if "pruned" in r.getMessage()]
        assert len(lines) == 1, [r.getMessage() for r in lines]
        assert "2 dead" in lines[0].getMessage()

    def test_loading_does_not_prune(self, tmp_path, monkeypatch):
        """``_load_grants`` runs on every file tool call and every shell
        command, so it must not stat the whole grant list. Pruning there
        would also be pointless: a folder that no longer exists cannot
        grant access to anything."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(sp, "feral_home", lambda: home)
        dead = str(tmp_path / "pytest-of-x" / "dead")
        gf = home / "workspace_grants.json"
        gf.write_text(json.dumps({dead: _grant()}))
        before = gf.stat().st_mtime_ns

        assert list(SandboxPolicy()._load_grants()) == [dead]
        assert gf.stat().st_mtime_ns == before

    def test_granting_a_folder_sweeps_the_dead_ones(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(sp, "feral_home", lambda: home)
        (home / "workspace_grants.json").write_text(json.dumps({
            str(tmp_path / "pytest-of-x" / "dead"): _grant(),
        }))
        wanted = tmp_path / "project"
        wanted.mkdir()

        policy = SandboxPolicy()
        assert policy.grant_folder(str(wanted), mode="readwrite")["ok"] is True
        assert [g["path"] for g in policy.list_grants()] == [str(wanted.resolve())]

    def test_revoking_does_not_take_a_real_grant_with_it(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(sp, "feral_home", lambda: home)
        (home / "workspace_grants.json").write_text(json.dumps({
            ABSENT_REAL: _grant("read"),
            str(tmp_path / "pytest-of-x" / "dead"): _grant(),
        }))
        policy = SandboxPolicy()
        assert policy.revoke_folder(str(tmp_path / "pytest-of-x" / "dead")) is True
        assert [g["path"] for g in policy.list_grants()] == [ABSENT_REAL]


class TestTheMigration:
    NAME = "1788609600_prune_ephemeral_workspace_grants"

    @pytest.fixture
    def module(self):
        from migrations import runner
        path = runner.migrations_dir() / f"{self.NAME}.py"
        assert path.exists(), f"migration file is missing: {path}"
        return runner._load(self.NAME, path)

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        h = tmp_path / "feral-home"
        h.mkdir()
        import config.loader as loader
        monkeypatch.setattr(loader, "feral_home", lambda: h)
        monkeypatch.setattr(loader, "feral_data_home", lambda: h)
        monkeypatch.setattr(sp, "feral_home", lambda: h)
        return h

    def test_it_is_discovered_by_the_runner(self):
        from migrations import runner
        assert self.NAME in dict(runner._discover())

    def test_it_is_not_a_recurring_sweep(self, module):
        """``_save_grants`` prunes on every grant and revoke, so the file
        cannot grow back and a per-boot stat pass would buy nothing."""
        assert getattr(module, "RECURRING", False) is False

    def test_no_grants_file_is_a_no_op(self, module, home):
        detail, changed = module.migrate()
        assert changed is False
        assert "no workspace grants" in detail

    def test_it_removes_the_dead_and_reports_the_count(self, module, home, tmp_path):
        live = tmp_path / "project"
        live.mkdir()
        grants = {str(live): _grant(), ABSENT_REAL: _grant("read")}
        for i in range(870):
            grants[str(tmp_path / "pytest-of-me" / f"pytest-{i}" / "work")] = _grant()
        (home / "workspace_grants.json").write_text(json.dumps(grants))

        detail, changed = module.migrate()
        assert changed is True
        assert "870" in detail
        left = json.loads((home / "workspace_grants.json").read_text())
        assert sorted(left) == sorted([str(live), ABSENT_REAL])

    def test_running_it_twice_changes_nothing(self, module, home, tmp_path):
        live = tmp_path / "project"
        live.mkdir()
        (home / "workspace_grants.json").write_text(json.dumps({
            str(live): _grant(),
            str(tmp_path / "pytest-of-x" / "dead"): _grant(),
        }))

        first = module.migrate()
        assert first[1] is True
        after_first = (home / "workspace_grants.json").read_text()

        second = module.migrate()
        assert second[1] is False
        assert "already clean" in second[0]
        assert (home / "workspace_grants.json").read_text() == after_first

    def test_it_never_removes_a_live_path(self, module, home, tmp_path):
        live = tmp_path / "work"
        live.mkdir()
        (home / "workspace_grants.json").write_text(json.dumps({str(live): _grant()}))
        detail, changed = module.migrate()
        assert changed is False
        assert json.loads((home / "workspace_grants.json").read_text()) == {
            str(live): _grant()
        }

    def test_a_broken_file_does_not_wedge_the_boot(self, module, home):
        (home / "workspace_grants.json").write_text("{not json")
        detail, changed = module.migrate()
        assert changed is False
        assert (home / "workspace_grants.json").read_text() == "{not json"


class TestTheGuard:
    """The conftest fixture that fails the run if the real file is touched."""

    def test_it_watches_the_operators_own_file(self):
        from tests.conftest import real_workspace_grants_path
        path = real_workspace_grants_path()
        assert path == Path.home() / ".feral" / "workspace_grants.json"
        assert "FERAL_HOME" not in str(path)

    def test_the_fingerprint_notices_a_write(self, tmp_path):
        from tests.conftest import grants_fingerprint
        target = tmp_path / "workspace_grants.json"
        absent = grants_fingerprint(target)
        assert absent[0] == "absent"

        target.write_text("{}")
        created = grants_fingerprint(target)
        assert created != absent

        target.write_text(json.dumps({"/Users/me": _grant()}))
        assert grants_fingerprint(target) != created

    def test_the_fingerprint_is_stable_when_nothing_happens(self, tmp_path):
        from tests.conftest import grants_fingerprint
        target = tmp_path / "workspace_grants.json"
        target.write_text("{}")
        assert grants_fingerprint(target) == grants_fingerprint(target)

    def test_the_suite_wide_fixture_is_session_scoped_and_autouse(self):
        """A per-test check would be too slow and would name the wrong
        test anyway; the leak is only visible across a whole run."""
        from tests import conftest
        marker = conftest.real_workspace_grants_untouched._pytestfixturefunction
        assert marker.scope == "session"
        assert marker.autouse is True
