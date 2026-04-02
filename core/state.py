import asyncio
import csv
import json
import os
from datetime import datetime

from core.config import DISCOVERY_CACHE_FILE, FAST_PATH_MAX_CONCURRENCY, LOCAL_TIMEZONE
from core.logger import app_logger


def _attach_local_timezone(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    if hasattr(LOCAL_TIMEZONE, "localize"):
        return LOCAL_TIMEZONE.localize(value)
    return value.replace(tzinfo=LOCAL_TIMEZONE)


class CacheManager:
    def __init__(self):
        self.api_url_template = None
        self.merchant_id_cache = {}
        self.live_dropdown_store_names: list[str] = []
        self.last_updated_at = None
        self.lock = asyncio.Lock()

    def load(self):
        if os.path.exists(DISCOVERY_CACHE_FILE):
            try:
                with open(DISCOVERY_CACHE_FILE) as f:
                    data = json.load(f)
                    self.api_url_template = data.get("template")
                    self.merchant_id_cache.update(data.get("merchant_ids", {}))
                    self.live_dropdown_store_names = sorted(data.get("live_dropdown_store_names", []))
                    last_updated = data.get("last_updated")
                    if last_updated:
                        parsed_last_updated = datetime.fromisoformat(last_updated)
                        self.last_updated_at = _attach_local_timezone(parsed_last_updated)
                    else:
                        self.last_updated_at = None
                    app_logger.info(f"Loaded {len(self.merchant_id_cache)} discovered IDs from cache.")
            except Exception as e:
                app_logger.warning(f"Failed to load discovery cache: {e}")
                self.last_updated_at = None

    async def save(self):
        async with self.lock:
            self.last_updated_at = datetime.now(LOCAL_TIMEZONE)
            data = {
                "template": self.api_url_template,
                "merchant_ids": self.merchant_id_cache,
                "live_dropdown_store_names": self.live_dropdown_store_names,
                "last_updated": self.last_updated_at.isoformat(),
            }
            try:
                os.makedirs(os.path.dirname(DISCOVERY_CACHE_FILE), exist_ok=True)
                with open(DISCOVERY_CACHE_FILE, "w") as f:
                    json.dump(data, f, indent=4)
                app_logger.info("Discovery cache saved.")
            except Exception as e:
                app_logger.warning(f"Failed to save discovery cache: {e}")

    def update_csv_with_cache(self):
        if not os.path.exists("urls.csv"):
            return
        try:
            updated_rows = []
            with open("urls.csv") as f:
                reader = csv.reader(f)
                header = next(reader)
                updated_rows.append(header)
                for row in reader:
                    if not row:
                        continue
                    store_name = row[2].strip() if len(row) > 2 else row[0].strip()
                    if not row[0].strip() and store_name in self.merchant_id_cache:
                        row[0] = self.merchant_id_cache[store_name]
                        app_logger.info(f"Filling missing merchant_id in CSV for: {store_name}")
                    updated_rows.append(row)

            with open("urls.csv", "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerows(updated_rows)
            app_logger.info("urls.csv has been updated with newly discovered IDs.")
        except Exception as e:
            app_logger.warning(f"Failed to update urls.csv: {e}")


class ScraperState:
    def __init__(self):
        self.run_started_at = datetime.now(LOCAL_TIMEZONE)
        self.run_finished_at = None
        self.job_trigger = os.getenv("GITHUB_EVENT_NAME", "local")
        self.job_status = "running"
        self.job_status_detail = ""
        self.fatal_error_message = ""
        self.auth_state_status = "unknown"
        self.cache_template_available_at_start = False
        self.cache_merchant_ids_at_start = 0
        self.browser_worker_pool_size = 0
        self.form_submitter_count = 0
        self.live_dropdown_store_count = 0
        self.live_dropdown_matched_configured_count = 0
        self.live_dropdown_live_only_count = 0
        self.live_dropdown_live_only_store_names: list[str] = []
        self.live_dropdown_skipped_configured_count = 0
        self.live_dropdown_discovery_attempt = ""
        self.previous_live_dropdown_store_names: list[str] = []
        self.current_live_dropdown_store_names: list[str] = []
        self.live_dropdown_new_stores: list[str] = []
        self.live_dropdown_missing_stores: list[str] = []
        self.live_dropdown_refresh_mode = ""
        self.live_dropdown_refresh_reason = ""
        self.job_summary_posted = False
        self.fast_path_eligible_at_start = 0
        self.ui_routed_at_start = 0
        self.requeued_from_fast_path = 0

        self.progress = {"current": 0, "total": 0, "lastUpdate": "N/A"}
        self.progress_lock = asyncio.Lock()

        self.run_failures = []
        self.failure_events: list[dict[str, object]] = []
        self.failure_timestamps = []
        self.failure_lock = asyncio.Lock()

        self.metrics = {
            "collection_times": [],
            "path_collection_times": {
                "fast_path": [],
                "ui": [],
            },
            "submission_times": [],
            "retries": 0,
            "total_orders": 0,
            "total_units": 0,
            "retry_stores": set(),
        }
        self.metrics_lock = asyncio.Lock()

        self.pending_chat_entries: list[dict[str, str]] = []
        self.pending_chat_lock = asyncio.Lock()
        self.chat_batch_count = 0

        self.concurrency_limit = 0
        self.active_workers_count = 0
        self.concurrency_condition = asyncio.Condition()
        self.last_concurrency_change = 0.0
        self.fast_path_semaphore = asyncio.Semaphore(FAST_PATH_MAX_CONCURRENCY)
        self.fast_path_lock = asyncio.Lock()
        self.fast_path_backoff_lock = asyncio.Lock()
        self.fast_path_started_count = 0
        self.fast_path_backoff_until = 0.0

        self.cache = CacheManager()

    def finish_run(self):
        self.run_finished_at = datetime.now(LOCAL_TIMEZONE)

    def set_job_status(self, status: str, detail: str = ""):
        self.job_status = status
        self.job_status_detail = detail

    async def init_progress(self, total: int):
        async with self.progress_lock:
            self.progress["total"] = total
            self.progress["current"] = 0
            self.progress["lastUpdate"] = "N/A"

    async def increment_progress(self):
        async with self.progress_lock:
            self.progress["current"] += 1
            self.progress["lastUpdate"] = datetime.now(LOCAL_TIMEZONE).strftime("%H:%M:%S")

    async def record_issue(self, failure_msg: str, timestamp: float, category: str = "general"):
        async with self.failure_lock:
            self.failure_events.append(
                {
                    "message": failure_msg,
                    "category": category,
                    "terminal": False,
                    "timestamp": datetime.now(LOCAL_TIMEZONE).isoformat(),
                }
            )

    async def add_failure(self, failure_msg: str, timestamp: float, category: str = "general"):
        async with self.failure_lock:
            self.run_failures.append(failure_msg)
            self.failure_timestamps.append(timestamp)
            self.failure_events.append(
                {
                    "message": failure_msg,
                    "category": category,
                    "terminal": True,
                    "timestamp": datetime.now(LOCAL_TIMEZONE).isoformat(),
                }
            )

    async def record_metric(self, store_name: str, duration: float, orders: int, units: int, path: str = "ui"):
        async with self.metrics_lock:
            self.metrics["collection_times"].append((store_name, duration))
            self.metrics["path_collection_times"].setdefault(path, []).append((store_name, duration))
            self.metrics["total_orders"] += orders
            self.metrics["total_units"] += units

    async def record_submission_time(self, store_name: str, duration: float):
        async with self.metrics_lock:
            self.metrics["submission_times"].append((store_name, duration))

    async def record_retry(self, store_name: str):
        async with self.metrics_lock:
            self.metrics["retries"] += 1
            self.metrics["retry_stores"].add(store_name)

    async def record_fast_path_requeue(self):
        async with self.metrics_lock:
            self.requeued_from_fast_path += 1
