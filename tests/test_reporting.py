import json
from datetime import datetime
from zoneinfo import ZoneInfo

from core import reporting
from core.state import ScraperState

LONDON = ZoneInfo("Europe/London")


def test_write_runtime_reports_for_all_success_run(tmp_path, monkeypatch):
    state = _build_state("completed", "Run completed successfully")
    state.auth_state_status = "reused"
    state.cache_template_available_at_start = True
    state.cache_merchant_ids_at_start = 4
    state.browser_worker_pool_size = 5
    state.form_submitter_count = 2
    state.progress["total"] = 2
    state.progress["current"] = 2
    state.metrics["collection_times"] = [("Belle Vale Morrisons", 1.4), ("Carterton Morrisons", 2.2)]
    state.metrics["submission_times"] = [("Belle Vale Morrisons", 0.4), ("Carterton Morrisons", 0.5)]
    state.metrics["retries"] = 1
    state.metrics["retry_stores"].add("Belle Vale Morrisons")
    state.metrics["total_orders"] = 50
    state.metrics["total_units"] = 200

    _patch_reporting_paths(monkeypatch, tmp_path)
    summary = reporting.write_runtime_reports(state)

    assert summary["status"] == "completed"
    assert summary["stores"]["successful_submissions"] == 2
    assert summary["discovery_cache"]["template_available_at_start"] is True
    assert summary["collection_metrics"]["fastest_store"] == {
        "store": "Belle Vale Morrisons",
        "seconds": 1.4,
    }

    with open(tmp_path / "run_summary.json", encoding="utf-8") as file_handle:
        persisted_summary = json.load(file_handle)
    with open(tmp_path / "failure_events.json", encoding="utf-8") as file_handle:
        persisted_events = json.load(file_handle)

    assert persisted_summary["stores"]["failed"] == 0
    assert persisted_events["count"] == 0


def test_build_run_summary_for_partial_success_with_mixed_failures():
    state = _build_state("completed_with_failures", "1 terminal failure(s)")
    state.progress["total"] = 3
    state.progress["current"] = 2
    state.run_failures = ["Belle Vale Morrisons (HTTP Submit Fail 500)"]
    state.failure_events = [
        {
            "message": "Belle Vale Morrisons (HTTP Submit Fail 500)",
            "category": "submission",
            "terminal": True,
            "timestamp": "2026-04-02T09:05:00+01:00",
        },
        {
            "message": "Worker-1 page (Cleanup failure)",
            "category": "cleanup",
            "terminal": False,
            "timestamp": "2026-04-02T09:06:00+01:00",
        },
    ]

    summary = reporting.build_run_summary(state)

    assert summary["status"] == "completed_with_failures"
    assert summary["stores"]["failed"] == 1
    assert summary["failure_summary"]["category_counts"] == {
        "submission": 1,
        "cleanup": 1,
    }
    assert summary["failure_summary"]["reason_counts"]["HTTP Submit Fail 500"] == 1


def test_build_run_summary_for_login_abort():
    state = _build_state("login_aborted", "Session priming failed after 3 attempts")
    state.auth_state_status = "refresh_failed"

    summary = reporting.build_run_summary(state)

    assert summary["status"] == "login_aborted"
    assert summary["auth"]["state"] == "refresh_failed"
    assert summary["stores"]["total"] == 0


def test_build_run_summary_for_fatal_exception():
    state = _build_state("fatal", "Unhandled exception in main execution block")
    state.fatal_error_message = "Browser launch failed"

    summary = reporting.build_run_summary(state)

    assert summary["status"] == "fatal"
    assert summary["fatal_error_message"] == "Browser launch failed"


def _build_state(status: str, detail: str) -> ScraperState:
    state = ScraperState()
    state.run_started_at = datetime(2026, 4, 2, 9, 0, tzinfo=LONDON)
    state.run_finished_at = datetime(2026, 4, 2, 9, 10, tzinfo=LONDON)
    state.job_trigger = "workflow_dispatch"
    state.set_job_status(status, detail)
    return state


def _patch_reporting_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(reporting, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(reporting, "RUN_SUMMARY_FILE", str(tmp_path / "run_summary.json"))
    monkeypatch.setattr(reporting, "FAILURE_EVENTS_FILE", str(tmp_path / "failure_events.json"))
