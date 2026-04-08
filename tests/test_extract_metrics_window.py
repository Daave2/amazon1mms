from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.manual.extract_metrics_window import _build_focus_store_summary


LONDON = ZoneInfo("Europe/London")


def test_build_focus_store_summary_highlights_focus_store_against_network():
    results = [
        {
            "store": "Morrisons Thornton Cleveleys",
            "dropdownName": "Thornton Cleveleys",
            "normalizedCombined": {
                "OrdersShopped_V2": 120,
                "RequestedQuantity_V2": 3000,
                "PickedUnits_V2": 2940,
                "AverageUPH_V2": 82.0,
                "LatePicksRate": 1.4,
                "ItemNotFoundRate_V2": 2.0,
                "ItemFoundRate_V2": 98.0,
                "OrderCancellations": 2,
                "TimeAvailable_V2": 14_400_000,
            },
            "displayMetrics": {
                "orders": "120",
                "units": "3000",
                "fulfilled": "2940",
                "uph": "82",
                "inf": "2.0 %",
                "found": "98.0 %",
                "cancelled": "2",
                "lates": "1.4 %",
                "time_available": "4:00",
            },
        },
        {
            "store": "Morrisons York",
            "dropdownName": "York",
            "normalizedCombined": {
                "OrdersShopped_V2": 180,
                "RequestedQuantity_V2": 4200,
                "PickedUnits_V2": 4100,
                "AverageUPH_V2": 78.0,
                "LatePicksRate": 3.2,
                "ItemNotFoundRate_V2": 2.4,
                "ItemFoundRate_V2": 97.6,
                "OrderCancellations": 4,
                "TimeAvailable_V2": 18_000_000,
            },
            "displayMetrics": {
                "orders": "180",
                "units": "4200",
                "fulfilled": "4100",
                "uph": "78",
                "inf": "2.4 %",
                "found": "97.6 %",
                "cancelled": "4",
                "lates": "3.2 %",
                "time_available": "5:00",
            },
        },
        {
            "store": "Morrisons Aberdeen",
            "dropdownName": "Aberdeen",
            "normalizedCombined": {
                "OrdersShopped_V2": 90,
                "RequestedQuantity_V2": 2100,
                "PickedUnits_V2": 2058,
                "AverageUPH_V2": 76.0,
                "LatePicksRate": 2.5,
                "ItemNotFoundRate_V2": 2.0,
                "ItemFoundRate_V2": 98.0,
                "OrderCancellations": 1,
                "TimeAvailable_V2": 10_800_000,
            },
            "displayMetrics": {
                "orders": "90",
                "units": "2100",
                "fulfilled": "2058",
                "uph": "76",
                "inf": "2.0 %",
                "found": "98.0 %",
                "cancelled": "1",
                "lates": "2.5 %",
                "time_available": "3:00",
            },
        },
    ]

    summary = _build_focus_store_summary(
        results,
        "Thornton Cleveleys",
        "today",
        datetime(2026, 4, 8, 0, 0, tzinfo=LONDON),
        datetime(2026, 4, 8, 14, 0, tzinfo=LONDON),
        LONDON,
    )

    assert summary["focusStoreFound"] is True
    assert summary["matchedStore"] == "Morrisons Thornton Cleveleys"
    assert summary["storeCount"] == 3
    assert summary["networkDisplay"]["orders"] == "390"
    assert summary["networkDisplay"]["units"] == "9300"
    assert summary["networkDisplay"]["lates"] == "2.4 %"
    assert round(summary["shares"]["ordersPct"], 3) == round(120 / 390 * 100, 3)
    assert summary["rankings"]["uph"]["position"] == 1
    assert summary["rankings"]["lates"]["guidance"] == "Lower is better"
