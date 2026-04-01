import ssl
from datetime import datetime

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
from core.state import ScraperState
from core.utils import format_metric_with_emoji, sanitize_store_name


async def post_to_chat_webhook(entries: list[dict[str, str]], state: ScraperState):
    if not CHAT_WEBHOOK_URL or not entries:
        return
    try:
        state.chat_batch_count += 1
        batch_header_text = datetime.now(LOCAL_TIMEZONE).strftime("%A %d %B, %H:%M")
        card_subtitle = f"{batch_header_text}  Batch {state.chat_batch_count} ({len(entries)} stores)"

        sorted_entries = sorted(entries, key=lambda e: sanitize_store_name(e.get("store", "")))

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

            formatted_uph = format_metric_with_emoji(uph_val, UPH_THRESHOLD, is_uph=True)
            formatted_lates = format_metric_with_emoji(lates_val, LATES_THRESHOLD)
            formatted_inf = format_metric_with_emoji(inf_val, INF_THRESHOLD)

            grid_items.extend(
                [
                    {"title": sanitize_store_name(entry.get("store", "N/A")), "textAlignment": "START"},
                    {"title": formatted_uph, "textAlignment": "CENTER"},
                    {"title": formatted_lates, "textAlignment": "CENTER"},
                    {"title": formatted_inf, "textAlignment": "CENTER"},
                ]
            )

        table_section = {
            "header": "Key Performance Indicators",
            "widgets": [
                {
                    "grid": {
                        "title": "Performance Summary",
                        "columnCount": 4,
                        "borderStyle": {"type": "STROKE", "cornerRadius": 4},
                        "items": grid_items,
                    }
                }
            ],
        }

        payload = {
            "cardsV2": [
                {
                    "cardId": f"batch-summary-{state.chat_batch_count}",
                    "card": {
                        "header": {
                            "title": "Seller Central Metrics Report (1MMS)",
                            "subtitle": card_subtitle,
                            "imageUrl": "https://i.imgur.com/u0e3d2x.png",
                            "imageType": "CIRCLE",
                        },
                        "sections": [table_section],
                    },
                }
            ]
        }

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


async def post_job_summary(state: ScraperState, duration: float):
    if not CHAT_WEBHOOK_URL:
        return
    try:
        total = state.progress["total"]
        success = state.progress["current"]
        failures = state.run_failures

        status_text = "Job Completed Successfully"
        status_icon = "✅"
        if failures:
            status_text = f"Job Completed with {len(failures)} Failures"
            status_icon = "⚠️"

        success_rate = (success / total) * 100 if total > 0 else 0
        throughput_spm = (success / (duration / 60)) if duration > 0 else 0

        coll_times = state.metrics["collection_times"]
        sub_times = state.metrics["submission_times"]
        retries = state.metrics["retries"]
        retry_stores = len(state.metrics["retry_stores"])
        total_orders = state.metrics["total_orders"]
        total_units = state.metrics["total_units"]

        avg_coll = sum(t[1] for t in coll_times) / len(coll_times) if coll_times else 0
        avg_sub = sum(t[1] for t in sub_times) / len(sub_times) if sub_times else 0

        sorted_coll = sorted([t[1] for t in coll_times])
        p95_coll = sorted_coll[int(len(sorted_coll) * 0.95)] if sorted_coll else 0
        fastest_store = min(coll_times, key=lambda x: x[1]) if coll_times else ("N/A", 0)
        slowest_store = max(coll_times, key=lambda x: x[1]) if coll_times else ("N/A", 0)

        bottleneck_msg = "Balanced Flow"
        if avg_coll > 2.0:
            bottleneck_msg = "🐢 Slow Scraping (Browser Lag)"
        elif avg_sub > 1.0:
            bottleneck_msg = "🐢 Slow Submission (Webhook Lag)"
        elif avg_coll < 1.0 and avg_sub < 0.5:
            bottleneck_msg = "🚀 High Speed (No Bottlenecks)"

        stats_section = {
            "header": "High-Level Stats",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Throughput",
                        "text": f"{throughput_spm:.1f} stores/min",
                        "startIcon": {"knownIcon": "FLIGHT_DEPARTURE"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Success Rate",
                        "text": f"{success}/{total} ({success_rate:.1f}%)",
                        "startIcon": {"knownIcon": "STAR"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Total Duration",
                        "text": f"{duration:.2f}s",
                        "startIcon": {"knownIcon": "CLOCK"},
                    }
                },
            ],
        }

        volume_section = {
            "header": "Business Volume \U0001f4e6",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Total Orders",
                        "text": f"{total_orders:,}",
                        "startIcon": {"knownIcon": "SHOPPING_CART"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Total Units",
                        "text": f"{total_units:,}",
                        "startIcon": {"knownIcon": "TICKET"},
                    }
                },
            ],
        }

        resilience_section = {
            "header": "Resilience & Health \U0001f3e5",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Total Retries",
                        "text": str(retries),
                        "startIcon": {"knownIcon": "MEMBERSHIP"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Stores Retried",
                        "text": str(retry_stores),
                        "startIcon": {"knownIcon": "STORE"},
                    }
                },
            ],
        }

        speed_section = {
            "header": "Speed Breakdown \u23f1\ufe0f",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Avg Collection Time",
                        "text": f"{avg_coll:.2f}s (Browser)",
                        "startIcon": {"knownIcon": "DESCRIPTION"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "p95 Collection Time",
                        "text": f"{p95_coll:.2f}s",
                        "startIcon": {"knownIcon": "DESCRIPTION"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Bottleneck Status",
                        "text": bottleneck_msg,
                        "startIcon": {"knownIcon": "TRAFFIC"},
                    }
                },
            ],
        }

        extremes_section = {
            "header": "Extremes \U0001f4c9\U0001f4c8",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Fastest Store",
                        "text": f"{fastest_store[0]} ({fastest_store[1]:.2f}s)",
                        "startIcon": {"knownIcon": "BOLT"},
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Slowest Store",
                        "text": f"{slowest_store[0]} ({slowest_store[1]:.2f}s)",
                        "startIcon": {"knownIcon": "SNAIL"},
                    }
                },
            ],
        }

        sections = [stats_section, volume_section, resilience_section, speed_section, extremes_section]

        if failures:
            failure_counts = {}
            for f in failures:
                msg = f
                if "(" in f and ")" in f:
                    msg = f[f.rfind("(") + 1 : f.rfind(")")]
                failure_counts[msg] = failure_counts.get(msg, 0) + 1

            failure_summary = "\n".join([f"• {k}: {v}" for k, v in failure_counts.items()])
            failure_list = "\n".join([f"• {f}" for f in failures[:5]])
            if len(failures) > 5:
                failure_list += f"\n...and {len(failures) - 5} more"

            failures_section = {
                "header": "Failure Analysis",
                "widgets": [
                    {"textParagraph": {"text": f"<b>Breakdown:</b>\n{failure_summary}"}},
                    {
                        "textParagraph": {
                            "text": f'<font color="#FF0000"><b>Recent Failures:</b>\n{failure_list}</font>'
                        }
                    },
                ],
            }
            sections.append(failures_section)

        payload = {
            "cardsV2": [
                {
                    "cardId": f"job-summary-{int(datetime.now().timestamp())}",
                    "card": {
                        "header": {
                            "title": f"{status_icon} {status_text} (1MMS)",
                            "subtitle": datetime.now(LOCAL_TIMEZONE).strftime("%A %d %B, %H:%M"),
                            "imageUrl": "https://i.imgur.com/u0e3d2x.png",
                            "imageType": "CIRCLE",
                        },
                        "sections": sections,
                    },
                }
            ]
        }

        timeout = aiohttp.ClientTimeout(total=30)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.post(CHAT_WEBHOOK_URL, json=payload) as resp:
                if resp.status != 200:
                    app_logger.error(f"Job summary post failed: {resp.status}")

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
