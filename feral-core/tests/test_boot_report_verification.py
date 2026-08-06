"""OK must not mean "the constructor did not raise".

boot_subsystem graded construction. Fifty subsystems used it and exactly
one, DockerSandbox, checked afterwards whether the thing it built could do
anything, through sixteen lines of mark_degraded boilerplate that no other
call site copied.

The gap is not hypothetical. LLMProvider is registered with optional=False
and reported OK on an install configured with provider=openrouter and
base_url=https://api.anthropic.com/v1. Constructing it raises nothing when
the endpoint belongs to another vendor, so every boot said OK while the
brain logged 610 consecutive 401s and failed over in silence.

So subsystems may now supply a probe, and the report records whether one
ran. A subsystem with no probe is still OK, which is honest, but it is
counted separately: "37 initialized" must not quietly mean "37 objects
were constructed".
"""

from __future__ import annotations

import pytest

from api.boot_report import (
    BootReport,
    SubsystemStatus,
    boot_subsystem,
)


class TestUnverifiedStaysHonest:
    def test_a_subsystem_without_a_probe_is_ok_but_unverified(self):
        report = BootReport()
        with boot_subsystem(report, "Thing"):
            pass

        s = report.subsystems[0]
        assert s.status == SubsystemStatus.OK
        assert s.verified is False
        assert report.ok_count == 1
        assert report.verified_count == 0

    def test_the_summary_separates_the_two_counts(self):
        report = BootReport()
        with boot_subsystem(report, "Unchecked"):
            pass
        with boot_subsystem(report, "Checked", verify=lambda: None):
            pass

        assert report.ok_count == 2
        assert report.verified_count == 1
        assert report.to_dict()["summary"]["verified"] == 1


class TestProbesDowngradeWhatDoesNotWork:
    def test_a_probe_returning_a_reason_degrades_the_subsystem(self):
        report = BootReport()
        with boot_subsystem(report, "DockerSandbox",
                            verify=lambda: "Docker daemon not running"):
            pass

        s = report.subsystems[0]
        assert s.status == SubsystemStatus.DEGRADED
        assert "Docker daemon not running" in s.message
        assert report.ok_count == 0

    def test_a_probe_returning_none_verifies_the_subsystem(self):
        report = BootReport()
        with boot_subsystem(report, "Thing", verify=lambda: None):
            pass

        s = report.subsystems[0]
        assert s.status == SubsystemStatus.OK
        assert s.verified is True

    def test_a_probe_returning_true_also_verifies(self):
        report = BootReport()
        with boot_subsystem(report, "Thing", verify=lambda: True):
            pass
        assert report.subsystems[0].status == SubsystemStatus.OK
        assert report.subsystems[0].verified is True

    def test_the_probe_runs_after_the_block_so_it_sees_the_result(self):
        """Probes read what the block assigned, which is the only way a
        health check on a just-constructed object can work."""
        report = BootReport()
        built = {}

        with boot_subsystem(report, "Thing",
                            verify=lambda: None if built.get("ok") else "not built"):
            built["ok"] = True

        assert report.subsystems[0].status == SubsystemStatus.OK

    def test_a_raising_probe_degrades_rather_than_fails_the_boot(self):
        """Not knowing whether a subsystem works is not the same as knowing
        it does not, and a broken probe must not take the brain down."""
        def _boom():
            raise RuntimeError("probe exploded")

        report = BootReport()
        with boot_subsystem(report, "Thing", optional=False, verify=_boom):
            pass

        s = report.subsystems[0]
        assert s.status == SubsystemStatus.DEGRADED
        assert "probe exploded" in s.message

    def test_a_probe_does_not_run_when_construction_failed(self):
        """A failed subsystem is already reported; probing its half-built
        state would only overwrite the real error with a confusing one."""
        calls = []
        report = BootReport()
        with pytest.raises(ValueError):
            with boot_subsystem(report, "Thing", optional=False,
                                verify=lambda: calls.append(1)):
                raise ValueError("construction failed")

        assert calls == []
        assert report.subsystems[0].status == SubsystemStatus.FAILED
        assert "construction failed" in report.subsystems[0].message


class TestTheLLMCoherenceProbe:
    """The specific config that burned this install, pinned end to end."""

    @staticmethod
    def _probe(provider, base_url):
        from providers.catalog import provider_base_url_mismatch
        return provider_base_url_mismatch(provider, base_url)

    def test_openrouter_pointed_at_anthropic_is_degraded(self):
        report = BootReport()
        with boot_subsystem(
            report, "LLMProvider", optional=False,
            verify=lambda: self._probe("openrouter", "https://api.anthropic.com/v1"),
        ):
            pass

        s = report.subsystems[0]
        assert s.status == SubsystemStatus.DEGRADED, "boot still reports OK"
        assert "anthropic" in s.message.lower()

    def test_a_coherent_config_boots_verified(self):
        report = BootReport()
        with boot_subsystem(
            report, "LLMProvider", optional=False,
            verify=lambda: self._probe("openrouter", None),
        ):
            pass

        assert report.subsystems[0].status == SubsystemStatus.OK
        assert report.subsystems[0].verified is True

    def test_a_self_hosted_endpoint_is_not_flagged(self):
        """Proxies and local models are why base_url exists; a check that
        fired on them is one operators learn to ignore."""
        report = BootReport()
        with boot_subsystem(
            report, "LLMProvider",
            verify=lambda: self._probe("openai", "http://localhost:1234/v1"),
        ):
            pass
        assert report.subsystems[0].status == SubsystemStatus.OK


def test_the_summary_line_states_what_ok_means(caplog):
    report = BootReport()
    with boot_subsystem(report, "Unchecked"):
        pass
    with boot_subsystem(report, "Checked", verify=lambda: None):
        pass

    with caplog.at_level("INFO", logger="feral.boot"):
        report.log_summary()

    assert "2 initialized (1 verified functional)" in caplog.text
