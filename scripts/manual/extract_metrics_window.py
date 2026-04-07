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
from core.state import CacheManager, ScraperState  # noqa: E402
from core.store_loader import load_stores_from_csv  # noqa: E402
from core.utils import ensure_directory, optimize_browser_context, safe_close  # noqa: E402
from services.metrics_service import (  # noqa: E402
    _build_fast_path_target_url,
    fetch_metrics_pair_fast_path,
    resolve_dropdown_name,
)


WINDOW_PRESETS = {"today", "yesterday", "this_week", "last_7_days", "custom"}


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


async def run_extraction(args: argparse.Namespace):
    settings = load_settings()
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
    app_logger.info("Discovery cache template available: %s", bool(cache.api_url_template))
    app_logger.info("%s stores selected for extraction.", len(selected_stores))
    app_logger.info("Artifacts will be written to %s", run_dir)

    state = ScraperState(settings=settings)
    browser = None
    context = None
    page = None
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
                raise RuntimeError("Saved auth state is not valid for manual extraction.")
            app_logger.info("Saved auth state is valid for manual extraction.")

            request_client = context.request
            state.browser_worker_pool_size = settings.fast_path_max_concurrency

            async def extract_single_store(index: int, store: dict[str, str]):
                store_name = store["store_name"]
                merchant_id = store["merchant_id"]
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
                    app_logger.exception("[%s/%s] Extraction failed for %s: %s", index, len(selected_stores), store_name, exc)
            await asyncio.gather(
                *(extract_single_store(index, store) for index, store in enumerate(selected_stores, start=1))
            )
    finally:
        await safe_close(page, "Manual extraction page")
        await safe_close(context, "Manual extraction context")
        await safe_close(browser, "Manual extraction browser")

    summary_payload = {
        "windowName": resolved_window_name,
        "windowStart": start_dt.isoformat(),
        "windowEnd": end_dt.isoformat(),
        "storeCount": len(results),
        "failureCount": len(failures),
        "failures": failures,
        "stores": results,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")

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
    app_logger.info(
        "Manual extract complete in %.2fs. Success=%s Failure=%s Output=%s",
        elapsed_seconds,
        len(results),
        len(failures),
        run_dir,
    )

    summary_lines = [
        "## Manual Extract Summary",
        "",
        f"- Window: `{start_dt.isoformat()} -> {end_dt.isoformat()}`",
        f"- Success: `{len(results)}`",
        f"- Failure: `{len(failures)}`",
        f"- Output: `{run_dir}`",
    ]
    github_step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_step_summary:
        Path(github_step_summary).write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    print(f"Output: {run_dir}")
    print(f"Window: {start_dt.isoformat()} -> {end_dt.isoformat()}")
    print(f"Stores extracted: {len(results)}")


def main():
    args = _parse_args()
    asyncio.run(run_extraction(args))


if __name__ == "__main__":
    main()
