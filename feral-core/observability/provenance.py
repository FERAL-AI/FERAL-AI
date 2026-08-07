"""Which copy of the code is actually running.

A whole afternoon of fixes was committed, the brain was restarted, and
nothing changed. The cause was not the fixes: `feral start` imported an
installed copy under site-packages that had been built hours earlier,
while the edits lived in the git working tree. Both existed, both were
importable, and no surface anywhere said which one was in use. The only
symptom was that a bug the author had just proven fixed kept happening.

That is a nasty failure because it discredits real work. The fix on this
machine was an editable install, but the reason it went unnoticed for
hours is that the running process never stated its own origin. So it
states it now, at boot, in one line: the directory the code was loaded
from, the commit, and whether that tree has uncommitted changes.

`git describe`-style information is deliberately gathered with a
subprocess rather than a library: git may be absent, the code may be
running from a wheel with no repository at all, and none of that is an
error. Unknown provenance is reported as unknown, never guessed.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class CodeProvenance:
    root: str
    commit: Optional[str] = None
    dirty: bool = False
    editable: Optional[bool] = None

    def one_line(self) -> str:
        """A single log line an operator can grep after a restart."""
        if self.commit:
            rev = self.commit + ("+dirty" if self.dirty else "")
        else:
            rev = "no git metadata"
        install = ""
        if self.editable is True:
            install = ", editable install"
        elif self.editable is False:
            # The exact configuration that wasted the afternoon: edits in a
            # checkout that the running process does not import.
            install = ", installed copy (edits to a checkout will NOT apply)"
        return f"running from {self.root} ({rev}{install})"


def _git(root: str, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True, text=True, timeout=5.0,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip()


def _is_editable(root: str) -> Optional[bool]:
    """True when the import path is the source tree rather than a copy.

    Determined by location rather than by parsing installer metadata,
    because the question that matters is only ever "is the code I am
    editing the code that is running".
    """
    try:
        import site

        for site_dir in site.getsitepackages():
            if os.path.commonpath([os.path.realpath(root),
                                   os.path.realpath(site_dir)]) == os.path.realpath(site_dir):
                return False
        return True
    except Exception:
        return None


def describe(module: Optional[object] = None) -> CodeProvenance:
    """Describe the origin of the running code.

    *module* defaults to this one; pass any module to ask where the code
    that imported it came from.
    """
    if module is None:
        module = describe.__module__ and __import__(__name__, fromlist=["*"])
    path = getattr(module, "__file__", None) or __file__
    root = os.path.dirname(os.path.dirname(os.path.abspath(path)))

    toplevel = _git(root, "rev-parse", "--show-toplevel")
    if toplevel:
        root = toplevel
        commit = _git(root, "rev-parse", "--short", "HEAD")
        # --porcelain is empty exactly when the tree is clean.
        status = _git(root, "status", "--porcelain")
        dirty = bool(status)
    else:
        commit, dirty = None, False

    return CodeProvenance(
        root=root, commit=commit, dirty=dirty, editable=_is_editable(root),
    )
