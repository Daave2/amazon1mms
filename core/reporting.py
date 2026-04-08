from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Iterable, Mapping

from core.config import Settings, load_settings
from core.utils import atomic_write_json, ensure_directory

OUTPUT_DIR = "output"
RUN_SUMMARY_FILE = f"{OUTPUT_DIR}/run_summary.json"
FAILURE_EVENTS_FILE = f"{OUTPUT_DIR}/failure_events.json"

FAILURE_CATEGORY_LABELS = {
    "api_fast_path": "API Fast Path",
    "ui_fallback": "UI Fallback",
    "submission": "Submission",
    "cleanup": "Cleanup",
    "worker": "Worker",
    "general": "General",
}

DISCOVERY_REFRESH_REASON_LABELS = {
    "cached_snapshot_fresh": "the cached snapshot is newer than 7 days",
    "manual_override": "a manual refresh was requested",
    "missing_cached_snapshot": "no cached live-dropdown snapshot exists yet",
    "missing_cache_timestamp": "the cached snapshot has no refresh timestamp",
    "weekly_refresh_due": "the cached snapshot is at least 7 days old",
    "refresh_failed_used_cached_snapshot": "the scheduled refresh failed and the cached snapshot was reused",
}


def _pluralize(word: str, count: int) -> str:
    return word if count == 1 else f"{word}s"


def extract_failure_source(message: str) -> str:
    if " (" in message and message.endswith(")"):
        return message[: message.rfind(" (")].strip()
    return message.strip()


def _extract_failure_reason(message: str) -> str:
    if "(" in message and ")" in message:
        return message[message.rfind("(") + 1 : message.rfind(")")]
    return message


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


def build_failure_digest(
    failure_events: Iterable[Mapping[str, object]],
    category_limit: int = 5,
    reason_limit: int = 3,
) -> list[dict[str, object]]:
    digest_by_category: dict[str, dict[str, object]] = {}

    for event in failure_events:
        category = str(event.get("category", "general"))
        message = str(event.get("message", "Unknown issue"))
        terminal = bool(event.get("terminal", False))
        reason = _extract_failure_reason(message)
        source = extract_failure_source(message)

        digest_entry = digest_by_category.setdefault(
            category,
            {
                "events": 0,
                "terminal": 0,
                "sources": set(),
                "reason_counts": Counter(),
            },
        )
        digest_entry["events"] += 1
        if terminal:
            digest_entry["terminal"] += 1
        digest_entry["sources"].add(source)
        digest_entry["reason_counts"][reason] += 1

    digests: list[dict[str, object]] = []
    for category, digest_entry in digest_by_category.items():
        reason_counts = digest_entry["reason_counts"]
        top_reasons = [reason for reason, _count in reason_counts.most_common(reason_limit)]
        digests.append(
            {
                "category": category,
                "label": FAILURE_CATEGORY_LABELS.get(category, category.title()),
                "events": digest_entry["events"],
                "terminal": digest_entry["terminal"],
                "affected_sources": len(digest_entry["sources"]),
                "top_reason": top_reasons[0] if top_reasons else "",
                "top_reasons": top_reasons,
            }
        )

    digests.sort(key=lambda item: (-int(item["events"]), -int(item["terminal"]), str(item["label"])))
    return digests[:category_limit]


def _limit_named_list(values: Iterable[str], limit: int = 5) -> list[str]:
    items = list(values)
    if len(items) <= limit:
        return items
    return [*items[:limit], f"...and {len(items) - limit} more"]


def build_dropdown_change_lines(summary: Mapping[str, object], detail_limit: int = 5) -> list[str]:
    discovery = summary.get("discovery", {})
    changes = discovery.get("changes", {})
    refresh_mode = str(discovery.get("refresh_mode", "")).strip()
    refresh_reason = str(discovery.get("refresh_reason", "")).strip()
    if (
        not refresh_mode
        and int(discovery.get("live_dropdown_stores", 0)) == 0
        and int(discovery.get("matched_configured", 0)) == 0
        and int(discovery.get("live_only", 0)) == 0
        and int(changes.get("previous_count", 0)) == 0
        and int(changes.get("current_count", 0)) == 0
        and int(changes.get("new_count", 0)) == 0
        and int(changes.get("missing_count", 0)) == 0
    ):
        return []

    reason_label = DISCOVERY_REFRESH_REASON_LABELS.get(refresh_reason, refresh_reason.replace("_", " "))

    if refresh_mode == "cached":
        lines = [
            f"Live dropdown refresh was skipped for this run; using the cached snapshot because {reason_label}.",
            (
                f"Cached snapshot queued {discovery.get('live_dropdown_stores', 0)} stores, "
                f"with {discovery.get('matched_configured', 0)} configured matches and "
                f"{discovery.get('live_only', 0)} live-only {_pluralize('store', int(discovery.get('live_only', 0)))}."
            ),
            "Dropdown changes were not rechecked in this run.",
        ]
    else:
        lines = [
            (
                f"Live dropdown queued {discovery.get('live_dropdown_stores', 0)} stores, "
                f"with {discovery.get('matched_configured', 0)} configured matches and "
                f"{discovery.get('live_only', 0)} live-only {_pluralize('store', int(discovery.get('live_only', 0)))}."
            )
        ]

        new_count = int(changes.get("new_count", 0))
        missing_count = int(changes.get("missing_count", 0))
        if new_count or missing_count:
            lines.append(f"Dropdown changed since last run: {new_count} new and {missing_count} missing.")
            if changes.get("new_stores"):
                lines.append("New stores: " + ", ".join(_limit_named_list(changes["new_stores"], detail_limit)))
            if changes.get("missing_stores"):
                lines.append("Missing stores: " + ", ".join(_limit_named_list(changes["missing_stores"], detail_limit)))
        else:
            lines.append("Dropdown was unchanged since the previous run.")

    live_only_store_names = discovery.get("live_only_store_names", [])
    if live_only_store_names:
        lines.append("Live-only stores: " + ", ".join(_limit_named_list(live_only_store_names, detail_limit)))

    return lines


def build_failure_digest_lines(summary: Mapping[str, object], digest_limit: int = 5) -> list[str]:
    failure_digest = summary.get("failure_digest", [])
    if not failure_digest:
        return ["No issues were recorded during this run."]

    lines = []
    for digest_entry in failure_digest[:digest_limit]:
        line = (
            f"{digest_entry['label']}: {digest_entry['events']} event(s), "
            f"{digest_entry['terminal']} terminal, "
            f"{digest_entry['affected_sources']} affected source(s)"
        )
        top_reason = str(digest_entry.get("top_reason", "")).strip()
        if top_reason:
            line += f"; top reason: {top_reason}"
        lines.append(line)
    return lines


def build_github_step_summary_markdown(summary: Mapping[str, object], gate_reason: str = "", gate_hour: str = "") -> str:
    lines = ["## Scraper Run Summary", ""]

    if gate_reason:
        lines.extend(["### Gate", "", f"- Gate reason: `{gate_reason}`"])
        if gate_hour and gate_hour != "manual":
            gate_label = "Current London time" if ":" in gate_hour else "Current London hour"
            lines.append(f"- {gate_label}: `{gate_hour}`")
        lines.append("")

    stores = summary.get("stores", {})
    retries = summary.get("retries", {})
    collection = summary.get("collection_metrics", {})

    lines.extend(
        [
            "### Overview",
            "",
            f"- Status: `{summary.get('status', 'unknown')}`",
            f"- Detail: {summary.get('status_detail', 'n/a')}",
            f"- Trigger: `{summary.get('trigger', 'unknown')}`",
            f"- Duration: `{summary.get('elapsed_seconds', 0)}s`",
            f"- Successful submissions: `{stores.get('successful_submissions', 0)}` / `{stores.get('total', 0)}`",
            f"- Terminal failures: `{stores.get('failed', 0)}`",
            f"- Retries: `{retries.get('total', 0)}` across `{retries.get('stores', 0)}` store(s)",
            f"- Auth state: `{summary.get('auth', {}).get('state', 'unknown')}`",
            "",
            "### Dropdown",
            "",
        ]
    )

    dropdown_lines = build_dropdown_change_lines(summary)
    if dropdown_lines:
        for dropdown_line in dropdown_lines:
            lines.append(f"- {dropdown_line}")
    else:
        lines.append("- Live dropdown data was not captured for this run.")
    lines.append("")

    fastest = collection.get("fastest_store")
    slowest = collection.get("slowest_store")
    if fastest or slowest:
        lines.extend(["### Timing", ""])
        if fastest:
            lines.append(f"- Fastest store: `{fastest['store']}` in `{fastest['seconds']}s`")
        if slowest:
            lines.append(f"- Slowest store: `{slowest['store']}` in `{slowest['seconds']}s`")
        lines.append("")

    lines.extend(["### Failure Digest", ""])
    for digest_line in build_failure_digest_lines(summary):
        lines.append(f"- {digest_line}")
    lines.append("")

    recent_failures = summary.get("failure_summary", {}).get("recent_failures", [])
    if recent_failures:
        lines.extend(["### Recent Failures", ""])
        for failure in recent_failures:
            lines.append(f"- {failure}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _summarize_timing_entries(entries: list[tuple[str, float]]) -> dict[str, object]:
    if not entries:
        return {"count": 0, "average_seconds": 0.0, "p95_seconds": 0.0}

    durations = sorted(duration for _store_name, duration in entries)
    average_seconds = sum(durations) / len(durations)
    p95_seconds = durations[int(len(durations) * 0.95)]
    return {
        "count": len(entries),
        "average_seconds": round(average_seconds, 3),
        "p95_seconds": round(p95_seconds, 3),
    }


def _build_store_timing_summary(store_timing: tuple[str, float] | None) -> dict[str, object] | None:
    if not store_timing:
        return None

    store_name, duration = store_timing
    return {"store": store_name, "seconds": round(duration, 3)}


def build_run_summary(state, settings: Settings | None = None) -> dict[str, object]:
    settings = settings or getattr(state, "settings", None) or load_settings()
    finished_at = state.run_finished_at or datetime.now(state.run_started_at.tzinfo)
    elapsed_seconds = max((finished_at - state.run_started_at).total_seconds(), 0.0)
    failure_events = state.failure_event_payload()
    failure_summary = summarize_failure_events(failure_events)
    failure_digest = build_failure_digest(failure_events)

    collection_times = state.metrics.collection_times
    path_collection_times = state.metrics.path_collection_times
    submission_times = state.metrics.submission_times

    avg_collection_seconds = sum(duration for _store_name, duration in collection_times) / len(collection_times) if collection_times else 0.0
    avg_submission_seconds = sum(duration for _store_name, duration in submission_times) / len(submission_times) if submission_times else 0.0

    sorted_collection_times = sorted(duration for _store_name, duration in collection_times)
    p95_collection_seconds = sorted_collection_times[int(len(sorted_collection_times) * 0.95)] if sorted_collection_times else 0.0
    fastest_store = min(collection_times, key=lambda item: item[1]) if collection_times else None
    slowest_store = max(collection_times, key=lambda item: item[1]) if collection_times else None

    return {
        "run_id": state.run_id,
        "status": state.job_status,
        "status_detail": state.job_status_detail,
        "fatal_error_message": state.fatal_error_message,
        "trigger": state.job_trigger,
        "started_at": state.run_started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "configured_concurrency": {
            "browser_workers": state.browser_worker_pool_size or settings.initial_concurrency,
            "form_submitters": state.form_submitter_count or settings.num_form_submitters,
        },
        "routing": {
            "fast_path_eligible_at_start": state.fast_path_eligible_at_start,
            "ui_routed_at_start": state.ui_routed_at_start,
            "requeued_from_fast_path": state.requeued_from_fast_path,
        },
        "stores": {
            "total": state.progress.total,
            "successful_submissions": state.progress.current,
            "failed": len(state.run_failures),
        },
        "issues": {
            "total_events": len(failure_events),
            "terminal_failures": len(state.run_failures),
            "non_terminal_events": max(len(failure_events) - len(state.run_failures), 0),
        },
        "retries": {
            "total": state.metrics.retries,
            "stores": len(state.metrics.retry_stores),
        },
        "auth": {"state": state.auth_state_status},
        "discovery_cache": {
            "template_available_at_start": state.cache_template_available_at_start,
            "merchant_id_count_at_start": state.cache_merchant_ids_at_start,
        },
        "discovery": {
            "live_dropdown_stores": state.live_dropdown_store_count,
            "matched_configured": state.live_dropdown_matched_configured_count,
            "live_only": state.live_dropdown_live_only_count,
            "live_only_store_names": list(state.live_dropdown_live_only_store_names),
            "skipped_configured": state.live_dropdown_skipped_configured_count,
            "discovery_attempt": state.live_dropdown_discovery_attempt,
            "refresh_mode": state.live_dropdown_refresh_mode,
            "refresh_reason": state.live_dropdown_refresh_reason,
            "changes": {
                "previous_count": len(state.previous_live_dropdown_store_names),
                "current_count": len(state.current_live_dropdown_store_names),
                "new_count": len(state.live_dropdown_new_stores),
                "missing_count": len(state.live_dropdown_missing_stores),
                "new_stores": list(state.live_dropdown_new_stores),
                "missing_stores": list(state.live_dropdown_missing_stores),
            },
        },
        "collection_metrics": {
            "average_seconds": round(avg_collection_seconds, 3),
            "p95_seconds": round(p95_collection_seconds, 3),
            "fastest_store": _build_store_timing_summary(fastest_store),
            "slowest_store": _build_store_timing_summary(slowest_store),
        },
        "path_metrics": {
            "fast_path": _summarize_timing_entries(path_collection_times.get("fast_path", [])),
            "ui": _summarize_timing_entries(path_collection_times.get("ui", [])),
        },
        "submission_metrics": {
            "average_seconds": round(avg_submission_seconds, 3),
            "queued": state.submissions.queued,
            "sent": state.submissions.sent,
            "replayed": state.submissions.replayed,
            "retryable_failures": state.submissions.retryable_failures,
            "terminal_failures": state.submissions.terminal_failures,
        },
        "business_totals": {
            "orders": state.metrics.total_orders,
            "units": state.metrics.total_units,
        },
        "failure_summary": failure_summary,
        "failure_digest": failure_digest,
    }


def write_runtime_reports(state, settings: Settings | None = None) -> dict[str, object]:
    explicit_settings = settings is not None
    settings = settings or getattr(state, "settings", None) or load_settings()
    output_dir = settings.output_dir if explicit_settings else OUTPUT_DIR
    run_summary_file = settings.run_summary_file if explicit_settings else RUN_SUMMARY_FILE
    failure_events_file = settings.failure_events_file if explicit_settings else FAILURE_EVENTS_FILE
    ensure_directory(output_dir)

    run_summary = build_run_summary(state, settings)
    failure_events_payload = {
        "generated_at": (state.run_finished_at or datetime.now(state.run_started_at.tzinfo)).isoformat(),
        "count": len(state.failure_events),
        "events": state.failure_event_payload(),
    }

    atomic_write_json(run_summary_file, run_summary, indent=2)
    atomic_write_json(failure_events_file, failure_events_payload, indent=2)

    return run_summary
