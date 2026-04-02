from datetime import datetime
from zoneinfo import ZoneInfo

from core.metrics import build_form_data, normalize_metrics_payload


def test_normalize_metrics_payload_for_single_summary_object():
    payload = {
        "metrics": {
            "OrdersShopped_V2": 12,
            "RequestedQuantity_V2": 120,
            "PickedUnits_V2": 110,
            "AverageUPH_V2": 88.6,
            "LatePicksRate": 2.4,
            "ItemNotFoundRate_V2": 3.1,
            "ItemFoundRate_V2": 96.9,
            "OrderCancellations": 4,
            "TimeAvailable_V2": 5_400_000,
        }
    }

    normalized = normalize_metrics_payload(payload)
    form_data = build_form_data(
        "Belle Vale Morrisons",
        normalized,
        current_dt=datetime(2026, 4, 2, 9, 30, tzinfo=ZoneInfo("Europe/London")),
    )

    assert normalized["OrdersShopped_V2"] == 12
    assert normalized["AverageUPH_V2"] == 88.6
    assert form_data["date"] == "2026-04-02"
    assert form_data["uph"] == "89"
    assert form_data["lates"] == "2.4 %"
    assert form_data["time_available"] == "1:30"


def test_normalize_metrics_payload_for_detailed_records_deduplicates_profiles():
    payload = [
        {
            "type": "MASTER",
            "shopperName": "Alex",
            "shopperProfile": "REGULAR",
            "metrics": {
                "OrdersShopped_V2": 10,
                "RequestedQuantity_V2": 100,
                "PickedUnits_V2": 90,
                "PickTimeInSec_V2": 3600,
                "LatePicksRate": 10.0,
                "ItemNotFoundRate_V2": 5.0,
                "OrderCancellations": 1,
                "TimeAvailable_V2": 3_600_000,
            },
        },
        {
            "type": "MASTER",
            "shopperName": "Alex",
            "shopperProfile": "COMBINED",
            "metrics": {
                "OrdersShopped_V2": 20,
                "RequestedQuantity_V2": 200,
                "PickedUnits_V2": 180,
                "PickTimeInSec_V2": 7200,
                "LatePicksRate": 5.0,
                "ItemNotFoundRate_V2": 2.0,
                "OrderCancellations": 2,
                "TimeAvailable_V2": 7_200_000,
            },
        },
        {
            "type": "MASTER",
            "shopperName": "Blair",
            "shopperProfile": "REGULAR",
            "metrics": {
                "OrdersShopped_V2": 5,
                "RequestedQuantity_V2": 50,
                "PickedUnits_V2": 45,
                "PickTimeInSec_V2": 1800,
                "LatePicksRate": 0.0,
                "ItemNotFoundRate_V2": 0.0,
                "OrderCancellations": 0,
                "TimeAvailable_V2": 1_800_000,
            },
        },
    ]

    normalized = normalize_metrics_payload(payload)

    assert normalized == {
        "OrdersShopped_V2": 25,
        "RequestedQuantity_V2": 250,
        "PickedUnits_V2": 225,
        "AverageUPH_V2": 90.0,
        "LatePicksRate": 4.0,
        "ItemNotFoundRate_V2": 1.6,
        "ItemFoundRate_V2": 98.4,
        "OrderCancellations": 2,
        "TimeAvailable_V2": 9_000_000,
    }
