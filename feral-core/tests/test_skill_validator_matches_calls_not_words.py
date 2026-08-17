"""The dangerous-call check must match calls, not words that contain them.

``SkillValidator._check_python_code`` scanned the raw source with
``if call in source``. ``exec`` is in ``DANGEROUS_CALLS`` and is also a
substring of ``execute``, which is the mandatory entry point of every
Python skill: ``BaseSkill.execute(self, endpoint_id, args, vault)``.

So the validator rejected 28 of the 29 shipped skills, 25 of them on that
collision alone. And because ``SkillValidator`` gates
``MarketplaceClient.preview_from_registry``, the web Marketplace install
and every app ``skill_dependencies`` resolution refused any skill carrying
Python at all. A third party could not ship a working skill through the
consent flow, which is the flow this repo spent a release building.

These tests hold both directions: real calls are still caught, and words
that merely contain them are not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills.package import SkillPackage, SkillValidator

REPO_SKILLS = Path(__file__).resolve().parents[1] / "skills" / "impl"


def _package(tmp_path: Path, impl: str) -> SkillPackage:
    """A minimal package on disk whose manifest is valid, so the only
    issues returned come from the code scan."""
    (tmp_path / "manifest.json").write_text(json.dumps({
        "skill_id": "probe_skill",
        "version": "1.0.0",
        "description": "A probe skill used by the validator tests.",
        "brand": {"name": "Probe Skill"},
        "endpoints": [],
    }))
    (tmp_path / "impl.py").write_text(impl)
    return SkillPackage(tmp_path)


def _security_issues(tmp_path: Path, impl: str) -> list[str]:
    return [i for i in SkillValidator().validate(_package(tmp_path, impl)) if "SECURITY" in i]


class TestAWordIsNotACall:
    def test_the_mandatory_entry_point_is_not_flagged(self, tmp_path):
        """This is the whole bug. `execute` contains `exec`."""
        impl = (
            "from skills.base import BaseSkill\n"
            "class ProbeSkill(BaseSkill):\n"
            "    async def execute(self, endpoint_id, args, vault):\n"
            "        return {'success': True}\n"
        )
        assert _security_issues(tmp_path, impl) == []

    def test_the_word_in_a_docstring_is_not_flagged(self, tmp_path):
        impl = 'def f():\n    """This skill will never exec or eval anything."""\n    return 1\n'
        assert _security_issues(tmp_path, impl) == []

    def test_a_variable_named_after_a_call_is_not_flagged(self, tmp_path):
        # `execution_log` and `evaluation` both contain a dangerous substring.
        impl = "def f():\n    execution_log = []\n    evaluation = 0\n    return execution_log, evaluation\n"
        assert _security_issues(tmp_path, impl) == []


class TestARealCallIsStillCaught:
    @pytest.mark.parametrize("impl,expected", [
        ("exec('payload')", "exec"),
        ("eval('1+1')", "eval"),
        ("__import__('os')", "__import__"),
        ("import os\nos.system('rm -rf /')", "os.system"),
        ("import os\nos.popen('ls')", "os.popen"),
        ("import os\nos.remove('/etc/passwd')", "os.remove"),
        ("import os\nos.unlink('/etc/passwd')", "os.unlink"),
        ("import shutil\nshutil.rmtree('/')", "shutil.rmtree"),
        ("import subprocess\nsubprocess.run(['ls'])", "subprocess.run"),
        ("import subprocess\nsubprocess.call(['ls'])", "subprocess.call"),
        ("import subprocess\nsubprocess.Popen(['ls'])", "subprocess.Popen"),
    ])
    def test_each_dangerous_call_is_reported(self, tmp_path, impl, expected):
        issues = _security_issues(tmp_path, impl)
        assert any(f"'{expected}'" in i for i in issues), f"{expected} not reported: {issues}"

    def test_a_dangerous_call_nested_inside_the_entry_point_is_caught(self, tmp_path):
        """The realistic shape: a skill that looks ordinary and shells out."""
        impl = (
            "import os\n"
            "from skills.base import BaseSkill\n"
            "class ProbeSkill(BaseSkill):\n"
            "    async def execute(self, endpoint_id, args, vault):\n"
            "        os.system(args['cmd'])\n"
            "        return {'success': True}\n"
        )
        issues = _security_issues(tmp_path, impl)
        assert any("'os.system'" in i for i in issues), issues

    def test_every_dangerous_call_name_is_still_reachable(self, tmp_path):
        """Guards the matcher against a name shape it cannot express.

        Every entry in DANGEROUS_CALLS is either a bare name or a single
        dotted attribute today. If someone adds a deeper path, this fails
        until the matcher and this fixture agree.
        """
        for name in sorted(SkillValidator.DANGEROUS_CALLS):
            impl = f"{name}('x')" if "." not in name else (
                f"import {name.split('.')[0]}\n{name}('x')"
            )
            issues = _security_issues(tmp_path, impl)
            assert any(f"'{name}'" in i for i in issues), f"{name} unreachable: {issues}"


class TestTheShippedSkillsPass:
    # The three that legitimately do what they are flagged for. These are
    # computer-use skills whose stated purpose is running programs, so the
    # scan reporting them is correct, not a false positive. Named rather
    # than pattern-excluded, so a fourth cannot join quietly.
    KNOWN_REAL_CALLERS = {
        "browser_use.py",          # subprocess.Popen, launches a browser
        "gui_computer_use.py",     # subprocess.run + os.unlink, screen automation
        "self_introspection.py",   # imports socket
    }

    def test_no_skill_is_flagged_for_a_word_it_merely_contains(self):
        """The measurement that made this a release blocker.

        Before the fix: 28 of 29 shipped skills flagged, 25 of them solely
        on the exec/eval substring, because every skill must define
        ``execute``. Since SkillValidator gates the Marketplace preview,
        that meant no third-party skill carrying Python could be installed
        or resolved as an app dependency.

        The invariant is not "no skill has dangerous calls". Three of them
        genuinely do, and reporting those is the scan working. It is that
        no skill is flagged for something it does not actually call.
        """
        validator = SkillValidator()
        offenders: dict[str, list[str]] = {}
        for impl in sorted(REPO_SKILLS.glob("*.py")):
            if impl.name == "__init__.py" or impl.name in self.KNOWN_REAL_CALLERS:
                continue
            issues = [i for i in validator._check_python_code(impl) if "SECURITY" in i]
            if issues:
                offenders[impl.name] = issues

        assert offenders == {}, (
            "skills flagged without making a dangerous call: "
            f"{offenders}. If one of these genuinely calls it, fix the skill "
            "or add it to KNOWN_REAL_CALLERS with the reason."
        )

    def test_the_known_real_callers_really_do_call_what_they_are_flagged_for(self):
        """Keeps the allowlist above honest.

        An entry that stops making the call should leave the list rather
        than sit there granting a permanent exemption, which is how an
        allowlist rots into a blanket suppression.
        """
        validator = SkillValidator()
        for name in sorted(self.KNOWN_REAL_CALLERS):
            path = REPO_SKILLS / name
            assert path.exists(), f"{name} is allowlisted but no longer exists"
            issues = [i for i in validator._check_python_code(path) if "SECURITY" in i]
            assert issues, (
                f"{name} is in KNOWN_REAL_CALLERS but no longer trips the scan. "
                "Remove it from the list."
            )
