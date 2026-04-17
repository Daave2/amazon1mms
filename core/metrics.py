from datetime import datetime
from math import trunc
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from core.config import FORM_STORE_NAME_MAPPINGS, STORE_PREFIX_RE
from core.schemas import AmazonShopperRecord

LOCAL_TIMEZONE = ZoneInfo("Europe/London")
PROFILE_PRIORITY = {"COMBINED": 0, "REGULAR": 1, "RESCUE": 2, "MANAGER": 3}


def _first_not_none(*values: float | None) -> float:
    for value in values:
        if value is not None:
            return value
    return 0.0


def _format_truncated_percent(value: float) -> str:
    truncated = trunc((value or 0.0) * 10) / 10
    return f"{truncated:.1f} %"


def _deduplicate_master_records(records: list[AmazonShopperRecord]) -> list[AmazonShopperRecord]:
    all_masters = [record for record in records if record.type == "MASTER"]
    candidate_records = all_masters or records

    by_shopper: dict[str, AmazonShopperRecord] = {}
    for record in candidate_records:
        shopper_name = record.shopperName or record.externalId or "unknown"
        profile = record.shopperProfile or "NONE"
        existing_profile = by_shopper[shopper_name].shopperProfile if shopper_name in by_shopper else "NONE"
        if shopper_name not in by_shopper or PROFILE_PRIORITY.get(profile, 99) < PROFILE_PRIORITY.get(
            existing_profile, 99
        ):
            by_shopper[shopper_name] = record

    return list(by_shopper.values())


def normalize_metrics_payload(api_data: list[dict[str, Any]] | dict[str, Any]) -> dict[str, float]:
    if isinstance(api_data, list):
        records = [AmazonShopperRecord.model_validate(metric_record) for metric_record in api_data]
        masters = _deduplicate_master_records(records)

        total_orders = sum(record.metrics.OrdersShopped_V2 for record in masters)
        total_units = sum(record.metrics.RequestedQuantity_V2 for record in masters)
        total_fulfilled = sum(record.metrics.PickedUnits_V2 for record in masters)
        total_pick_time_sec = sum(record.metrics.PickTimeInSec_V2 for record in masters)
        total_time_ms = sum(record.metrics.TimeAvailable_V2 for record in masters)

        uph = (total_fulfilled / (total_pick_time_sec / 3600)) if total_pick_time_sec > 0 else 0.0

        total_late_picks_count = sum(
            record.metrics.OrdersShopped_V2 * (record.metrics.LatePicksRate / 100) for record in masters
        )
        late_picks_rate = (total_late_picks_count / total_orders * 100) if total_orders > 0 else 0.0

        total_inf_count = sum(
            record.metrics.RequestedQuantity_V2 * (record.metrics.ItemNotFoundRate_V2 / 100) for record in masters
        )
        inf_rate = (total_inf_count / total_units * 100) if total_units > 0 else 0.0

        return {
            "OrdersShopped_V2": total_orders,
            "RequestedQuantity_V2": total_units,
            "PickedUnits_V2": total_fulfilled,
            "AverageUPH_V2": uph,
            "LatePicksRate": late_picks_rate,
            "ItemNotFoundRate_V2": inf_rate,
            "ItemFoundRate_V2": 100.0 - inf_rate,
            "OrderCancellations": sum(record.metrics.OrderCancellations for record in masters),
            "TimeAvailable_V2": total_time_ms,
        }

    record = AmazonShopperRecord.model_validate(api_data)
    return {
        "OrdersShopped_V2": _first_not_none(record.OrdersShopped_V2, record.metrics.OrdersShopped_V2),
        "RequestedQuantity_V2": _first_not_none(record.RequestedQuantity_V2, record.metrics.RequestedQuantity_V2),
        "PickedUnits_V2": _first_not_none(record.PickedUnits_V2, record.metrics.PickedUnits_V2),
        "AverageUPH_V2": _first_not_none(record.AverageUPH_V2, record.metrics.AverageUPH_V2),
        "LatePicksRate": _first_not_none(record.LatePicksRate, record.metrics.LatePicksRate),
        "ItemNotFoundRate_V2": _first_not_none(
            record.ItemNotFoundRate_V2,
            record.metrics.ItemNotFoundRate_V2,
        ),
        "ItemFoundRate_V2": _first_not_none(record.ItemFoundRate_V2, record.metrics.ItemFoundRate_V2),
        "OrderCancellations": _first_not_none(record.OrderCancellations, record.metrics.OrderCancellations),
        "TimeAvailable_V2": _first_not_none(record.TimeAvailable_V2, record.metrics.TimeAvailable_V2),
    }


def format_time_available(milliseconds_from_api: float) -> str:
    total_seconds = int(float(milliseconds_from_api or 0.0) / 1000)
    total_minutes, _ = divmod(abs(total_seconds), 60)
    total_hours, remaining_minutes = divmod(total_minutes, 60)
    return f"{total_hours}:{remaining_minutes:02d}"


def normalize_form_store_name(
    store_name: str,
    form_store_name_mappings: Mapping[str, str] | None = None,
) -> str:
    cleaned_name = store_name.strip()
    mappings = form_store_name_mappings or FORM_STORE_NAME_MAPPINGS
    mapped_name = mappings.get(cleaned_name)
    if mapped_name:
        return mapped_name

    if "morrison" not in cleaned_name.lower():
        return cleaned_name

    old_store_name = STORE_PREFIX_RE.sub("", cleaned_name).strip()
    return f"Morrisons - {old_store_name}" if old_store_name else cleaned_name


def build_form_data(
    store_name: str,
    normalized_metrics: dict[str, float],
    current_dt: datetime | None = None,
    local_timezone: ZoneInfo = LOCAL_TIMEZONE,
    form_store_name_mappings: Mapping[str, str] | None = None,
) -> dict[str, str]:
    current_date = (current_dt or datetime.now(local_timezone)).strftime("%Y-%m-%d")
    lates_val = normalized_metrics.get("LatePicksRate", 0.0)

    return {
        "date": current_date,
        "store": normalize_form_store_name(store_name, form_store_name_mappings),
        "orders": str(int(normalized_metrics.get("OrdersShopped_V2") or 0)),
        "units": str(int(normalized_metrics.get("RequestedQuantity_V2") or 0)),
        "fulfilled": str(int(normalized_metrics.get("PickedUnits_V2") or 0)),
        "uph": f"{(normalized_metrics.get('AverageUPH_V2') or 0.0):.0f}",
        "inf": f"{(normalized_metrics.get('ItemNotFoundRate_V2') or 0.0):.1f} %",
        "found": f"{(normalized_metrics.get('ItemFoundRate_V2') or 0.0):.1f} %",
        "cancelled": str(int(normalized_metrics.get("OrderCancellations") or 0)),
        "lates": _format_truncated_percent(lates_val),
        "time_available": format_time_available(normalized_metrics.get("TimeAvailable_V2", 0.0)),
    }
