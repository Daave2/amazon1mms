from datetime import datetime
from zoneinfo import ZoneInfo

from core.forms import build_form_payload, build_submission_log_entry


def test_build_form_payload_uses_declared_field_map():
    payload = build_form_payload(
        {"store": "Belle Vale Morrisons", "orders": "12", "ignored": "value"},
        {"store": "entry.store", "orders": "entry.orders"},
    )

    assert payload == {"entry.store": "Belle Vale Morrisons", "entry.orders": "12"}


def test_build_submission_log_entry_adds_timestamp():
    entry = build_submission_log_entry(
        {"store": "Belle Vale Morrisons", "orders": "12"},
        current_dt=datetime(2026, 4, 2, 9, 45, tzinfo=ZoneInfo("Europe/London")),
    )

    assert entry == {
        "timestamp": "2026-04-02 09:45:00",
        "store": "Belle Vale Morrisons",
        "orders": "12",
    }
