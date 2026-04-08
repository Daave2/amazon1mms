import asyncio
import re
import ssl
from datetime import datetime
from html import escape

import aiohttp
import certifi

from core.config import Settings, load_settings
from core.logger import app_logger
from core.reporting import (
    build_dropdown_change_lines,
    build_failure_digest_lines,
    build_run_summary,
)
from core.state import ScraperState
from core.utils import format_metric_with_emoji, sanitize_store_name

CARD_IMAGE_URL = "https://i.imgur.com/u0e3d2x.png"

AUTH_STATE_LABELS = {
    "reused": "Reused existing session",
    "refreshed": "Logged in again",
    "refresh_required": "Session expired",
    "refresh_failed": "Login refresh failed",
    "missing": "No saved session",
    "unknown": "Unknown",
}

JOB_STATUS_META = {
    "completed": ("✅", "Run Completed"),
    "completed_with_failures": ("⚠️", "Run Completed With Failures"),
    "login_aborted": ("⛔", "Run Aborted During Login"),
    "aborted_no_stores": ("⏹️", "Run Aborted With No Stores"),
    "fatal": ("🚨", "Run Failed"),
    "running": ("⏳", "Run In Progress"),
}


def _extract_numeric_value(raw_value: object) -> float | None:
    if raw_value is None:
        return None

    match = re.search(r"-?\d+(?:\.\d+)?", str(raw_value))
    if not match:
        return None
    return float(match.group(0))


def _format_average(entries: list[dict[str, str]], field: str, suffix: str = "") -> str:
    numeric_values = [
        numeric_value
        for entry in entries
        if (numeric_value := _extract_numeric_value(entry.get(field))) is not None
    ]
    if not numeric_values:
        return "N/A"

    average_value = sum(numeric_values) / len(numeric_values)
    return f"{average_value:.1f}{suffix}"


def _humanize_token(value: str) -> str:
    return value.replace("_", " ").strip().title()


def _format_html_lines(lines: list[str]) -> str:
    return "<br>".join(escape(line) for line in lines)


def _pluralize(word: str, count: int) -> str:
    return word if count == 1 else f"{word}s"


def _collect_metric_rows(entries: list[dict[str, str]], field: str) -> list[dict[str, object]]:
    metric_rows: list[dict[str, object]] = []
    for entry in entries:
        numeric_value = _extract_numeric_value(entry.get(field))
        if numeric_value is None:
            continue

        metric_rows.append(
            {
                "store": sanitize_store_name(entry.get("store", "Unknown")),
                "value": numeric_value,
            }
        )
    return metric_rows


def _normalize_store_label(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _find_focus_entry(entries: list[dict[str, str]], focus_store: str) -> dict[str, str] | None:
    requested = _normalize_store_label(focus_store)
    exact_match = None
    partial_match = None

    for entry in entries:
        normalized_store = _normalize_store_label(entry.get("store", ""))
        if normalized_store == requested:
            exact_match = entry
            break
        if requested and requested in normalized_store:
            partial_match = entry

    return exact_match or partial_match


def _format_delta_text(metric_name: str, focus_value: float, network_value: float, higher_is_better: bool) -> str:
    delta = focus_value - network_value
    if abs(delta) < 0.05:
        return f"{metric_name} is in line with network at {focus_value:.1f}."

    if higher_is_better:
        direction = "above" if delta > 0 else "below"
    else:
        direction = "better than" if delta < 0 else "worse than"

    return (
        f"{metric_name} is {abs(delta):.1f} {'pp' if metric_name != 'UPH' else ''} "
        f"{direction} network ({focus_value:.1f} vs {network_value:.1f})."
    ).replace("  ", " ").replace(" .", ".")


def _build_rank_line(
    entries: list[dict[str, str]],
    focus_entry: dict[str, str],
    field: str,
    higher_is_better: bool,
    label: str,
) -> str:
    metric_rows = _collect_metric_rows(entries, field)
    focus_store = sanitize_store_name(focus_entry.get("store", "Unknown"))
    if higher_is_better:
        ordered = sorted(metric_rows, key=lambda row: (-float(row["value"]), str(row["store"])))
    else:
        ordered = sorted(metric_rows, key=lambda row: (float(row["value"]), str(row["store"])))

    for index, row in enumerate(ordered, start=1):
        if row["store"] == focus_store:
            return f"{label} rank: {index}/{len(ordered)}"
    return f"{label} rank: N/A"


def _build_focus_batch_payload(
    entries: list[dict[str, str]],
    state: ScraperState,
    settings: Settings,
    focus_store: str,
) -> dict[str, object]:
    batch_header_text = datetime.now(settings.local_timezone).strftime("%A %d %B, %H:%M")
    sorted_entries = sorted(entries, key=lambda entry: sanitize_store_name(entry.get("store", "")))
    focus_entry = _find_focus_entry(sorted_entries, focus_store)
    if not focus_entry:
        return {}

    focus_store_name = sanitize_store_name(focus_entry.get("store", focus_store))
    uph_rows = _collect_metric_rows(sorted_entries, "uph")
    lates_rows = _collect_metric_rows(sorted_entries, "lates")
    inf_rows = _collect_metric_rows(sorted_entries, "inf")
    orders_rows = _collect_metric_rows(sorted_entries, "orders")
    units_rows = _collect_metric_rows(sorted_entries, "units")

    focus_uph = _extract_numeric_value(focus_entry.get("uph")) or 0.0
    focus_lates = _extract_numeric_value(focus_entry.get("lates")) or 0.0
    focus_inf = _extract_numeric_value(focus_entry.get("inf")) or 0.0
    focus_orders = _extract_numeric_value(focus_entry.get("orders")) or 0.0
    focus_units = _extract_numeric_value(focus_entry.get("units")) or 0.0

    network_uph = sum(float(row["value"]) for row in uph_rows) / len(uph_rows) if uph_rows else 0.0
    network_lates = sum(float(row["value"]) for row in lates_rows) / len(lates_rows) if lates_rows else 0.0
    network_inf = sum(float(row["value"]) for row in inf_rows) / len(inf_rows) if inf_rows else 0.0
    network_orders = sum(float(row["value"]) for row in orders_rows)
    network_units = sum(float(row["value"]) for row in units_rows)

    comparison_lines = [
        f"{focus_store_name} processed {int(focus_orders)} orders and {int(focus_units)} units, representing {(focus_orders / network_orders * 100 if network_orders else 0.0):.1f}% of network orders.",
        _format_delta_text("UPH", focus_uph, network_uph, higher_is_better=True),
        _format_delta_text("Lates", focus_lates, network_lates, higher_is_better=False),
        _format_delta_text("INF", focus_inf, network_inf, higher_is_better=False),
        _build_rank_line(sorted_entries, focus_entry, "uph", True, "UPH"),
        _build_rank_line(sorted_entries, focus_entry, "lates", False, "Lates"),
        _build_rank_line(sorted_entries, focus_entry, "inf", False, "INF"),
    ]
    outlier_lines = [
        _format_ranked_metric_line("Lowest UPH", uph_rows, descending=False),
        _format_ranked_metric_line("Highest Lates", lates_rows, descending=True, suffix="%", zero_is_all_clear=True),
        _format_ranked_metric_line("Highest INF", inf_rows, descending=True, suffix="%", zero_is_all_clear=True),
    ]

    sections = [
        {
            "header": "Focus Store",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Store",
                        "text": focus_store_name,
                        "startIcon": {"knownIcon": "STORE"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "UPH",
                        "text": focus_entry.get("uph", "N/A"),
                        "startIcon": {"knownIcon": "STAR"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Lates",
                        "text": focus_entry.get("lates", "N/A"),
                        "startIcon": {"knownIcon": "CLOCK"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "INF",
                        "text": focus_entry.get("inf", "N/A"),
                        "startIcon": {"knownIcon": "DESCRIPTION"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Orders",
                        "text": focus_entry.get("orders", "N/A"),
                        "startIcon": {"knownIcon": "SHOPPING_CART"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Units",
                        "text": focus_entry.get("units", "N/A"),
                        "startIcon": {"knownIcon": "TICKET"},
                    }
                },
            ],
        },
        {
            "header": "Network Comparison",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Stores In Batch",
                        "text": str(len(sorted_entries)),
                        "startIcon": {"knownIcon": "STORE"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Network Avg UPH",
                        "text": f"{network_uph:.1f}",
                        "startIcon": {"knownIcon": "STAR"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Network Avg Lates",
                        "text": f"{network_lates:.1f} %",
                        "startIcon": {"knownIcon": "CLOCK"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Network Avg INF",
                        "text": f"{network_inf:.1f} %",
                        "startIcon": {"knownIcon": "DESCRIPTION"},
                    }
                },
            ],
        },
        {
            "header": "Store vs Network",
            "widgets": [
                {
                    "textParagraph": {
                        "text": _format_html_lines(comparison_lines),
                    }
                }
            ],
        },
        {
            "header": "Network Outliers",
            "widgets": [
                {
                    "textParagraph": {
                        "text": _format_html_lines(outlier_lines),
                    }
                }
            ],
        },
    ]

    return {
        "cardsV2": [
            {
                "cardId": f"focus-summary-{state.chat_batch_count}",
                "card": {
                    "header": {
                        "title": "1MMS KPI Batch",
                        "subtitle": f"{batch_header_text} • Batch {state.chat_batch_count} • {focus_store_name} vs network",
                        "imageUrl": CARD_IMAGE_URL,
                        "imageType": "CIRCLE",
                    },
                    "sections": sections,
                },
            },
            _build_batch_table_card(
                sorted_entries,
                batch_header_text,
                state.chat_batch_count,
                settings,
                focus_store=focus_store_name,
            ),
        ]
    }


def _describe_target_position(value: float, threshold: float, higher_is_better: bool) -> str:
    if higher_is_better:
        delta = value - threshold
        if delta >= 0:
            if delta < 2:
                return "just above target"
            if delta < 8:
                return "above target"
            return "well above target"
        if abs(delta) < 2:
            return "just below target"
        if abs(delta) < 8:
            return "below target"
        return "well below target"

    delta = threshold - value
    if delta >= 0:
        if delta < 0.25:
            return "just inside target"
        if delta < 1.0:
            return "inside target"
        return "well inside target"
    if abs(delta) < 0.5:
        return "just above target"
    if abs(delta) < 2.0:
        return "above target"
    return "well above target"


def _format_ranked_metric_line(
    label: str,
    metric_rows: list[dict[str, object]],
    descending: bool,
    suffix: str = "",
    zero_is_all_clear: bool = False,
) -> str:
    if not metric_rows:
        return f"{label}: no data"

    sorted_rows = sorted(
        metric_rows,
        key=lambda row: (-float(row["value"]), str(row["store"])) if descending else (float(row["value"]), str(row["store"])),
    )

    if zero_is_all_clear and all(float(row["value"]) == 0.0 for row in sorted_rows):
        return f"{label}: all stores at 0.0{suffix}"

    worst_rows = sorted_rows[:3]
    formatted_rows = ", ".join(
        f"{row['store']} ({float(row['value']):.1f}{suffix})"
        for row in worst_rows
    )
    return f"{label}: {formatted_rows}"


def _build_batch_summary_lines(entries: list[dict[str, str]], settings: Settings) -> list[str]:
    uph_rows = _collect_metric_rows(entries, "uph")
    lates_rows = _collect_metric_rows(entries, "lates")
    inf_rows = _collect_metric_rows(entries, "inf")

    summary_lines: list[str] = []

    if uph_rows:
        avg_uph = sum(float(row["value"]) for row in uph_rows) / len(uph_rows)
        below_target = sum(float(row["value"]) < settings.uph_threshold for row in uph_rows)
        lowest_uph = min(uph_rows, key=lambda row: (float(row["value"]), str(row["store"])))
        if below_target:
            uph_tail = (
                f"{below_target} {_pluralize('store', below_target)} below target, "
                f"with {lowest_uph['store']} lowest at {float(lowest_uph['value']):.1f}"
            )
        else:
            uph_tail = (
                f"all stores at or above target, with {lowest_uph['store']} still lowest at "
                f"{float(lowest_uph['value']):.1f}"
            )
        summary_lines.append(
            f"UPH averaged {avg_uph:.1f} batch-wide, "
            f"{_describe_target_position(avg_uph, settings.uph_threshold, higher_is_better=True)} versus the {settings.uph_threshold:.0f} target, with {uph_tail}."
        )

    if lates_rows:
        avg_lates = sum(float(row["value"]) for row in lates_rows) / len(lates_rows)
        above_target = sum(float(row["value"]) > settings.lates_threshold for row in lates_rows)
        zero_lates = sum(float(row["value"]) == 0.0 for row in lates_rows)
        highest_lates = max(lates_rows, key=lambda row: (float(row["value"]), str(row["store"])))
        lates_mid = (
            f"{above_target} {_pluralize('store', above_target)} over target"
            if above_target
            else "no stores over target"
        )
        summary_lines.append(
            f"Lates averaged {avg_lates:.1f}% batch-wide, "
            f"{_describe_target_position(avg_lates, settings.lates_threshold, higher_is_better=False)} versus the {settings.lates_threshold:.1f}% target, "
            f"with {lates_mid} and {zero_lates} at 0.0%; highest was {highest_lates['store']} at {float(highest_lates['value']):.1f}%."
        )

    if inf_rows:
        avg_inf = sum(float(row["value"]) for row in inf_rows) / len(inf_rows)
        above_target = sum(float(row["value"]) > settings.inf_threshold for row in inf_rows)
        zero_inf = sum(float(row["value"]) == 0.0 for row in inf_rows)
        highest_inf = max(inf_rows, key=lambda row: (float(row["value"]), str(row["store"])))
        inf_mid = (
            f"{above_target} {_pluralize('store', above_target)} over target"
            if above_target
            else "no stores over target"
        )
        summary_lines.append(
            f"INF averaged {avg_inf:.1f}% batch-wide, "
            f"{_describe_target_position(avg_inf, settings.inf_threshold, higher_is_better=False)} versus the {settings.inf_threshold:.1f}% target, "
            f"with {inf_mid} and {zero_inf} at 0.0%; highest was {highest_inf['store']} at {float(highest_inf['value']):.1f}%."
        )

    return summary_lines


def _build_batch_table_card(
    entries: list[dict[str, str]],
    batch_header_text: str,
    batch_number: int,
    settings: Settings,
    focus_store: str = "",
) -> dict[str, object]:
    focus_entry = _find_focus_entry(entries, focus_store) if focus_store else None
    ordered_entries = list(entries)
    if focus_entry:
        ordered_entries = [focus_entry, *[entry for entry in entries if entry is not focus_entry]]

    grid_items = [
        {"title": "Store", "textAlignment": "START"},
        {"title": "UPH", "textAlignment": "CENTER"},
        {"title": "Lates", "textAlignment": "CENTER"},
        {"title": "INF", "textAlignment": "CENTER"},
    ]

    for entry in ordered_entries:
        uph_val = entry.get("uph", "N/A")
        lates_val = entry.get("lates", "0.0 %") or "0.0 %"
        inf_val = entry.get("inf", "0.0 %") or "0.0 %"
        store_title = sanitize_store_name(entry.get("store", "N/A"))
        if focus_entry is entry:
            store_title = f"{store_title} (Focus)"

        grid_items.extend(
            [
                {
                    "title": store_title,
                    "textAlignment": "START",
                },
                {
                    "title": format_metric_with_emoji(uph_val, settings.uph_threshold, is_uph=True),
                    "textAlignment": "CENTER",
                },
                {
                    "title": format_metric_with_emoji(lates_val, settings.lates_threshold),
                    "textAlignment": "CENTER",
                },
                {
                    "title": format_metric_with_emoji(inf_val, settings.inf_threshold),
                    "textAlignment": "CENTER",
                },
            ]
        )

    return {
        "cardId": f"batch-table-{batch_number}",
        "card": {
            "header": {
                "title": "1MMS KPI Table",
                "subtitle": f"{batch_header_text} • Batch {batch_number} • Full network view",
                "imageUrl": CARD_IMAGE_URL,
                "imageType": "CIRCLE",
            },
            "sections": [
                {
                    "header": "Store Metrics",
                    "widgets": [
                        {
                            "grid": {
                                "title": "Performance Snapshot",
                                "columnCount": 4,
                                "borderStyle": {"type": "STROKE", "cornerRadius": 4},
                                "items": grid_items,
                            }
                        }
                    ],
                }
            ],
        },
    }


def build_batch_chat_payload(
    entries: list[dict[str, str]],
    state: ScraperState,
    settings: Settings | None = None,
) -> dict[str, object]:
    settings = settings or getattr(state, "settings", None) or load_settings()
    focus_store = str(getattr(state, "chat_focus_store", "") or "").strip()
    if focus_store:
        focused_payload = _build_focus_batch_payload(entries, state, settings, focus_store)
        if focused_payload:
            return focused_payload

    batch_header_text = datetime.now(settings.local_timezone).strftime("%A %d %B, %H:%M")
    card_subtitle = f"{batch_header_text} • Batch {state.chat_batch_count} • {len(entries)} stores"

    sorted_entries = sorted(entries, key=lambda entry: sanitize_store_name(entry.get("store", "")))
    uph_rows = _collect_metric_rows(sorted_entries, "uph")
    lates_rows = _collect_metric_rows(sorted_entries, "lates")
    inf_rows = _collect_metric_rows(sorted_entries, "inf")
    stores_needing_attention = len(
        {
            row["store"]
            for row in uph_rows
            if float(row["value"]) < settings.uph_threshold
        }
        | {
            row["store"]
            for row in lates_rows
            if float(row["value"]) > settings.lates_threshold
        }
        | {
            row["store"]
            for row in inf_rows
            if float(row["value"]) > settings.inf_threshold
        }
    )
    summary_lines = _build_batch_summary_lines(sorted_entries, settings)
    outlier_lines = [
        _format_ranked_metric_line("Lowest UPH", uph_rows, descending=False),
        _format_ranked_metric_line("Highest Lates", lates_rows, descending=True, suffix="%", zero_is_all_clear=True),
        _format_ranked_metric_line("Highest INF", inf_rows, descending=True, suffix="%", zero_is_all_clear=True),
    ]

    sections = [
        {
            "header": "Batch Overview",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Stores In Batch",
                        "text": str(len(sorted_entries)),
                        "startIcon": {"knownIcon": "STORE"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Average UPH",
                        "text": _format_average(sorted_entries, "uph"),
                        "startIcon": {"knownIcon": "STAR"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Average Lates",
                        "text": _format_average(sorted_entries, "lates", " %"),
                        "startIcon": {"knownIcon": "CLOCK"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Average INF",
                        "text": _format_average(sorted_entries, "inf", " %"),
                        "startIcon": {"knownIcon": "DESCRIPTION"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Stores Needing Attention",
                        "text": str(stores_needing_attention),
                        "startIcon": {"knownIcon": "STORE"},
                    }
                },
            ],
        },
        {
            "header": "Batch Summary",
            "widgets": [
                {
                    "textParagraph": {
                        "text": _format_html_lines(summary_lines or ["No batch summary available."]),
                    }
                }
            ],
        },
        {
            "header": "Metric Outliers",
            "widgets": [
                {
                    "textParagraph": {
                        "text": _format_html_lines(outlier_lines),
                    }
                }
            ],
        },
    ]

    return {
        "cardsV2": [
            {
                "cardId": f"batch-summary-{state.chat_batch_count}",
                "card": {
                    "header": {
                        "title": "1MMS KPI Batch",
                        "subtitle": card_subtitle,
                        "imageUrl": CARD_IMAGE_URL,
                        "imageType": "CIRCLE",
                    },
                    "sections": sections,
                },
            },
            _build_batch_table_card(
                sorted_entries,
                batch_header_text,
                state.chat_batch_count,
                settings,
                focus_store=focus_store,
            ),
        ]
    }


def _build_discovery_text(state: ScraperState, total_stores: int) -> str:
    queue_count = state.live_dropdown_store_count or total_stores
    discovery_bits = [
        f"{queue_count} queued",
        f"{state.live_dropdown_matched_configured_count} configured",
        f"{state.live_dropdown_live_only_count} live-only",
    ]
    if state.live_dropdown_skipped_configured_count:
        discovery_bits.append(f"{state.live_dropdown_skipped_configured_count} skipped")
    if state.live_dropdown_discovery_attempt:
        discovery_bits.append(f"via {state.live_dropdown_discovery_attempt}")
    return " • ".join(discovery_bits)


def _section_from_lines(header: str, lines: list[str]) -> dict[str, object]:
    return {
        "header": header,
        "widgets": [
            {
                "textParagraph": {
                    "text": _format_html_lines(lines),
                }
            }
        ],
    }


def _build_timing_text(store_timing: dict[str, object] | None) -> str:
    if not store_timing:
        return "N/A"
    return f"{sanitize_store_name(str(store_timing['store']))} ({store_timing['seconds']:.2f}s)"


def build_job_summary_payload(
    state: ScraperState,
    settings: Settings | None = None,
    duration: float | None = None,
) -> dict[str, object]:
    settings = settings or getattr(state, "settings", None) or load_settings()
    summary = build_run_summary(state, settings)
    duration_seconds = duration if duration is not None else float(summary["elapsed_seconds"])
    duration_seconds = max(duration_seconds, 0.0)

    success = int(summary["stores"]["successful_submissions"])
    total = int(summary["stores"]["total"])
    failures = int(summary["stores"]["failed"])
    issue_total = int(summary["issues"]["total_events"])
    terminal_failures = int(summary["issues"]["terminal_failures"])
    non_terminal_events = int(summary["issues"]["non_terminal_events"])
    success_rate = (success / total) * 100 if total > 0 else 0.0
    throughput_spm = (success / (duration_seconds / 60)) if duration_seconds > 0 else 0.0

    status = str(summary["status"])
    status_icon, status_title = JOB_STATUS_META.get(status, ("ℹ️", _humanize_token(status)))
    trigger_text = _humanize_token(str(summary["trigger"]))
    auth_text = AUTH_STATE_LABELS.get(
        str(summary["auth"]["state"]),
        _humanize_token(str(summary["auth"]["state"])),
    )

    sections = [
        {
            "header": "Run Overview",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Result",
                        "text": summary["status_detail"] or status_title,
                        "startIcon": {"knownIcon": "STAR"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Stores",
                        "text": f"{success}/{total} successful • {failures} failed ({success_rate:.1f}%)",
                        "startIcon": {"knownIcon": "STORE"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Total Duration",
                        "text": f"{duration_seconds:.2f}s",
                        "startIcon": {"knownIcon": "CLOCK"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Throughput",
                        "text": f"{throughput_spm:.1f} stores/min",
                        "startIcon": {"knownIcon": "FLIGHT_DEPARTURE"},
                    }
                },
            ],
        },
        {
            "header": "Run Context",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Trigger",
                        "text": trigger_text,
                        "startIcon": {"knownIcon": "DESCRIPTION"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Auth State",
                        "text": auth_text,
                        "startIcon": {"knownIcon": "MEMBERSHIP"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Discovery Queue",
                        "text": _build_discovery_text(state, total),
                        "startIcon": {"knownIcon": "STORE"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Workers",
                        "text": (
                            f"{summary['configured_concurrency']['browser_workers']} browser • "
                            f"{summary['configured_concurrency']['form_submitters']} submitter"
                        ),
                        "startIcon": {"knownIcon": "TRAFFIC"},
                    }
                },
            ],
        },
        {
            "header": "Volume & Performance",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Orders",
                        "text": f"{summary['business_totals']['orders']:,}",
                        "startIcon": {"knownIcon": "SHOPPING_CART"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Units",
                        "text": f"{summary['business_totals']['units']:,}",
                        "startIcon": {"knownIcon": "TICKET"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Avg Collection",
                        "text": f"{summary['collection_metrics']['average_seconds']:.2f}s",
                        "startIcon": {"knownIcon": "DESCRIPTION"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "p95 Collection",
                        "text": f"{summary['collection_metrics']['p95_seconds']:.2f}s",
                        "startIcon": {"knownIcon": "DESCRIPTION"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Retries",
                        "text": (
                            f"{summary['retries']['total']} total • "
                            f"{summary['retries']['stores']} store(s)"
                        ),
                        "startIcon": {"knownIcon": "MEMBERSHIP"},
                    }
                },
            ],
        },
    ]

    focus_summary = getattr(state, "focus_store_summary", None)
    if isinstance(focus_summary, dict) and focus_summary.get("focusStoreFound"):
        focus_display = focus_summary.get("focusDisplay", {})
        network_display = focus_summary.get("networkDisplay", {})
        matched_store = sanitize_store_name(str(focus_summary.get("matchedStore", focus_summary.get("requestedFocusStore", "Focus Store"))))
        sections.append(
            {
                "header": "Focus Store",
                "widgets": [
                    {
                        "decoratedText": {
                            "topLabel": "Store",
                            "text": matched_store,
                            "startIcon": {"knownIcon": "STORE"},
                        }
                    },
                    {
                        "decoratedText": {
                            "topLabel": "Store UPH / Network",
                            "text": f"{focus_display.get('uph', 'N/A')} / {network_display.get('uph', 'N/A')}",
                            "startIcon": {"knownIcon": "STAR"},
                        }
                    },
                    {
                        "decoratedText": {
                            "topLabel": "Store Lates / Network",
                            "text": f"{focus_display.get('lates', 'N/A')} / {network_display.get('lates', 'N/A')}",
                            "startIcon": {"knownIcon": "CLOCK"},
                        }
                    },
                    {
                        "decoratedText": {
                            "topLabel": "Store INF / Network",
                            "text": f"{focus_display.get('inf', 'N/A')} / {network_display.get('inf', 'N/A')}",
                            "startIcon": {"knownIcon": "DESCRIPTION"},
                        }
                    },
                ],
            }
        )

    if summary["fatal_error_message"]:
        sections.append(
            {
                "header": "Fatal Error",
                "widgets": [
                    {
                        "textParagraph": {
                            "text": escape(str(summary["fatal_error_message"])),
                        }
                    }
                ],
            }
        )

    if summary["collection_metrics"]["fastest_store"] or summary["collection_metrics"]["slowest_store"]:
        sections.append(
            {
                "header": "Collection Extremes",
                "widgets": [
                    {
                        "decoratedText": {
                            "topLabel": "Fastest Store",
                            "text": _build_timing_text(summary["collection_metrics"]["fastest_store"]),
                            "startIcon": {"knownIcon": "BOLT"},
                        }
                    },
                    {
                        "decoratedText": {
                            "topLabel": "Slowest Store",
                            "text": _build_timing_text(summary["collection_metrics"]["slowest_store"]),
                            "startIcon": {"knownIcon": "CLOCK"},
                        }
                    },
                ],
            }
        )

    dropdown_change_lines = build_dropdown_change_lines(summary)
    if dropdown_change_lines:
        sections.append(_section_from_lines("Dropdown Changes", dropdown_change_lines))

    if issue_total:
        failure_digest_lines = build_failure_digest_lines(summary)
        recent_lines = list(summary["failure_summary"]["recent_failures"])
        if summary["failure_summary"]["overflow_count"]:
            recent_lines.append(f"...and {summary['failure_summary']['overflow_count']} more")

        sections.append(
            {
                "header": "Failure Digest",
                "widgets": [
                    {
                        "decoratedText": {
                            "topLabel": "Issue Totals",
                            "text": f"{issue_total} total • {terminal_failures} terminal • {non_terminal_events} non-terminal",
                            "startIcon": {"knownIcon": "DESCRIPTION"},
                        }
                    },
                    {
                        "textParagraph": {
                            "text": _format_html_lines(failure_digest_lines),
                        }
                    },
                ],
            }
        )

        if recent_lines:
            sections.append(_section_from_lines("Recent Events", recent_lines))

    return {
        "cardsV2": [
            {
                "cardId": f"job-summary-{int(datetime.now().timestamp())}",
                "card": {
                    "header": {
                        "title": f"{status_icon} {status_title} (1MMS)",
                        "subtitle": datetime.now(settings.local_timezone).strftime("%A %d %B, %H:%M"),
                        "imageUrl": CARD_IMAGE_URL,
                        "imageType": "CIRCLE",
                    },
                    "sections": sections,
                },
            }
        ]
    }


async def post_to_chat_webhook(
    entries: list[dict[str, str]],
    state: ScraperState,
    settings: Settings | None = None,
):
    settings = settings or getattr(state, "settings", None) or load_settings()
    if not settings.chat_webhook_url or not entries:
        return
    try:
        state.chat_batch_count += 1
        payload = build_batch_chat_payload(entries, state, settings)

        timeout = aiohttp.ClientTimeout(total=30)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.post(settings.chat_webhook_url, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    app_logger.error(f"Chat webhook post failed. Status: {resp.status}. Response: {error_text}")
                    await state.record_issue(
                        f"Chat batch post failed ({resp.status})",
                        0.0,
                        category="general",
                    )
    except Exception as e:
        app_logger.error(f"Error posting to chat webhook: {e}", exc_info=settings.debug_mode)
        await state.record_issue("Chat batch post exception", 0.0, category="general")


async def post_job_summary(
    state: ScraperState,
    settings: Settings | None = None,
    duration: float | None = None,
):
    settings = settings or getattr(state, "settings", None) or load_settings()
    if not settings.chat_webhook_url or state.job_summary_posted:
        return
    try:
        payload = build_job_summary_payload(state, settings, duration)

        timeout = aiohttp.ClientTimeout(total=30)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.post(settings.chat_webhook_url, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    app_logger.error(f"Job summary post failed: {resp.status}. Response: {error_text}")
                    await state.record_issue(
                        f"Job summary post failed ({resp.status})",
                        0.0,
                        category="general",
                    )
                    return

        state.job_summary_posted = True
    except Exception as e:
        app_logger.error(f"Error posting job summary: {e}", exc_info=settings.debug_mode)
        await state.record_issue("Job summary post exception", 0.0, category="general")


async def chat_dispatcher_worker(
    chat_queue: asyncio.Queue,
    state: ScraperState,
    settings: Settings | None = None,
):
    settings = settings or getattr(state, "settings", None) or load_settings()
    if not settings.chat_webhook_url:
        while True:
            item = await chat_queue.get()
            chat_queue.task_done()
            if item is None:
                return

    batch: list[dict[str, str]] = []
    while True:
        item = await chat_queue.get()
        try:
            if item is None:
                if batch:
                    await post_to_chat_webhook(batch, state, settings)
                    batch = []
                return

            batch.append(item)
            if len(batch) >= settings.chat_batch_size:
                await post_to_chat_webhook(batch, state, settings)
                batch = []
        finally:
            chat_queue.task_done()
