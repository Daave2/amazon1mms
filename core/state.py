import asyncio
import csv
import json
import os
from datetime import datetime

from core.config import DISCOVERY_CACHE_FILE, LOCAL_TIMEZONE
from core.logger import app_logger


class CacheManager:
    def __init__(self):
        self.api_url_template = None
        self.merchant_id_cache = {}
        self.lock = asyncio.Lock()

    def load(self):
        if os.path.exists(DISCOVERY_CACHE_FILE):
            try:
                with open(DISCOVERY_CACHE_FILE) as f:
                    data = json.load(f)
                    self.api_url_template = data.get("template")
                    self.merchant_id_cache.update(data.get("merchant_ids", {}))
                    app_logger.info(f"Loaded {len(self.merchant_id_cache)} discovered IDs from cache.")
            except Exception as e:
                app_logger.warning(f"Failed to load discovery cache: {e}")

    async def save(self):
        async with self.lock:
            data = {
                "template": self.api_url_template,
                "merchant_ids": self.merchant_id_cache,
                "last_updated": datetime.now(LOCAL_TIMEZONE).isoformat(),
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
        self.progress = {"current": 0, "total": 0, "lastUpdate": "N/A"}
        self.progress_lock = asyncio.Lock()

        self.run_failures = []
        self.failure_timestamps = []
        self.failure_lock = asyncio.Lock()

        self.metrics = {
            "collection_times": [],
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

        self.cache = CacheManager()

    async def init_progress(self, total: int):
        async with self.progress_lock:
            self.progress["total"] = total
            self.progress["current"] = 0
            self.progress["lastUpdate"] = "N/A"

    async def increment_progress(self):
        async with self.progress_lock:
            self.progress["current"] += 1
            self.progress["lastUpdate"] = datetime.now(LOCAL_TIMEZONE).strftime("%H:%M:%S")

    async def add_failure(self, failure_msg: str, timestamp: float):
        async with self.failure_lock:
            self.run_failures.append(failure_msg)
            self.failure_timestamps.append(timestamp)

    async def record_metric(self, store_name: str, duration: float, orders: int, units: int):
        async with self.metrics_lock:
            self.metrics["collection_times"].append((store_name, duration))
            self.metrics["total_orders"] += orders
            self.metrics["total_units"] += units

    async def record_submission_time(self, store_name: str, duration: float):
        async with self.metrics_lock:
            self.metrics["submission_times"].append((store_name, duration))

    async def record_retry(self, store_name: str):
        async with self.metrics_lock:
            self.metrics["retries"] += 1
            self.metrics["retry_stores"].add(store_name)
