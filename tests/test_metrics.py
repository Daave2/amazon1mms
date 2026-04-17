from datetime import datetime
from zoneinfo import ZoneInfo

from core.config import load_settings
from core.metrics import build_form_data, normalize_form_store_name, normalize_metrics_payload


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
    assert form_data["store"] == "Morrisons - Belle Vale"
    assert form_data["uph"] == "89"
    assert form_data["lates"] == "2.4 %"
    assert form_data["time_available"] == "1:30"


def test_normalize_form_store_name_uses_old_form_store_labels():
    assert normalize_form_store_name("Morrisons Auckland") == "Morrisons - Bishop Auckland"
    assert normalize_form_store_name("Morrisons Analby") == "Morrisons - Hull"
    assert normalize_form_store_name("Bradford") == "Morrisons - Thornbury"
    assert normalize_form_store_name("Morrisons Cardiff Tygals") == "Morrisons - Cardiff"
    assert normalize_form_store_name("Catcliffe Morrisons") == "Morrisons - Sheffield"
    assert normalize_form_store_name("Morrisons Harrow - Trident Point") == "Morrisons - Harrow"
    assert normalize_form_store_name("Carterton Morrisons") == "Morrisons - Oxford"
    assert normalize_form_store_name("Morrisons Stevenson") == "Morrisons - Stevenston"
    assert normalize_form_store_name("Morrisons Thornton Cleveleys") == "Morrisons - Thornton-Cleveleys"
    assert normalize_form_store_name("Morrisons Welwyn") == "Morrisons - Welwyn Garden City"


def test_normalize_form_store_name_standardizes_old_form_style():
    assert normalize_form_store_name("Acton") == "Morrisons - Acton"
    assert normalize_form_store_name("Belle Vale Morrisons") == "Morrisons - Belle Vale"
    assert normalize_form_store_name("Morrisons York") == "Morrisons - York"
    assert normalize_form_store_name("St Helens Morrisons") == "Morrisons - St. Helens"
    assert normalize_form_store_name("Morrisons Bedford") == "Morrisons - Bedford"
    assert normalize_form_store_name("Network") == "Network"


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


def test_build_store_submission_overlays_lates_and_cancellations_from_detail_payload():
    from services.metrics_service import build_store_submission

    settings = load_settings()
    summary_payload = {
        "OrdersShopped_V2": 268,
        "RequestedQuantity_V2": 6828,
        "PickedUnits_V2": 6675,
        "AverageUPH_V2": 92,
        "ItemNotFoundRate_V2": 3.6,
        "ItemFoundRate_V2": 96.4,
        "TimeAvailable_V2": 3_600_000,
    }
    detail_payload = [
        {
            "type": "MASTER",
            "shopperName": "Janine",
            "metrics": {
                "OrdersShopped_V2": 16,
                "RequestedQuantity_V2": 428,
                "PickedUnits_V2": 421,
                "PickTimeInSec_V2": 17_410.566,
                "LatePicksRate": 12.5,
                "ItemNotFoundRate_V2": 3.27,
                "OrderCancellations": 0,
                "TimeAvailable_V2": 19_584_956,
            },
        },
        {
            "type": "MASTER",
            "shopperName": "Name Not Found",
            "metrics": {
                "OrdersShopped_V2": 0,
                "RequestedQuantity_V2": 0,
                "PickedUnits_V2": 0,
                "PickTimeInSec_V2": 0,
                "LatePicksRate": 0,
                "ItemNotFoundRate_V2": 0,
                "OrderCancellations": 2,
                "TimeAvailable_V2": 0,
            },
        },
        {
            "type": "MASTER",
            "shopperName": "Other",
            "metrics": {
                "OrdersShopped_V2": 252,
                "RequestedQuantity_V2": 6400,
                "PickedUnits_V2": 6254,
                "PickTimeInSec_V2": 249_810.196,
                "LatePicksRate": 0,
                "ItemNotFoundRate_V2": 3.62,
                "OrderCancellations": 0,
                "TimeAvailable_V2": 288_766_514,
            },
        },
    ]

    form_data, normalized = build_store_submission(
        "Morrisons Welwyn",
        summary_payload,
        settings,
        detail_api_data=detail_payload,
    )

    assert normalized["OrdersShopped_V2"] == 268
    assert normalized["RequestedQuantity_V2"] == 6828
    assert normalized["PickedUnits_V2"] == 6675
    assert normalized["LatePicksRate"] == 0.7462686567164178
    assert normalized["OrderCancellations"] == 2
    assert form_data["store"] == "Morrisons - Welwyn Garden City"
    assert form_data["lates"] == "0.7 %"
    assert form_data["cancelled"] == "2"


def test_build_form_data_truncates_lates_to_match_dashboard():
    form_data = build_form_data(
        "Morrisons York",
        {
            "OrdersShopped_V2": 139,
            "RequestedQuantity_V2": 3655,
            "PickedUnits_V2": 3617,
            "AverageUPH_V2": 81,
            "LatePicksRate": 2.8834,
            "ItemNotFoundRate_V2": 2.3,
            "ItemFoundRate_V2": 97.7,
            "OrderCancellations": 6,
            "TimeAvailable_V2": 0,
        },
        current_dt=datetime(2026, 4, 7, 12, 0, tzinfo=ZoneInfo("Europe/London")),
    )

    assert form_data["lates"] == "2.8 %"
