from datetime import datetime
from zoneinfo import ZoneInfo

from core.state import ScraperState
from services import chat_service

LONDON = ZoneInfo("Europe/London")


def test_build_batch_chat_payload_includes_attention_summary():
    state = ScraperState()
    state.chat_batch_count = 2

    payload = chat_service.build_batch_chat_payload(
        [
            {"store": "Morrisons Welling", "uph": "52", "lates": "0.0 %", "inf": "0.0 %"},
            {"store": "Belle Vale Morrisons", "uph": "88", "lates": "4.5 %", "inf": "2.1 %"},
        ],
        state,
    )

    summary_card = payload["cardsV2"][0]["card"]
    table_card = payload["cardsV2"][1]["card"]

    assert len(payload["cardsV2"]) == 2
    assert summary_card["header"]["title"] == "1MMS KPI Batch"
    assert table_card["header"]["title"] == "1MMS KPI Table"
    assert [section["header"] for section in summary_card["sections"]] == [
        "Batch Overview",
        "Batch Summary",
        "Metric Outliers",
    ]
    assert "2" == summary_card["sections"][0]["widgets"][0]["decoratedText"]["text"]
    assert "2" == summary_card["sections"][0]["widgets"][4]["decoratedText"]["text"]
    summary_text = summary_card["sections"][1]["widgets"][0]["textParagraph"]["text"]
    outlier_text = summary_card["sections"][2]["widgets"][0]["textParagraph"]["text"]
    table_items = table_card["sections"][0]["widgets"][0]["grid"]["items"]
    assert "UPH averaged 70.0 batch-wide, well below target versus the 80 target" in summary_text
    assert "Lates averaged 2.2% batch-wide" in summary_text
    assert "INF averaged 1.1% batch-wide" in summary_text
    assert "Lowest UPH: Welling (52.0), Belle Vale (88.0)" in outlier_text
    assert "Highest Lates: Belle Vale (4.5%), Welling (0.0%)" in outlier_text
    assert "Highest INF: Belle Vale (2.1%), Welling (0.0%)" in outlier_text
    assert any(item["title"] == "Belle Vale" for item in table_items)
    assert any(item["title"] == "Welling" for item in table_items)
    assert len(table_items) == 12
    assert table_items[:4] == [
        {"title": "Store", "textAlignment": "START"},
        {"title": "UPH", "textAlignment": "CENTER"},
        {"title": "Lates", "textAlignment": "CENTER"},
        {"title": "INF", "textAlignment": "CENTER"},
    ]


def test_build_batch_chat_payload_limits_outliers_to_worst_three():
    state = ScraperState()
    state.chat_batch_count = 3

    payload = chat_service.build_batch_chat_payload(
        [
                {
                    "store": f"Morrisons Store {index:02d}",
                    "uph": str(50 + index),
                    "lates": f"{index / 10:.1f} %",
                    "inf": f"{index / 10:.1f} %",
                }
                for index in range(1, 8)
            ],
            state,
    )

    summary_card = payload["cardsV2"][0]["card"]
    table_card = payload["cardsV2"][1]["card"]
    outlier_text = summary_card["sections"][2]["widgets"][0]["textParagraph"]["text"]
    table_items = table_card["sections"][0]["widgets"][0]["grid"]["items"]

    assert "Lowest UPH: Store 01 (51.0), Store 02 (52.0), Store 03 (53.0)" in outlier_text
    assert "Highest Lates: Store 07 (0.7%), Store 06 (0.6%), Store 05 (0.5%)" in outlier_text
    assert "Highest INF: Store 07 (0.7%), Store 06 (0.6%), Store 05 (0.5%)" in outlier_text
    assert "Store 04" not in outlier_text
    assert any(item["title"] == "Store 07" for item in table_items)
    assert len(table_items) == 32


def test_build_batch_chat_payload_can_focus_on_named_store_against_network():
    state = ScraperState()
    state.chat_batch_count = 1
    state.chat_focus_store = "Thornton Cleveleys"

    payload = chat_service.build_batch_chat_payload(
        [
            {
                "store": "Morrisons Thornton Cleveleys",
                "orders": "120",
                "units": "3000",
                "uph": "82",
                "lates": "1.4 %",
                "inf": "2.0 %",
            },
            {
                "store": "Morrisons York",
                "orders": "180",
                "units": "4200",
                "uph": "78",
                "lates": "3.2 %",
                "inf": "2.4 %",
            },
        ],
        state,
    )

    summary_card = payload["cardsV2"][0]["card"]
    table_card = payload["cardsV2"][1]["card"]
    assert summary_card["header"]["subtitle"].endswith("Thornton Cleveleys vs network")
    assert [section["header"] for section in summary_card["sections"]] == [
        "Focus Store",
        "Network Comparison",
        "Store vs Network",
        "Network Outliers",
    ]
    comparison_text = summary_card["sections"][2]["widgets"][0]["textParagraph"]["text"]
    table_items = table_card["sections"][0]["widgets"][0]["grid"]["items"]
    assert "Thornton Cleveleys processed 120 orders and 3000 units" in comparison_text
    assert "UPH rank: 1/2" in comparison_text
    assert "Lates rank: 1/2" in comparison_text
    assert table_items[4]["title"] == "Thornton Cleveleys (Focus)"


def test_build_job_summary_payload_includes_run_context_and_issues():
    state = ScraperState()
    state.run_started_at = datetime(2026, 4, 2, 12, 0, tzinfo=LONDON)
    state.run_finished_at = datetime(2026, 4, 2, 12, 1, tzinfo=LONDON)
    state.set_job_status("completed_with_failures", "2 terminal failure(s)")
    state.auth_state_status = "refreshed"
    state.browser_worker_pool_size = 25
    state.form_submitter_count = 5
    state.live_dropdown_store_count = 85
    state.live_dropdown_matched_configured_count = 84
    state.live_dropdown_live_only_count = 1
    state.live_dropdown_live_only_store_names = ["Live Only Store"]
    state.live_dropdown_skipped_configured_count = 18
    state.live_dropdown_discovery_attempt = "settled-load"
    state.previous_live_dropdown_store_names = ["Welling"]
    state.current_live_dropdown_store_names = ["Belle Vale", "Live Only Store"]
    state.live_dropdown_new_stores = ["Belle Vale"]
    state.live_dropdown_missing_stores = ["Welling"]
    state.progress["total"] = 85
    state.progress["current"] = 83
    state.run_failures = [
        "Morrisons Chippenham (HTTP Submit Fail 500)",
        "Morrisons Cardiff Tygals (Worker Exception)",
    ]
    state.failure_events = [
        {
            "message": "Morrisons Chippenham (HTTP Submit Fail 500)",
            "category": "submission",
            "terminal": True,
            "timestamp": "2026-04-02T12:00:20+01:00",
        },
        {
            "message": "Morrisons Cardiff Tygals (Worker Exception)",
            "category": "worker",
            "terminal": True,
            "timestamp": "2026-04-02T12:00:25+01:00",
        },
        {
            "message": "Worker-1 page (Cleanup failure)",
            "category": "cleanup",
            "terminal": False,
            "timestamp": "2026-04-02T12:00:30+01:00",
        },
    ]
    state.metrics["collection_times"] = [
        ("Belle Vale Morrisons", 8.4),
        ("Morrisons Welling", 11.2),
    ]
    state.metrics["submission_times"] = [("Belle Vale Morrisons", 0.4)]
    state.metrics["retries"] = 3
    state.metrics["retry_stores"].update({"Morrisons Chippenham", "Morrisons Cardiff Tygals"})
    state.metrics["total_orders"] = 1520
    state.metrics["total_units"] = 6401

    payload = chat_service.build_job_summary_payload(state, duration=40.55)

    card = payload["cardsV2"][0]["card"]
    section_headers = [section["header"] for section in card["sections"]]

    assert card["header"]["title"] == "⚠️ Run Completed With Failures (1MMS)"
    assert section_headers == [
        "Run Overview",
        "Run Context",
        "Volume & Performance",
        "Collection Extremes",
        "Dropdown Changes",
        "Failure Digest",
        "Recent Events",
    ]
    assert (
        card["sections"][1]["widgets"][1]["decoratedText"]["text"]
        == "Logged in again"
    )
    assert "85 queued • 84 configured • 1 live-only • 18 skipped • via settled-load" == (
        card["sections"][1]["widgets"][2]["decoratedText"]["text"]
    )
    assert "Dropdown changed since last run: 1 new and 1 missing." in (
        card["sections"][4]["widgets"][0]["textParagraph"]["text"]
    )
    assert "Submission: 1 event(s), 1 terminal, 1 affected source(s); top reason: HTTP Submit Fail 500" in (
        card["sections"][5]["widgets"][1]["textParagraph"]["text"]
    )
    assert "Cleanup: 1 event(s), 0 terminal, 1 affected source(s); top reason: Cleanup failure" in (
        card["sections"][5]["widgets"][1]["textParagraph"]["text"]
    )
    assert "Morrisons Chippenham (HTTP Submit Fail 500)" in (
        card["sections"][6]["widgets"][0]["textParagraph"]["text"]
    )


def test_build_job_summary_payload_includes_focus_store_section_when_present():
    state = ScraperState()
    state.run_started_at = datetime(2026, 4, 2, 12, 0, tzinfo=LONDON)
    state.run_finished_at = datetime(2026, 4, 2, 12, 1, tzinfo=LONDON)
    state.set_job_status("completed", "Run completed successfully")
    state.progress["total"] = 2
    state.progress["current"] = 2
    state.focus_store_summary = {
        "focusStoreFound": True,
        "matchedStore": "Morrisons Thornton Cleveleys",
        "focusDisplay": {"uph": "82", "lates": "1.4 %", "inf": "2.0 %"},
        "networkDisplay": {"uph": "80", "lates": "2.3 %", "inf": "2.6 %"},
    }

    payload = chat_service.build_job_summary_payload(state, duration=60.0)

    card = payload["cardsV2"][0]["card"]
    section_headers = [section["header"] for section in card["sections"]]
    assert "Focus Store" in section_headers
    focus_section = next(section for section in card["sections"] if section["header"] == "Focus Store")
    assert focus_section["widgets"][0]["decoratedText"]["text"] == "Thornton Cleveleys"
    assert focus_section["widgets"][1]["decoratedText"]["text"] == "82 / 80"
    assert focus_section["widgets"][2]["decoratedText"]["text"] == "1.4 % / 2.3 %"


def test_build_job_summary_payload_shows_fatal_error_section():
    state = ScraperState()
    state.run_started_at = datetime(2026, 4, 2, 12, 0, tzinfo=LONDON)
    state.run_finished_at = datetime(2026, 4, 2, 12, 0, 12, tzinfo=LONDON)
    state.set_job_status("fatal", "Unhandled exception in main execution block")
    state.fatal_error_message = "Store selector dropdown did not open"

    payload = chat_service.build_job_summary_payload(state)

    card = payload["cardsV2"][0]["card"]
    assert card["header"]["title"] == "🚨 Run Failed (1MMS)"
    assert "Fatal Error" in [section["header"] for section in card["sections"]]
