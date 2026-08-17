"""Structured boot health report for FERAL Brain initialization."""
from __future__ import annotations
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("feral.boot")

_FIRST_PARTY_ROOTS: frozenset[str] | None = None


def first_party_roots() -> frozenset[str]:
    """Top-level package names that ship inside feral-core.

    Read off the tree rather than written down, because a hand-maintained
    list is exactly the thing that goes stale the week a package is added
    and then makes this classifier wrong in the quiet direction.

    ``boot_report.py`` lives at ``feral-core/api/``, so the core root is
    two parents up; every directory under it holding an ``__init__.py``
    is one of ours. ``build/`` and ``dist/`` are skipped: they are stale
    duplicates of the same names and contribute nothing.
    """
    global _FIRST_PARTY_ROOTS
    if _FIRST_PARTY_ROOTS is not None:
        return _FIRST_PARTY_ROOTS
    core_root = Path(__file__).resolve().parent.parent
    roots: set[str] = set()
    try:
        for child in core_root.iterdir():
            if not child.is_dir() or child.name in ("build", "dist"):
                continue
            if (child / "__init__.py").is_file():
                roots.add(child.name)
    except OSError as exc:
        # A boot classifier that cannot read its own tree must not claim
        # every import failure is a third-party one, so say so and treat
        # the set as empty (every ImportError then reports as SKIPPED,
        # the pre-existing behaviour, but loudly rather than silently).
        logger.warning(
            "[boot] could not enumerate first-party packages under %s (%s); "
            "an ImportError from FERAL's own code will be reported as a "
            "missing optional dependency.", core_root, exc,
        )
    _FIRST_PARTY_ROOTS = frozenset(roots)
    return _FIRST_PARTY_ROOTS


class SubsystemStatus(str, Enum):
    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"
    DEGRADED = "degraded"


@dataclass
class SubsystemReport:
    name: str
    status: SubsystemStatus
    message: str = ""
    elapsed_ms: float = 0.0
    optional: bool = True
    # Whether anything checked that this subsystem can do its job, as opposed
    # to merely checking that constructing it raised nothing. Most cannot:
    # see boot_subsystem for why the distinction is recorded rather than
    # assumed away.
    verified: bool = False


@dataclass
class BootReport:
    subsystems: list[SubsystemReport] = field(default_factory=list)
    total_elapsed_ms: float = 0.0
    current: Optional[str] = None

    def record(self, name: str, status: SubsystemStatus, message: str = "",
               elapsed_ms: float = 0.0, optional: bool = True,
               verified: bool = False):
        self.subsystems.append(SubsystemReport(
            name=name, status=status, message=message,
            elapsed_ms=elapsed_ms, optional=optional, verified=verified,
        ))

    def mark_degraded(self, name: str, message: str = ""):
        """Downgrade the most recent record for ``name`` to DEGRADED.

        Used when a subsystem started (no exception raised), but we've detected
        at runtime that it can't actually do useful work — e.g. DockerSandbox
        imported fine but Docker daemon isn't running.
        """
        for s in reversed(self.subsystems):
            if s.name == name:
                s.status = SubsystemStatus.DEGRADED
                if message:
                    s.message = message
                return
        self.record(name, SubsystemStatus.DEGRADED, message=message)

    @property
    def ok_count(self) -> int:
        return sum(1 for s in self.subsystems if s.status == SubsystemStatus.OK)

    @property
    def skipped_count(self) -> int:
        return sum(1 for s in self.subsystems if s.status == SubsystemStatus.SKIPPED)

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self.subsystems if s.status == SubsystemStatus.FAILED)

    @property
    def degraded_count(self) -> int:
        return sum(1 for s in self.subsystems if s.status == SubsystemStatus.DEGRADED)

    @property
    def verified_count(self) -> int:
        """OK subsystems that something actually checked the function of."""
        return sum(
            1 for s in self.subsystems
            if s.status == SubsystemStatus.OK and s.verified
        )

    def log_summary(self):
        logger.info("=" * 60)
        logger.info("FERAL Brain Boot Report")
        logger.info("=" * 60)
        icons = {"ok": "✓", "skipped": "○", "failed": "✗", "degraded": "⚠"}
        labels = {"ok": "OK", "skipped": "SKIP", "failed": "FAIL", "degraded": "DEGR"}
        for s in self.subsystems:
            icon = icons.get(s.status.value, "?")
            color_label = labels.get(s.status.value, s.status.value.upper())
            detail = f" — {s.message}" if s.message else ""
            ms = f" ({s.elapsed_ms:.0f}ms)" if s.elapsed_ms > 0 else ""
            logger.info(f"  {icon} [{color_label:4s}] {s.name}{ms}{detail}")
        logger.info("-" * 60)
        # "initialized" means constructed. Saying how many of those were
        # actually checked keeps the headline from implying the stronger
        # claim, which is the whole reason this line existed unqualified
        # while a subsystem underneath it failed every call it made.
        logger.info(
            f"  {self.ok_count} initialized "
            f"({self.verified_count} verified functional), "
            f"{self.degraded_count} degraded, "
            f"{self.skipped_count} skipped, {self.failed_count} failed "
            f"({self.total_elapsed_ms:.0f}ms total)"
        )
        logger.info("=" * 60)

    def to_dict(self) -> dict:
        return {
            "subsystems": [
                {"name": s.name, "status": s.status.value, "message": s.message,
                 "elapsed_ms": s.elapsed_ms, "verified": s.verified}
                for s in self.subsystems
            ],
            "current": self.current,
            "last": (
                {"name": self.subsystems[-1].name, "status": self.subsystems[-1].status.value}
                if self.subsystems else None
            ),
            "summary": {
                "ok": self.ok_count,
                "verified": self.verified_count,
                "degraded": self.degraded_count,
                "skipped": self.skipped_count,
                "failed": self.failed_count,
                "total_ms": self.total_elapsed_ms,
            },
        }


@contextmanager
def boot_subsystem(report: BootReport, name: str, optional: bool = True,
                   verify=None):
    """Record subsystem boot status, optionally checking that it works.

    Without *verify* this grades construction: the block did not raise, so
    the subsystem is OK. That is a weaker claim than the report reads as,
    and the gap is not hypothetical. LLMProvider reported OK on an install
    whose base_url pointed at a different vendor than its provider, and it
    went on to fail 610 consecutive calls with 401 while every boot said OK.

    *verify* closes that gap for one subsystem at a time. It is called after
    the block, so it can read whatever the block assigned, and it returns:

    * ``None`` or ``True`` — the subsystem was checked and works. Recorded
      OK with ``verified=True``.
    * a string — the subsystem constructed but cannot do its job, and the
      string says why. Recorded DEGRADED.

    A probe that raises is itself a degraded result rather than a boot
    failure: not knowing whether a subsystem works is not the same as
    knowing it does. Subsystems with no probe stay OK and unverified, which
    is exactly what the report should say about them, and the summary counts
    them separately so "37 initialized" does not silently mean "37 objects
    were constructed".
    """
    start = time.time()
    report.current = name
    try:
        yield
        elapsed = (time.time() - start) * 1000
        if verify is None:
            report.record(name, SubsystemStatus.OK, elapsed_ms=elapsed,
                          optional=optional)
        else:
            try:
                problem = verify()
            except Exception as probe_exc:
                report.record(
                    name, SubsystemStatus.DEGRADED,
                    message=f"health probe failed: {type(probe_exc).__name__}: "
                            f"{str(probe_exc)[:150]}",
                    elapsed_ms=elapsed, optional=optional,
                )
                logger.warning(
                    "[boot] %s constructed but its health probe failed: %s",
                    name, probe_exc,
                )
            else:
                if problem is None or problem is True:
                    report.record(name, SubsystemStatus.OK, elapsed_ms=elapsed,
                                  optional=optional, verified=True)
                else:
                    report.record(
                        name, SubsystemStatus.DEGRADED, message=str(problem),
                        elapsed_ms=elapsed, optional=optional, verified=True,
                    )
                    logger.warning(
                        "[boot] %s constructed but is not functional: %s",
                        name, problem,
                    )
    except ImportError as e:
        elapsed = (time.time() - start) * 1000
        # "Missing dependency" is a claim about a third party, and this
        # handler used to make it about every ImportError, silently. A
        # renamed helper, a circular import or a typo inside feral-core
        # raises ImportError too, and was filed as SKIPPED with no log
        # line at any level: the subsystem was gone, the boot report said
        # an optional extra was not installed, and no pip install could
        # ever fix it. The distinction is the same one memory/sqlite_features
        # draws between FTS5 and loadable extensions: absent third-party
        # code is a degrade, absent first-party code is a defect.
        failed_root = (getattr(e, "name", "") or "").split(".")[0]
        if failed_root and failed_root in first_party_roots():
            report.record(
                name, SubsystemStatus.FAILED,
                message=f"first-party import failed: {e}",
                elapsed_ms=elapsed, optional=optional,
            )
            logger.warning(
                "[boot] %s init failed (%s): %r is FERAL's own package, not an "
                "optional dependency, so this is a defect in the build or the "
                "import graph and no install will fix it: %s",
                name, "required" if not optional else "optional",
                failed_root, e,
            )
            if not optional:
                raise
        else:
            report.record(name, SubsystemStatus.SKIPPED,
                          message=f"Missing dependency: {e}", elapsed_ms=elapsed, optional=optional)
            # INFO, not silence. SKIPPED only reached the operator through
            # the end-of-boot summary, so a subsystem that vanished because
            # an extra was not installed left no line naming the subsystem
            # at the moment it was lost.
            logger.info(
                "[boot] %s skipped: optional dependency %s is not installed (%s)",
                name, failed_root or "(unknown)", e,
            )
            if not optional:
                raise
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        report.record(name, SubsystemStatus.FAILED,
                      message=str(e)[:200], elapsed_ms=elapsed, optional=optional)
        # v2026.5.29 — emit an immediate WARN with the exception so
        # operators can correlate "feature X doesn't work" with its
        # boot-time root cause without waiting for the end-of-boot
        # summary. Without this an optional subsystem (e.g. DigitalTwin)
        # could silently stay None and the only signal would be a 200
        # response with `answer: ""` from /api/digital-twin/ask.
        logger.warning(
            "[boot] %s init failed (%s): %s",
            name,
            "required" if not optional else "optional",
            str(e)[:200],
        )
        if not optional:
            raise
    finally:
        if report.current == name:
            report.current = None
