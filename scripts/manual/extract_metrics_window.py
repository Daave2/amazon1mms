"""
Manually extract store metrics for an explicit date/hour window without submitting to the form.

Examples:
    python scripts/manual/extract_metrics_window.py --stores "Morrisons York" --window-preset today
    python scripts/manual/extract_metrics_window.py --stores "Morrisons York,Morrisons Aberdeen" --window-preset this_week
    python scripts/manual/extract_metrics_window.py --window-preset custom --start "2026-04-07 00" --end "2026-04-07 12"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import load_settings  # noqa: E402
from core.logger import app_logger, configure_logging  # noqa: E402
from core.metrics import build_form_data, normalize_metrics_payload  # noqa: E402
from core.reporting import write_runtime_reports  # noqa: E402
from core.state import CacheManager, ScraperState  # noqa: E402
from core.store_loader import load_stores_from_csv  # noqa: E402
from core.utils import ensure_directory, optimize_browser_context, safe_close  # noqa: E402
from services.chat_service import chat_dispatcher_worker, post_job_summary  # noqa: E402
from services.forms_service import SubmissionManager, SubmissionTask, build_submission_id  # noqa: E402
from services.metrics_service import (  # noqa: E402
    _build_fast_path_target_url,
    fetch_metrics_pair_fast_path,
    resolve_dropdown_name,
)


WINDOW_PRESETS = {"today", "yesterday", "this_week", "last_7_days", "custom"}
DEFAULT_FOCUS_STORE = "Morrisons Thornton Cleveleys"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stores",
        default="",
        help="Comma-separated configured store names. Leave blank to extract all configured stores with merchant ids.",
    )
    parser.add_argument(
        "--window-preset",
        choices=sorted(WINDOW_PRESETS),
        default="today",
        help="Named date window to extract.",
    )
    parser.add_argument(
        "--start",
        default="",
        help="Custom start in local time. Accepted formats: YYYY-MM-DD HH or YYYY-MM-DDTHH.",
    )
    parser.add_argument(
        "--end",
        default="",
        help="Custom end in local time. Accepted formats: YYYY-MM-DD HH or YYYY-MM-DDTHH.",
    )
    parser.add_argument(
        "--focus-store",
        default=DEFAULT_FOCUS_STORE,
        help="Store to highlight in the comparison summary while still extracting all stores.",
    )
    parser.add_argument(
        "--no-focus-store",
        action="store_true",
        help="Skip focus-store comparison output and post the full-network summary only.",
    )
    return parser.parse_args()


def _parse_local_hour(value: str, timezone) -> datetime:
    cleaned = value.strip()
    for fmt in ("%Y-%m-%d %H", "%Y-%m-%dT%H", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed.replace(tzinfo=timezone, minute=0, second=0, microsecond=0)
        except ValueError:
            continue
    raise ValueError(f"Unsupported datetime format: {value!r}")


def _resolve_window(args: argparse.Namespace, timezone) -> tuple[datetime, datetime, str]:
    now = datetime.now(timezone).replace(minute=0, second=0, microsecond=0)
    preset = args.window_preset

    if preset == "today":
        return now.replace(hour=0), now, "today"
    if preset == "yesterday":
        yesterday = now - timedelta(days=1)
        start = yesterday.replace(hour=0)
        end = yesterday.replace(hour=23)
        return start, end, "yesterday"
    if preset == "this_week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0)
        return start, now, "this_week"
    if preset == "last_7_days":
        start = (now - timedelta(days=6)).replace(hour=0)
        return start, now, "last_7_days"
    if preset == "custom":
        if not args.start or not args.end:
            raise ValueError("--start and --end are required when --window-preset=custom")
        start = _parse_local_hour(args.start, timezone)
        end = _parse_local_hour(args.end, timezone)
        if end < start:
            raise ValueError("Custom end must be at or after custom start")
        return start, end, "custom"
    raise ValueError(f"Unsupported window preset: {preset}")


def _select_stores(store_rows: list[dict[str, str]], store_filter: str, cache: CacheManager, settings) -> list[dict[str, str]]:
    if not store_filter.strip():
        selected = store_rows
    else:
        requested = {item.strip().lower() for item in store_filter.split(",") if item.strip()}
        selected = [
            row
            for row in store_rows
            if row["store_name"].strip().lower() in requested
            or resolve_dropdown_name(row["store_name"], settings).strip().lower() in requested
            or row.get("dropdown_name", "").strip().lower() in requested
        ]

    prepared: list[dict[str, str]] = []
    for row in selected:
        merchant_id = row.get("merchant_id", "").strip() or cache.merchant_id_cache.get(row["store_name"], "").strip()
        if not merchant_id:
            continue
        prepared.append(
            {
                **row,
                "merchant_id": merchant_id,
            }
        )
    return prepared


def _store_slug(store_name: str) -> str:
    return store_name.lower().replace(" ", "_").replace("/", "_")


def _normalize_store_label(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _safe_divide(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _format_signed_delta(value: float, decimals: int = 1, suffix: str = "") -> str:
    return f"{value:+.{decimals}f}{suffix}"


def _format_share(part: float, whole: float) -> str:
    return f"{_safe_divide(part, whole) * 100:.1f}%"


def _aggregate_normalized_metrics(metric_rows: list[dict[str, float]]) -> dict[str, float]:
    total_orders = sum(float(row.get("OrdersShopped_V2") or 0.0) for row in metric_rows)
    total_units = sum(float(row.get("RequestedQuantity_V2") or 0.0) for row in metric_rows)
    total_fulfilled = sum(float(row.get("PickedUnits_V2") or 0.0) for row in metric_rows)
    total_time_available = sum(float(row.get("TimeAvailable_V2") or 0.0) for row in metric_rows)
    total_cancellations = sum(float(row.get("OrderCancellations") or 0.0) for row in metric_rows)
    total_late_orders = sum(
        float(row.get("OrdersShopped_V2") or 0.0) * (float(row.get("LatePicksRate") or 0.0) / 100.0)
        for row in metric_rows
    )
    total_inf_units = sum(
        float(row.get("RequestedQuantity_V2") or 0.0) * (float(row.get("ItemNotFoundRate_V2") or 0.0) / 100.0)
        for row in metric_rows
    )

    total_pick_hours = 0.0
    for row in metric_rows:
        picked_units = float(row.get("PickedUnits_V2") or 0.0)
        uph = float(row.get("AverageUPH_V2") or 0.0)
        if picked_units > 0 and uph > 0:
            total_pick_hours += picked_units / uph

    inf_rate = _safe_divide(total_inf_units, total_units) * 100.0

    return {
        "OrdersShopped_V2": total_orders,
        "RequestedQuantity_V2": total_units,
        "PickedUnits_V2": total_fulfilled,
        "AverageUPH_V2": _safe_divide(total_fulfilled, total_pick_hours),
        "LatePicksRate": _safe_divide(total_late_orders, total_orders) * 100.0,
        "ItemNotFoundRate_V2": inf_rate,
        "ItemFoundRate_V2": 100.0 - inf_rate if total_units > 0 else 0.0,
        "OrderCancellations": total_cancellations,
        "TimeAvailable_V2": total_time_available,
    }


def _find_focus_store_result(results: list[dict[str, Any]], focus_store: str) -> dict[str, Any] | None:
    requested = _normalize_store_label(focus_store)
    exact_match = None
    partial_match = None

    for result in results:
        candidates = {
            _normalize_store_label(result.get("store", "")),
            _normalize_store_label(result.get("dropdownName", "")),
        }
        if requested in candidates:
            exact_match = result
            break
        if requested and any(requested in candidate for candidate in candidates if candidate):
            partial_match = result

    return exact_match or partial_match


def _compute_rankings(results: list[dict[str, Any]], focus_store_name: str) -> dict[str, dict[str, Any]]:
    focus_store_key = _normalize_store_label(focus_store_name)
    ranking_specs = [
        ("orders", "OrdersShopped_V2", True, "Higher is better"),
        ("units", "RequestedQuantity_V2", True, "Higher is better"),
        ("uph", "AverageUPH_V2", True, "Higher is better"),
        ("inf", "ItemNotFoundRate_V2", False, "Lower is better"),
        ("found", "ItemFoundRate_V2", True, "Higher is better"),
        ("cancelled", "OrderCancellations", False, "Lower is better"),
        ("lates", "LatePicksRate", False, "Lower is better"),
    ]

    rankings: dict[str, dict[str, Any]] = {}
    for label, metric_key, higher_is_better, guidance in ranking_specs:
        if higher_is_better:
            ordered = sorted(
                results,
                key=lambda item: (
                    -(float(item["normalizedCombined"].get(metric_key) or 0.0)),
                    item["store"].lower(),
                ),
            )
        else:
            ordered = sorted(
                results,
                key=lambda item: (
                    float(item["normalizedCombined"].get(metric_key) or 0.0),
                    item["store"].lower(),
                ),
            )

        for index, result in enumerate(ordered, start=1):
            if _normalize_store_label(result["store"]) == focus_store_key:
                rankings[label] = {
                    "position": index,
                    "total": len(ordered),
                    "guidance": guidance,
                }
                break

    return rankings


def _build_focus_store_summary(
    results: list[dict[str, Any]],
    focus_store: str,
    window_name: str,
    start_dt: datetime,
    end_dt: datetime,
    timezone,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "requestedFocusStore": focus_store,
        "focusStoreFound": False,
        "storeCount": len(results),
        "windowName": window_name,
        "windowStart": start_dt.isoformat(),
        "windowEnd": end_dt.isoformat(),
        "allStoresIncluded": True,
        "allStoresCsv": "summary.csv",
    }

    if not results:
        return summary

    network_metrics = _aggregate_normalized_metrics([result["normalizedCombined"] for result in results])
    network_display = build_form_data("Network", network_metrics, current_dt=end_dt, local_timezone=timezone)
    summary["networkMetrics"] = network_metrics
    summary["networkDisplay"] = network_display

    focus_result = _find_focus_store_result(results, focus_store)
    if focus_result is None:
        summary["availableStores"] = [result["store"] for result in results]
        return summary

    peer_results = [result for result in results if result["store"] != focus_result["store"]]
    peer_metrics = _aggregate_normalized_metrics([result["normalizedCombined"] for result in peer_results])
    peer_display = build_form_data("Peer Network", peer_metrics, current_dt=end_dt, local_timezone=timezone)
    focus_metrics = focus_result["normalizedCombined"]
    focus_display = focus_result["displayMetrics"]

    summary["focusStoreFound"] = True
    summary["matchedStore"] = focus_result["store"]
    summary["matchedDropdownName"] = focus_result.get("dropdownName", "")
    summary["focusMetrics"] = focus_metrics
    summary["focusDisplay"] = focus_display
    summary["peerNetworkMetrics"] = peer_metrics
    summary["peerNetworkDisplay"] = peer_display
    summary["shares"] = {
        "ordersPct": _safe_divide(float(focus_metrics.get("OrdersShopped_V2") or 0.0), float(network_metrics.get("OrdersShopped_V2") or 0.0))
        * 100.0,
        "unitsPct": _safe_divide(float(focus_metrics.get("RequestedQuantity_V2") or 0.0), float(network_metrics.get("RequestedQuantity_V2") or 0.0))
        * 100.0,
        "fulfilledPct": _safe_divide(float(focus_metrics.get("PickedUnits_V2") or 0.0), float(network_metrics.get("PickedUnits_V2") or 0.0))
        * 100.0,
        "cancelledPct": _safe_divide(
            float(focus_metrics.get("OrderCancellations") or 0.0),
            float(network_metrics.get("OrderCancellations") or 0.0),
        )
        * 100.0,
    }
    summary["deltas"] = {
        "uph": float(focus_metrics.get("AverageUPH_V2") or 0.0) - float(network_metrics.get("AverageUPH_V2") or 0.0),
        "inf": float(focus_metrics.get("ItemNotFoundRate_V2") or 0.0)
        - float(network_metrics.get("ItemNotFoundRate_V2") or 0.0),
        "found": float(focus_metrics.get("ItemFoundRate_V2") or 0.0)
        - float(network_metrics.get("ItemFoundRate_V2") or 0.0),
        "lates": float(focus_metrics.get("LatePicksRate") or 0.0) - float(network_metrics.get("LatePicksRate") or 0.0),
    }
    summary["rankings"] = _compute_rankings(results, focus_result["store"])

    return summary


def _build_no_focus_summary(
    results: list[dict[str, Any]],
    window_name: str,
    start_dt: datetime,
    end_dt: datetime,
) -> dict[str, Any]:
    return {
        "requestedFocusStore": "",
        "focusStoreFound": False,
        "storeCount": len(results),
        "windowName": window_name,
        "windowStart": start_dt.isoformat(),
        "windowEnd": end_dt.isoformat(),
        "allStoresIncluded": True,
        "allStoresCsv": "summary.csv",
    }


def _build_focus_store_markdown(summary: dict[str, Any]) -> str:
    lines = ["## Focus Store Comparison", ""]
    lines.append(f"- Focus store requested: `{summary['requestedFocusStore']}`")
    lines.append(f"- Window: `{summary['windowStart']}` -> `{summary['windowEnd']}`")
    lines.append(f"- All stores included: `{summary['storeCount']}`")
    lines.append(f"- Full network export: `{summary['allStoresCsv']}`")

    if not summary.get("focusStoreFound"):
        lines.append("- Requested focus store was not present in this extract.")
        return "\n".join(lines) + "\n"

    focus_display = summary["focusDisplay"]
    network_display = summary["networkDisplay"]
    shares = summary["shares"]
    deltas = summary["deltas"]
    rankings = summary["rankings"]

    lines.extend(
        [
            "",
            f"### {summary['matchedStore']}",
            "",
            "| Metric | Focus Store | Network |",
            "| --- | ---: | ---: |",
            f"| Orders | {focus_display['orders']} | {network_display['orders']} |",
            f"| Units Requested | {focus_display['units']} | {network_display['units']} |",
            f"| Units Fulfilled | {focus_display['fulfilled']} | {network_display['fulfilled']} |",
            f"| UPH | {focus_display['uph']} | {network_display['uph']} |",
            f"| INF | {focus_display['inf']} | {network_display['inf']} |",
            f"| Item Found Rate | {focus_display['found']} | {network_display['found']} |",
            f"| Order cancellations | {focus_display['cancelled']} | {network_display['cancelled']} |",
            f"| Late Picks | {focus_display['lates']} | {network_display['lates']} |",
            "",
            "### Store vs Network",
            "",
            f"- Orders share of network: `{_format_share(float(summary['focusMetrics'].get('OrdersShopped_V2') or 0.0), float(summary['networkMetrics'].get('OrdersShopped_V2') or 0.0))}`",
            f"- Units share of network: `{_format_share(float(summary['focusMetrics'].get('RequestedQuantity_V2') or 0.0), float(summary['networkMetrics'].get('RequestedQuantity_V2') or 0.0))}`",
            f"- Fulfilled units share of network: `{_format_share(float(summary['focusMetrics'].get('PickedUnits_V2') or 0.0), float(summary['networkMetrics'].get('PickedUnits_V2') or 0.0))}`",
            f"- UPH vs network: `{_format_signed_delta(deltas['uph'], decimals=0)}`",
            f"- INF vs network: `{_format_signed_delta(deltas['inf'], suffix=' pp')}`",
            f"- Item found vs network: `{_format_signed_delta(deltas['found'], suffix=' pp')}`",
            f"- Late Picks vs network: `{_format_signed_delta(deltas['lates'], suffix=' pp')}`",
            f"- Order cancellations share of network: `{shares['cancelledPct']:.1f}%`",
            "",
            "### Network Ranking",
            "",
            f"- Orders rank: `{rankings['orders']['position']}/{rankings['orders']['total']}`",
            f"- Units rank: `{rankings['units']['position']}/{rankings['units']['total']}`",
            f"- UPH rank: `{rankings['uph']['position']}/{rankings['uph']['total']}` ({rankings['uph']['guidance']})",
            f"- INF rank: `{rankings['inf']['position']}/{rankings['inf']['total']}` ({rankings['inf']['guidance']})",
            f"- Item found rank: `{rankings['found']['position']}/{rankings['found']['total']}` ({rankings['found']['guidance']})",
            f"- Order cancellations rank: `{rankings['cancelled']['position']}/{rankings['cancelled']['total']}` ({rankings['cancelled']['guidance']})",
            f"- Late Picks rank: `{rankings['lates']['position']}/{rankings['lates']['total']}` ({rankings['lates']['guidance']})",
            "",
        ]
    )

    return "\n".join(lines)


class ExtractSubmissionManager(SubmissionManager):
    async def record_extracted_submission(self, form_data: dict[str, str]) -> SubmissionTask:
        task = SubmissionTask(
            submission_id=build_submission_id(self.state.run_id, form_data.get("store", "Unknown")),
            run_id=self.state.run_id,
            form_data=dict(form_data),
        )
        await self.ledger.append_event(self._build_event(task, status="sent", http_status=204))
        self.state.submissions.queued += 1
        self.state.submissions.sent += 1
        await self.log_submission(task)
        await self.state.increment_progress()
        return task


async def run_extraction(args: argparse.Namespace):
    settings = load_settings()
    focus_store = "" if args.no_focus_store else args.focus_store.strip()
    ensure_directory(settings.output_dir)
    configure_logging(settings)
    storage_state_path = Path(settings.storage_state_path)
    if not storage_state_path.exists():
        raise FileNotFoundError(
            f"Storage state file not found at '{storage_state_path}'. Run the normal scraper login flow first."
        )

    cache = CacheManager(settings)
    cache.load()
    if not cache.api_url_template:
        raise RuntimeError("Discovery cache does not contain an API URL template. Run the scraper first to prime discovery.")

    start_dt, end_dt, resolved_window_name = _resolve_window(args, settings.local_timezone)
    store_rows = load_stores_from_csv("urls.csv")
    selected_stores = _select_stores(store_rows, args.stores, cache, settings)
    if not selected_stores:
        raise RuntimeError("No stores matched the requested filter with a usable merchant id.")

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = REPO_ROOT / settings.output_dir / "extracts" / f"metrics_window_{resolved_window_name}_{timestamp}"
    responses_dir = run_dir / "responses"
    ensure_directory(str(responses_dir))
    app_logger.info("Manual extract starting.")
    app_logger.info(
        "Window resolved to %s -> %s (%s).",
        start_dt.isoformat(),
        end_dt.isoformat(),
        resolved_window_name,
    )
    if args.stores.strip():
        app_logger.info("Store filter requested: %s", args.stores)
    else:
        app_logger.info("No store filter provided. Extracting all configured stores with merchant ids.")
    if focus_store:
        app_logger.info("Focus store for comparison output: %s", focus_store)
    else:
        app_logger.info("No focus store requested. Extracting and posting the full-network summary only.")
    app_logger.info("Discovery cache template available: %s", bool(cache.api_url_template))
    app_logger.info("%s stores selected for extraction.", len(selected_stores))
    app_logger.info("Artifacts will be written to %s", run_dir)

    state = ScraperState(settings=settings)
    state.chat_focus_store = focus_store
    await state.init_progress(len(selected_stores))
    state.browser_worker_pool_size = settings.fast_path_max_concurrency
    state.form_submitter_count = 0
    browser = None
    context = None
    page = None
    chat_queue: asyncio.Queue = asyncio.Queue()
    submission_manager = ExtractSubmissionManager(settings, state, chat_queue)
    chat_dispatcher_task = asyncio.create_task(chat_dispatcher_worker(chat_queue, state, settings))
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    started_at = time.monotonic()

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(storage_state_path))
            await optimize_browser_context(context, settings)
            page = await context.new_page()
            await page.goto(settings.base_dashboard_url, timeout=settings.page_timeout_ms, wait_until="domcontentloaded")
            login_visible = await page.locator("input#ap_email, input#ap_password, input[name='email']").first.is_visible()
            if login_visible:
                state.auth_state_status = "refresh_required"
                raise RuntimeError("Saved auth state is not valid for manual extraction.")
            state.auth_state_status = "reused"
            app_logger.info("Saved auth state is valid for manual extraction.")

            request_client = context.request

            async def extract_single_store(index: int, store: dict[str, str]):
                store_name = store["store_name"]
                merchant_id = store["merchant_id"]
                store_started_at = time.monotonic()
                app_logger.info("[%s/%s] Starting extraction for %s", index, len(selected_stores), store_name)
                try:
                    summary_url = _build_fast_path_target_url(
                        cache.api_url_template,
                        merchant_id,
                        settings,
                        start_dt=start_dt,
                        end_dt=end_dt,
                    )
                    detail_url = _build_fast_path_target_url(
                        cache.api_url_template,
                        merchant_id,
                        settings,
                        detail=True,
                        start_dt=start_dt,
                        end_dt=end_dt,
                    )

                    summary_payload, detail_payload = await fetch_metrics_pair_fast_path(
                        request_client,
                        summary_url,
                        detail_url,
                        store_name,
                        state,
                        45_000,
                    )

                    summary_path = responses_dir / f"{_store_slug(store_name)}_summary.json"
                    detail_path = responses_dir / f"{_store_slug(store_name)}_detail.json"
                    summary_path.write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")
                    detail_path.write_text(json.dumps(detail_payload, indent=2), encoding="utf-8")

                    normalized_summary = normalize_metrics_payload(summary_payload)
                    normalized_detail = normalize_metrics_payload(detail_payload)
                    combined = dict(normalized_summary)
                    combined["LatePicksRate"] = normalized_detail.get("LatePicksRate", 0.0)
                    combined["OrderCancellations"] = normalized_detail.get("OrderCancellations", 0.0)
                    display_metrics = build_form_data(
                        store_name,
                        combined,
                        current_dt=end_dt,
                        local_timezone=settings.local_timezone,
                    )
                    await submission_manager.record_extracted_submission(display_metrics)
                    await state.record_metric(
                        store_name,
                        time.monotonic() - store_started_at,
                        int(combined.get("OrdersShopped_V2", 0)),
                        int(combined.get("RequestedQuantity_V2", 0)),
                        path="fast_path",
                    )

                    results.append(
                        {
                            "store": store_name,
                            "dropdownName": store.get("dropdown_name", ""),
                            "merchantId": merchant_id,
                            "windowName": resolved_window_name,
                            "windowStart": start_dt.isoformat(),
                            "windowEnd": end_dt.isoformat(),
                            "summaryUrl": summary_url,
                            "detailUrl": detail_url,
                            "summaryPath": str(summary_path),
                            "detailPath": str(detail_path),
                            "normalizedSummary": normalized_summary,
                            "normalizedDetail": normalized_detail,
                            "normalizedCombined": combined,
                            "displayMetrics": display_metrics,
                        }
                    )
                    app_logger.info(
                        "[%s/%s] Finished %s: orders=%s units=%s lates=%s inf=%s cancelled=%s",
                        index,
                        len(selected_stores),
                        store_name,
                        display_metrics["orders"],
                        display_metrics["units"],
                        display_metrics["lates"],
                        display_metrics["inf"],
                        display_metrics["cancelled"],
                    )
                except Exception as exc:
                    failures.append({"store": store_name, "error": str(exc)})
                    await state.add_failure(
                        f"{store_name} (Manual Extract Failure)",
                        asyncio.get_running_loop().time(),
                        category="api_fast_path",
                    )
                    app_logger.exception("[%s/%s] Extraction failed for %s: %s", index, len(selected_stores), store_name, exc)
            await asyncio.gather(
                *(extract_single_store(index, store) for index, store in enumerate(selected_stores, start=1))
            )
    finally:
        await chat_queue.put(None)
        await chat_queue.join()
        await chat_dispatcher_task
        await safe_close(page, "Manual extraction page")
        await safe_close(context, "Manual extraction context")
        await safe_close(browser, "Manual extraction browser")

    results.sort(key=lambda item: item["store"].lower())
    failures.sort(key=lambda item: item["store"].lower())

    if focus_store:
        focus_summary_payload = _build_focus_store_summary(
            results,
            focus_store,
            resolved_window_name,
            start_dt,
            end_dt,
            settings.local_timezone,
        )
        focus_summary_markdown = _build_focus_store_markdown(focus_summary_payload)
    else:
        focus_summary_payload = _build_no_focus_summary(results, resolved_window_name, start_dt, end_dt)
        focus_summary_markdown = ""
    state.focus_store_summary = focus_summary_payload

    summary_payload = {
        "windowName": resolved_window_name,
        "windowStart": start_dt.isoformat(),
        "windowEnd": end_dt.isoformat(),
        "storeCount": len(results),
        "failureCount": len(failures),
        "requestedFocusStore": focus_store,
        "focusStoreFound": focus_summary_payload.get("focusStoreFound", False),
        "failures": failures,
        "focusStoreSummary": focus_summary_payload,
        "stores": results,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")

    if focus_store:
        (run_dir / "focus_store_summary.json").write_text(json.dumps(focus_summary_payload, indent=2), encoding="utf-8")
        (run_dir / "focus_store_summary.md").write_text(focus_summary_markdown, encoding="utf-8")

        stable_focus_json = REPO_ROOT / settings.output_dir / "focus_store_summary.json"
        stable_focus_markdown = REPO_ROOT / settings.output_dir / "focus_store_summary.md"
        stable_focus_json.write_text(json.dumps(focus_summary_payload, indent=2), encoding="utf-8")
        stable_focus_markdown.write_text(focus_summary_markdown, encoding="utf-8")

    csv_path = run_dir / "summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "store",
                "merchantId",
                "orders",
                "units",
                "fulfilled",
                "uph",
                "inf",
                "found",
                "cancelled",
                "lates",
                "time_available",
            ],
        )
        writer.writeheader()
        for result in results:
            display = result["displayMetrics"]
            writer.writerow(
                {
                    "store": result["store"],
                    "merchantId": result["merchantId"],
                    "orders": display["orders"],
                    "units": display["units"],
                    "fulfilled": display["fulfilled"],
                    "uph": display["uph"],
                    "inf": display["inf"],
                    "found": display["found"],
                    "cancelled": display["cancelled"],
                    "lates": display["lates"],
                    "time_available": display["time_available"],
                }
            )

    elapsed_seconds = time.monotonic() - started_at
    state.finish_run()
    if state.job_status == "running":
        if state.run_failures:
            state.set_job_status("completed_with_failures", f"{len(state.run_failures)} terminal failure(s)")
        else:
            state.set_job_status("completed", "Run completed successfully")
    await post_job_summary(state, settings, elapsed_seconds)
    write_runtime_reports(state, settings)
    app_logger.info(
        "Manual extract complete in %.2fs. Success=%s Failure=%s Output=%s",
        elapsed_seconds,
        len(results),
        len(failures),
        run_dir,
    )
    if not focus_store:
        app_logger.info("No focus store comparison requested.")
    elif focus_summary_payload.get("focusStoreFound"):
        app_logger.info(
            "Focus store summary ready for %s against %s stores.",
            focus_summary_payload.get("matchedStore"),
            focus_summary_payload.get("storeCount"),
        )
    else:
        app_logger.warning("Focus store %s was not found in the extracted store set.", focus_store)

    print(f"Output: {run_dir}")
    print(f"Window: {start_dt.isoformat()} -> {end_dt.isoformat()}")
    print(f"Stores extracted: {len(results)}")


def main():
    args = _parse_args()
    asyncio.run(run_extraction(args))


if __name__ == "__main__":
    main()
