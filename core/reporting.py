import json
import os
from collections import Counter
from datetime import datetime
from typing import Iterable, Mapping

from core.config import INITIAL_CONCURRENCY, NUM_FORM_SUBMITTERS, OUTPUT_DIR

FAILURE_CATEGORY_LABELS = {
    "api_fast_path": "API Fast Path",
    "ui_fallback": "UI Fallback",
    "submission": "Submission",
    "cleanup": "Cleanup",
    "worker": "Worker",
    "general": "General",
}

RUN_SUMMARY_FILE = os.path.join(OUTPUT_DIR, "run_summary.json")
FAILURE_EVENTS_FILE = os.path.join(OUTPUT_DIR, "failure_events.json")


def summarize_failure_events(
    failure_events: Iterable[Mapping[str, object]],
    recent_limit: int = 5,
) -> dict[str, object]:
    category_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    recent_failures: list[str] = []

    for event in failure_events:
        message = str(event.get("message", "Unknown issue"))
        category = str(event.get("category", "general"))
        category_counts[category] += 1
        reason_counts[_extract_failure_reason(message)] += 1
        recent_failures.append(message)

    return {
        "category_counts": dict(category_counts),
        "reason_counts": dict(reason_counts),
        "recent_failures": recent_failures[-recent_limit:],
        "overflow_count": max(len(recent_failures) - recent_limit, 0),
    }


def _extract_failure_reason(message: str) -> str:
    if "(" in message and ")" in message:
        return message[message.rfind("(") + 1 : message.rfind(")")]
    return message


def build_run_summary(state) -> dict[str, object]:
    finished_at = state.run_finished_at or datetime.now(state.run_started_at.tzinfo)
    started_at = state.run_started_at
    elapsed_seconds = max((finished_at - started_at).total_seconds(), 0.0)
    failure_summary = summarize_failure_events(state.failure_events)

    collection_times = state.metrics["collection_times"]
    submission_times = state.metrics["submission_times"]

    avg_collection_seconds = (
        sum(duration for _store_name, duration in collection_times) / len(collection_times)
        if collection_times
        else 0.0
    )
    avg_submission_seconds = (
        sum(duration for _store_name, duration in submission_times) / len(submission_times)
        if submission_times
        else 0.0
    )

    sorted_collection_times = sorted(duration for _store_name, duration in collection_times)
    p95_collection_seconds = (
        sorted_collection_times[int(len(sorted_collection_times) * 0.95)]
        if sorted_collection_times
        else 0.0
    )

    fastest_store = (
        min(collection_times, key=lambda item: item[1])
        if collection_times
        else None
    )
    slowest_store = (
        max(collection_times, key=lambda item: item[1])
        if collection_times
        else None
    )

    return {
        "status": state.job_status,
        "status_detail": state.job_status_detail,
        "fatal_error_message": state.fatal_error_message,
        "trigger": state.job_trigger,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "configured_concurrency": {
            "browser_workers": state.browser_worker_pool_size or INITIAL_CONCURRENCY,
            "form_submitters": state.form_submitter_count or NUM_FORM_SUBMITTERS,
        },
        "stores": {
            "total": state.progress["total"],
            "successful_submissions": state.progress["current"],
            "failed": len(state.run_failures),
        },
        "issues": {
            "total_events": len(state.failure_events),
            "terminal_failures": len(state.run_failures),
            "non_terminal_events": max(len(state.failure_events) - len(state.run_failures), 0),
        },
        "retries": {
            "total": state.metrics["retries"],
            "stores": len(state.metrics["retry_stores"]),
        },
        "auth": {
            "state": state.auth_state_status,
        },
        "discovery_cache": {
            "template_available_at_start": state.cache_template_available_at_start,
            "merchant_id_count_at_start": state.cache_merchant_ids_at_start,
        },
        "collection_metrics": {
            "average_seconds": round(avg_collection_seconds, 3),
            "p95_seconds": round(p95_collection_seconds, 3),
            "fastest_store": _build_store_timing_summary(fastest_store),
            "slowest_store": _build_store_timing_summary(slowest_store),
        },
        "submission_metrics": {
            "average_seconds": round(avg_submission_seconds, 3),
        },
        "business_totals": {
            "orders": state.metrics["total_orders"],
            "units": state.metrics["total_units"],
        },
        "failure_summary": failure_summary,
    }


def write_runtime_reports(state) -> dict[str, object]:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    run_summary = build_run_summary(state)
    failure_events_payload = {
        "generated_at": (state.run_finished_at or datetime.now(state.run_started_at.tzinfo)).isoformat(),
        "count": len(state.failure_events),
        "events": list(state.failure_events),
    }

    with open(RUN_SUMMARY_FILE, "w", encoding="utf-8") as file_handle:
        json.dump(run_summary, file_handle, indent=2)

    with open(FAILURE_EVENTS_FILE, "w", encoding="utf-8") as file_handle:
        json.dump(failure_events_payload, file_handle, indent=2)

    return run_summary


def _build_store_timing_summary(store_timing: tuple[str, float] | None) -> dict[str, object] | None:
    if not store_timing:
        return None

    store_name, duration = store_timing
    return {
        "store": store_name,
        "seconds": round(duration, 3),
    }
