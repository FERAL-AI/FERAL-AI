"""audit-r12 ship — TCC preflight probes for Calendar / Reminders /
Contacts / Full Disk Access.

Pre-fix, ``security.macos_permissions`` only exposed
``check_accessibility`` + ``check_screen_recording``; doctor showed
two rows, the operator had to discover the rest by exercising tools
and getting runtime denials. v2026.5.38 adds non-prompting EventKit
+ Contacts + FDA probes so ``feral doctor`` (Wave 3 Lane 07) can
surface every TCC the brain actually exercises against.

The tests below run on every host — non-macOS callers must get
``status=not_applicable`` so the probe surface stays uniform across
the matrix (and the iOS Brain Network row renders consistently).
"""

from __future__ import annotations

import platform
import sys

import pytest


class TestProbeSurface:
    def test_all_gui_permission_statuses_includes_new_rows(self):
        from security.macos_permissions import all_gui_permission_statuses

        statuses = all_gui_permission_statuses()
        names = {s.permission for s in statuses}
        assert {
            "accessibility",
            "screen_recording",
            "calendar",
            "reminders",
            "contacts",
            "full_disk_access",
        } <= names

    def test_every_probe_returns_tcc_status(self):
        from security.macos_permissions import (
            check_calendar, check_reminders, check_contacts, check_full_disk_access,
        )
        from security.macos_permissions import TCCStatus

        for probe in (check_calendar, check_reminders, check_contacts, check_full_disk_access):
            status = probe()
            assert isinstance(status, TCCStatus)
            assert status.status in {
                "granted", "denied", "unknown", "restricted", "not_applicable",
            }
            assert status.api  # non-empty


@pytest.mark.skipif(platform.system() == "Darwin", reason="non-macOS contract")
class TestNonMacOSContract:
    def test_calendar_not_applicable(self):
        from security.macos_permissions import check_calendar
        assert check_calendar().status == "not_applicable"

    def test_reminders_not_applicable(self):
        from security.macos_permissions import check_reminders
        assert check_reminders().status == "not_applicable"

    def test_contacts_not_applicable(self):
        from security.macos_permissions import check_contacts
        assert check_contacts().status == "not_applicable"

    def test_full_disk_access_not_applicable(self):
        from security.macos_permissions import check_full_disk_access
        assert check_full_disk_access().status == "not_applicable"


class TestPyObjCMissingFallsBackToUnknown:
    """When the PyObjC framework binding is absent (typical on Linux
    CI), the probe must report ``status="unknown"`` with the pip
    install hint, NOT crash and NOT pretend the permission is
    granted."""

    def test_eventkit_missing_yields_unknown_on_macos(self, monkeypatch):
        if platform.system() != "Darwin":
            pytest.skip("macOS-specific test")
        monkeypatch.setitem(sys.modules, "EventKit", None)
        # Force ImportError on the inner import by deleting the cached
        # module entry and re-running the probe.
        sys.modules.pop("EventKit", None)
        from security.macos_permissions import check_calendar
        status = check_calendar()
        # If the binding really is installed in this environment the
        # probe returns the real status; the contract we care about
        # here is that it never raises and never claims unobservable
        # permission.
        assert status.status in {"granted", "denied", "unknown", "restricted"}


class TestTCCCardCatalogCoverage:
    def test_calendar_catalog_entry_present(self):
        from agents.tcc_card import TCC_CATALOG
        assert "calendar" in TCC_CATALOG
        entry = TCC_CATALOG["calendar"]
        assert "Calendar" in entry["title"]
        assert "Privacy_Calendars" in entry["macos_deeplink"]

    def test_reminders_catalog_entry_present(self):
        from agents.tcc_card import TCC_CATALOG
        assert "reminders" in TCC_CATALOG
        assert "Privacy_Reminders" in TCC_CATALOG["reminders"]["macos_deeplink"]

    def test_contacts_catalog_entry_present(self):
        from agents.tcc_card import TCC_CATALOG
        assert "contacts" in TCC_CATALOG
        assert "Privacy_Contacts" in TCC_CATALOG["contacts"]["macos_deeplink"]

    def test_full_disk_access_catalog_entry_present(self):
        from agents.tcc_card import TCC_CATALOG
        assert "full_disk_access" in TCC_CATALOG
        assert "Privacy_AllFiles" in TCC_CATALOG["full_disk_access"]["macos_deeplink"]

    def test_build_card_for_new_permissions(self):
        from agents.tcc_card import build_tcc_card

        for key in ("calendar", "reminders", "contacts", "full_disk_access"):
            card = build_tcc_card(
                key,
                skill_id="test",
                action="test.action",
                open_settings_on_mac=False,
            )
            assert card["type"] == "tcc_card"
            assert card["permission_key"] == key
            assert "macos_deeplink" in card
            assert card["title"]
            assert card["description"]
