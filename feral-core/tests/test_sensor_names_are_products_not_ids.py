"""No model-facing prose may name a sensor by its internal id.

`jw_health_glasses` is the BLE vendor's SDK id and `W300` is the part
number of the board inside the product. Both are load-bearing: samples
are keyed on the first and BLE matching needs the second. Neither is
what the product is called, and a model handed either one renders it by
title-casing. Asked what streams into it, the agent answered "JW Health
Glasses" -- hours before that answer was due to be recorded.

The structured fields carry both forms. These tests pin the prose, which
is the part the model paraphrases back to the user.
"""
import pytest

from api.device_view import group_subdevice_rows, peripheral_display_name
from integrations.health_canonical import build_reading, source_display_name

FORBIDDEN = ("jw_health_glasses", "jw health", "w300", "veepoo")


def _clean(text: str) -> bool:
    low = str(text).lower()
    return not any(tok in low for tok in FORBIDDEN)


def test_display_map_covers_the_ids_this_install_actually_stores():
    assert source_display_name("jw_health_glasses") == "Theora glasses"
    assert source_display_name("w300") == "Theora glasses"
    assert source_display_name("veepoo_wristband") == "VITRO wristband"


def test_unknown_ids_pass_through_rather_than_being_invented():
    assert source_display_name("some_new_band") == "some_new_band"
    assert source_display_name("") == ""


def test_reading_carries_the_name_beside_the_id():
    r = build_reading("hr", 64, source="jw_health_glasses", ts=1.0)
    assert r["source"] == "jw_health_glasses", "the id must survive for matching"
    assert r["source_name"] == "Theora glasses"


@pytest.mark.parametrize("reported", ["W300", "", "w300"])
def test_peripheral_name_never_shows_the_part_number(reported):
    name = peripheral_display_name("jw_health_glasses", reported)
    assert name == "Theora glasses"
    assert _clean(name)


def test_two_distinct_units_stay_distinguishable():
    """Mapping must not collapse two real devices into one name."""
    rows = [
        {"node_id": "a", "capability": "jw_health_glasses", "last_seen": 2.0,
         "first_seen": 1.0, "attrs": {"device_name": "W300"}},
        {"node_id": "b", "capability": "jw_health_glasses", "last_seen": 3.0,
         "first_seen": 1.0, "attrs": {"device_name": "W610"}},
    ]
    merged = group_subdevice_rows(rows, now=10.0)
    assert len(merged) == 2
    assert len({m["name"] for m in merged}) == 2


def test_glasses_trend_prose_names_the_product():
    from integrations.health_platforms import HealthAggregator

    agg = HealthAggregator.__new__(HealthAggregator)

    class _Store:
        def get_trend(self, metric, days=7):
            if metric == "hr":
                return {"sample_count": 3, "min": 60, "max": 100, "avg": 80,
                        "sources": ["jw_health_glasses"],
                        "daily": [{"date": "2026-09-11", "min": 60, "avg": 80,
                                   "max": 100, "count": 3}]}
            return {"sample_count": 0, "min": None, "max": None, "avg": None,
                    "sources": [], "daily": []}

    agg._biometric_history = lambda: _Store()
    trend = agg._build_glasses_vitals_trend(days=7)

    assert trend["source_ids"] == ["jw_health_glasses"], "id kept for matching"
    assert trend["sources"] == ["Theora glasses"]
    assert trend["source_names"] == ["Theora glasses"]
    assert trend["primary_source_name"] == "Theora glasses"
    # The note is the sentence the model repeats.
    assert "Theora glasses" in trend["note"]
    assert _clean(trend["note"])


def test_every_prose_field_of_the_trend_is_clean():
    from integrations.health_platforms import HealthAggregator

    agg = HealthAggregator.__new__(HealthAggregator)

    class _Store:
        def get_trend(self, metric, days=7):
            return {"sample_count": 1, "min": 60, "max": 60, "avg": 60,
                    "sources": ["jw_health_glasses"],
                    "daily": [{"date": "2026-09-11", "min": 60, "avg": 60,
                               "max": 60, "count": 1}]}

    agg._biometric_history = lambda: _Store()
    trend = agg._build_glasses_vitals_trend(days=7)

    dirty = [
        k for k, v in trend.items()
        if not k.endswith(("_id", "_ids"))
        and isinstance(v, str) and not _clean(v)
    ]
    assert not dirty, f"internal ids leaked into prose fields: {dirty}"


@pytest.mark.asyncio
async def test_model_facing_endpoints_ship_no_internal_ids_at_any_depth():
    """The contract that actually matters.

    Four earlier rounds added a product name beside each id and the agent
    kept saying the id: given both, the model quotes the one that looks
    like ground truth. So the model-facing endpoints ship names only.
    """
    import json

    from integrations.health_platforms import HealthAggregator

    agg = HealthAggregator.__new__(HealthAggregator)

    class _Store:
        def get_trend(self, metric, days=7):
            return {"sample_count": 2, "min": 60, "max": 90, "avg": 75,
                    "sources": ["jw_health_glasses"],
                    "daily": [{"date": "2026-09-11", "min": 60, "avg": 75,
                               "max": 90, "count": 2}]}

        def sample_metrics(self, days=7):
            return ["hr"]

        def get_samples(self, metric, since=0.0):
            return [{"value": 64, "ts": 1.0, "source": "jw_health_glasses"}]

    agg._biometric_history = lambda: _Store()

    trend = await agg.get_vitals_trend(days=7)
    history = await agg.get_health_history(days=7)

    for name, payload in (("vitals_trend", trend), ("health_history", history)):
        blob = json.dumps(payload, default=str).lower()
        leaked = [tok for tok in FORBIDDEN if tok in blob]
        assert not leaked, f"{name} shipped internal ids to the model: {leaked}"
        assert "theora glasses" in blob, f"{name} lost the product name"

    # Internal callers still get the ids they match on.
    with_ids = await agg.get_vitals_trend(days=7, include_ids=True)
    assert with_ids["source_ids"] == ["jw_health_glasses"]
