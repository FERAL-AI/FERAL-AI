"""The running process must say which copy of the code it is.

An afternoon of committed, test-passing fixes appeared to do nothing after
a restart. The fixes were fine. `feral start` imported an installed copy
under site-packages built hours earlier, while the edits lived in the git
working tree. Both were importable and no surface named which was in use,
so the only symptom was a bug that had been proven fixed still occurring,
which reads exactly like the fix not working.

The lasting protection is not the editable install, which is one machine's
configuration. It is that the process now states its origin at boot, so
"is my change actually running" is a grep rather than an inference.
"""

from __future__ import annotations

import os

from observability.provenance import CodeProvenance, describe


class TestDescribesTheRealTree:
    def test_it_finds_this_repository(self):
        import memory.store as m

        p = describe(m)
        assert os.path.isdir(p.root)
        # Whatever the checkout is called, the module must live under it.
        assert os.path.commonpath([
            os.path.realpath(p.root), os.path.realpath(m.__file__)
        ]) == os.path.realpath(p.root)

    def test_it_reports_a_commit_for_a_git_checkout(self):
        import memory.store as m

        p = describe(m)
        assert p.commit, "no commit reported for a git checkout"
        assert len(p.commit) >= 7

    def test_running_from_a_checkout_is_reported_as_editable(self):
        import memory.store as m

        assert describe(m).editable is True


class TestTheLineAnOperatorReads:
    def test_an_installed_copy_says_edits_will_not_apply(self):
        """The whole point. This is the state that wasted the afternoon, and
        the line has to name the consequence, not just the location."""
        p = CodeProvenance(root="/usr/lib/python3/site-packages",
                           commit=None, dirty=False, editable=False)
        line = p.one_line()
        assert "installed copy" in line
        assert "NOT apply" in line

    def test_a_dirty_checkout_is_marked(self):
        p = CodeProvenance(root="/repo", commit="abc1234", dirty=True, editable=True)
        assert "abc1234+dirty" in p.one_line()

    def test_a_clean_checkout_is_not_marked_dirty(self):
        p = CodeProvenance(root="/repo", commit="abc1234", dirty=False, editable=True)
        assert "dirty" not in p.one_line()

    def test_missing_git_metadata_is_stated_not_guessed(self):
        """Running from a wheel with no repository is normal, not an error."""
        p = CodeProvenance(root="/opt/app", commit=None, dirty=False, editable=None)
        assert "no git metadata" in p.one_line()


class TestItNeverBreaksBoot:
    def test_a_module_without_a_file_does_not_raise(self):
        """Namespace packages and frozen modules have no __file__; boot must
        survive not knowing, because an unknown origin is not a failure."""
        class Fake:
            pass

        p = describe(Fake())
        assert isinstance(p, CodeProvenance)
        assert p.one_line()

    def test_describe_is_cheap_enough_for_boot(self):
        """It runs on every start, so it must not stall it."""
        import time

        import memory.store as m

        start = time.time()
        describe(m)
        assert time.time() - start < 5.0
