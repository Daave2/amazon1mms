import re
import ssl
from datetime import datetime
from html import escape

import aiohttp
import certifi

from core.config import (
    CHAT_BATCH_SIZE,
    CHAT_WEBHOOK_URL,
    DEBUG_MODE,
    INF_THRESHOLD,
    LATES_THRESHOLD,
    LOCAL_TIMEZONE,
    UPH_THRESHOLD,
)
from core.logger import app_logger
from core.reporting import (
    FAILURE_CATEGORY_LABELS,
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


def _build_batch_attention_lines(entries: list[dict[str, str]]) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        issues: list[str] = []
        uph_value = _extract_numeric_value(entry.get("uph"))
        lates_value = _extract_numeric_value(entry.get("lates"))
        inf_value = _extract_numeric_value(entry.get("inf"))

        if uph_value is not None and uph_value < UPH_THRESHOLD:
            issues.append(f"UPH {entry.get('uph', 'N/A')}")
        if lates_value is not None and lates_value > LATES_THRESHOLD:
            issues.append(f"Lates {entry.get('lates', 'N/A')}")
        if inf_value is not None and inf_value > INF_THRESHOLD:
            issues.append(f"INF {entry.get('inf', 'N/A')}")

        if issues:
            lines.append(f"{sanitize_store_name(entry.get('store', 'Unknown'))}: {', '.join(issues)}")

    return lines


def build_batch_chat_payload(entries: list[dict[str, str]], state: ScraperState) -> dict[str, object]:
    batch_header_text = datetime.now(LOCAL_TIMEZONE).strftime("%A %d %B, %H:%M")
    card_subtitle = f"{batch_header_text} • Batch {state.chat_batch_count} • {len(entries)} stores"

    sorted_entries = sorted(entries, key=lambda entry: sanitize_store_name(entry.get("store", "")))
    attention_lines = _build_batch_attention_lines(sorted_entries)

    grid_items = [
        {"title": "Store", "textAlignment": "START"},
        {"title": "UPH", "textAlignment": "CENTER"},
        {"title": "Lates", "textAlignment": "CENTER"},
        {"title": "INF", "textAlignment": "CENTER"},
    ]

    for entry in sorted_entries:
        uph_val = entry.get("uph", "N/A")
        lates_val = entry.get("lates", "0.0 %") or "0.0 %"
        inf_val = entry.get("inf", "0.0 %") or "0.0 %"

        grid_items.extend(
            [
                {
                    "title": sanitize_store_name(entry.get("store", "N/A")),
                    "textAlignment": "START",
                },
                {
                    "title": format_metric_with_emoji(uph_val, UPH_THRESHOLD, is_uph=True),
                    "textAlignment": "CENTER",
                },
                {
                    "title": format_metric_with_emoji(lates_val, LATES_THRESHOLD),
                    "textAlignment": "CENTER",
                },
                {
                    "title": format_metric_with_emoji(inf_val, INF_THRESHOLD),
                    "textAlignment": "CENTER",
                },
            ]
        )

    sections = [
        {
            "header": "Batch Overview",
            "widgets": [
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
                        "text": str(len(attention_lines)),
                        "startIcon": {"knownIcon": "STORE"},
                    }
                },
            ],
        },
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
        },
    ]

    if attention_lines:
        visible_lines = attention_lines[:6]
        if len(attention_lines) > len(visible_lines):
            visible_lines.append(f"...and {len(attention_lines) - len(visible_lines)} more")
        sections.append(
            {
                "header": "Attention Needed",
                "widgets": [
                    {
                        "textParagraph": {
                            "text": _format_html_lines(visible_lines),
                        }
                    }
                ],
            }
        )

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
            }
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


def _build_failure_lines(counter_map: dict[str, int], label_map: dict[str, str] | None = None) -> list[str]:
    if not counter_map:
        return []

    sorted_items = sorted(counter_map.items(), key=lambda item: (-item[1], item[0]))
    lines = []
    for key, count in sorted_items[:5]:
        label = label_map.get(key, _humanize_token(key)) if label_map else key
        lines.append(f"{label}: {count}")
    return lines


def _build_timing_text(store_timing: dict[str, object] | None) -> str:
    if not store_timing:
        return "N/A"
    return f"{sanitize_store_name(str(store_timing['store']))} ({store_timing['seconds']:.2f}s)"


def build_job_summary_payload(state: ScraperState, duration: float | None = None) -> dict[str, object]:
    summary = build_run_summary(state)
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

    if issue_total:
        failure_summary = summary["failure_summary"]
        category_lines = _build_failure_lines(
            failure_summary["category_counts"],
            FAILURE_CATEGORY_LABELS,
        )
        reason_lines = _build_failure_lines(failure_summary["reason_counts"])
        recent_lines = list(failure_summary["recent_failures"])
        if failure_summary["overflow_count"]:
            recent_lines.append(f"...and {failure_summary['overflow_count']} more")

        issue_widgets = [
            {
                "decoratedText": {
                    "topLabel": "Issue Totals",
                    "text": f"{issue_total} total • {terminal_failures} terminal • {non_terminal_events} non-terminal",
                    "startIcon": {"knownIcon": "DESCRIPTION"},
                }
            }
        ]
        if category_lines:
            issue_widgets.append(
                {
                    "textParagraph": {
                        "text": f"<b>By Category</b><br>{_format_html_lines(category_lines)}",
                    }
                }
            )
        if reason_lines:
            issue_widgets.append(
                {
                    "textParagraph": {
                        "text": f"<b>Top Reasons</b><br>{_format_html_lines(reason_lines)}",
                    }
                }
            )
        if recent_lines:
            issue_widgets.append(
                {
                    "textParagraph": {
                        "text": f"<b>Recent Events</b><br>{_format_html_lines(recent_lines)}",
                    }
                }
            )

        sections.append({"header": "Issues", "widgets": issue_widgets})

    return {
        "cardsV2": [
            {
                "cardId": f"job-summary-{int(datetime.now().timestamp())}",
                "card": {
                    "header": {
                        "title": f"{status_icon} {status_title} (1MMS)",
                        "subtitle": datetime.now(LOCAL_TIMEZONE).strftime("%A %d %B, %H:%M"),
                        "imageUrl": CARD_IMAGE_URL,
                        "imageType": "CIRCLE",
                    },
                    "sections": sections,
                },
            }
        ]
    }


async def post_to_chat_webhook(entries: list[dict[str, str]], state: ScraperState):
    if not CHAT_WEBHOOK_URL or not entries:
        return
    try:
        state.chat_batch_count += 1
        payload = build_batch_chat_payload(entries, state)

        timeout = aiohttp.ClientTimeout(total=30)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.post(CHAT_WEBHOOK_URL, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    app_logger.error(f"Chat webhook post failed. Status: {resp.status}. Response: {error_text}")
    except Exception as e:
        app_logger.error(f"Error posting to chat webhook: {e}", exc_info=DEBUG_MODE)


async def post_job_summary(state: ScraperState, duration: float | None = None):
    if not CHAT_WEBHOOK_URL or state.job_summary_posted:
        return
    try:
        payload = build_job_summary_payload(state, duration)

        timeout = aiohttp.ClientTimeout(total=30)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.post(CHAT_WEBHOOK_URL, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    app_logger.error(f"Job summary post failed: {resp.status}. Response: {error_text}")
                    return

        state.job_summary_posted = True
    except Exception as e:
        app_logger.error(f"Error posting job summary: {e}", exc_info=DEBUG_MODE)


async def add_to_pending_chat(entry: dict[str, str], state: ScraperState):
    if not CHAT_WEBHOOK_URL:
        return
    async with state.pending_chat_lock:
        state.pending_chat_entries.append(entry)
        if len(state.pending_chat_entries) >= CHAT_BATCH_SIZE:
            entries_to_send = state.pending_chat_entries[:CHAT_BATCH_SIZE]
            del state.pending_chat_entries[:CHAT_BATCH_SIZE]
            await post_to_chat_webhook(entries_to_send, state)


async def flush_pending_chat_entries(state: ScraperState):
    if not CHAT_WEBHOOK_URL:
        return
    async with state.pending_chat_lock:
        if state.pending_chat_entries:
            entries = state.pending_chat_entries[:]
            state.pending_chat_entries.clear()
            await post_to_chat_webhook(entries, state)
