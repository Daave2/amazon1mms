from __future__ import annotations

from datetime import datetime
from typing import Mapping
from zoneinfo import ZoneInfo

LOCAL_TIMEZONE = ZoneInfo("Europe/London")

LOG_FIELDNAMES = [
    "timestamp",
    "run_id",
    "submission_id",
    "date",
    "store",
    "orders",
    "units",
    "fulfilled",
    "uph",
    "inf",
    "found",
    "cancelled",
    "lates",
    "field_11",
    "time_available",
]


def build_form_payload(form_data: Mapping[str, str], field_map: Mapping[str, str]) -> dict[str, str]:
    return {field_map[key]: value for key, value in form_data.items() if key in field_map}


def build_submission_log_entry(
    data: Mapping[str, str],
    current_dt: datetime | None = None,
) -> dict[str, str]:
    timestamp = (current_dt or datetime.now(LOCAL_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    return {"timestamp": timestamp, **data}
