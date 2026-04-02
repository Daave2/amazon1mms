from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

from core.config import Settings, load_settings
from core.logger import app_logger
from core.utils import atomic_write_json, atomic_write_text

DISCOVERY_CACHE_FILE = load_settings().discovery_cache_file


def _attach_local_timezone(value: datetime, settings: Settings) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=settings.local_timezone)


@dataclass
class FailureEvent:
    message: str
    category: str = "general"
    terminal: bool = False
    timestamp: str = ""

    def __getitem__(self, key: str):
        return getattr(self, key)


@dataclass
class ProgressState:
    current: int = 0
    total: int = 0
    last_update: str = "N/A"

    def __getitem__(self, key: str):
        key_map = {"lastUpdate": "last_update"}
        return getattr(self, key_map.get(key, key))

    def __setitem__(self, key: str, value):
        key_map = {"lastUpdate": "last_update"}
        setattr(self, key_map.get(key, key), value)


@dataclass
class MetricsState:
    collection_times: list[tuple[str, float]] = field(default_factory=list)
    path_collection_times: dict[str, list[tuple[str, float]]] = field(
        default_factory=lambda: {"fast_path": [], "ui": []}
    )
    submission_times: list[tuple[str, float]] = field(default_factory=list)
    retries: int = 0
    total_orders: int = 0
    total_units: int = 0
    retry_stores: set[str] = field(default_factory=set)

    def __getitem__(self, key: str):
        return getattr(self, key)

    def __setitem__(self, key: str, value):
        setattr(self, key, value)


@dataclass
class DiscoveryState:
    live_dropdown_store_count: int = 0
    live_dropdown_matched_configured_count: int = 0
    live_dropdown_live_only_count: int = 0
    live_dropdown_live_only_store_names: list[str] = field(default_factory=list)
    live_dropdown_skipped_configured_count: int = 0
    live_dropdown_discovery_attempt: str = ""
    previous_live_dropdown_store_names: list[str] = field(default_factory=list)
    current_live_dropdown_store_names: list[str] = field(default_factory=list)
    live_dropdown_new_stores: list[str] = field(default_factory=list)
    live_dropdown_missing_stores: list[str] = field(default_factory=list)
    live_dropdown_refresh_mode: str = ""
    live_dropdown_refresh_reason: str = ""


@dataclass
class RoutingState:
    fast_path_eligible_at_start: int = 0
    ui_routed_at_start: int = 0
    requeued_from_fast_path: int = 0


@dataclass
class SubmissionState:
    queued: int = 0
    sent: int = 0
    replayed: int = 0
    retryable_failures: int = 0
    terminal_failures: int = 0


class CacheManager:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self._discovery_cache_file = self.settings.discovery_cache_file if settings is not None else DISCOVERY_CACHE_FILE
        self.api_url_template: str | None = None
        self.merchant_id_cache: dict[str, str] = {}
        self.live_dropdown_store_names: list[str] = []
        self.last_updated_at: datetime | None = None
        self.lock = asyncio.Lock()
        self._dirty = False
        self._csv_backfill_pending = False

    def load(self):
        if not os.path.exists(self._discovery_cache_file):
            return

        try:
            with open(self._discovery_cache_file, encoding="utf-8") as file_handle:
                data = json.load(file_handle)
        except Exception as exc:
            app_logger.warning(f"Failed to load discovery cache: {exc}")
            self.last_updated_at = None
            return

        self.api_url_template = data.get("template")
        self.merchant_id_cache = dict(data.get("merchant_ids", {}))
        self.live_dropdown_store_names = sorted(data.get("live_dropdown_store_names", []))
        last_updated = data.get("last_updated")
        if last_updated:
            try:
                self.last_updated_at = _attach_local_timezone(datetime.fromisoformat(last_updated), self.settings)
            except ValueError:
                self.last_updated_at = None
        else:
            self.last_updated_at = None

        app_logger.info(f"Loaded {len(self.merchant_id_cache)} discovered IDs from cache.")

    def set_api_url_template(self, template: str) -> bool:
        if not template or self.api_url_template == template:
            return False

        self.api_url_template = template
        self._dirty = True
        return True

    def set_merchant_id(self, store_name: str, merchant_id: str) -> bool:
        if not merchant_id or self.merchant_id_cache.get(store_name) == merchant_id:
            return False

        self.merchant_id_cache[store_name] = merchant_id
        self._dirty = True
        self._csv_backfill_pending = True
        return True

    def set_live_dropdown_store_names(self, store_names: list[str]) -> bool:
        normalized_names = sorted(set(store_names))
        if self.live_dropdown_store_names == normalized_names:
            return False

        self.live_dropdown_store_names = normalized_names
        self._dirty = True
        return True

    async def save(self):
        async with self.lock:
            if not self._dirty and self.last_updated_at is not None:
                return

            self.last_updated_at = datetime.now(self.settings.local_timezone)
            payload = {
                "template": self.api_url_template,
                "merchant_ids": self.merchant_id_cache,
                "live_dropdown_store_names": self.live_dropdown_store_names,
                "last_updated": self.last_updated_at.isoformat(),
            }
            try:
                atomic_write_json(self._discovery_cache_file, payload, indent=4)
                self._dirty = False
                app_logger.info("Discovery cache saved.")
            except Exception as exc:
                app_logger.warning(f"Failed to save discovery cache: {exc}")

    def update_csv_with_cache(self, csv_path: str = "urls.csv") -> bool:
        if not os.path.exists(csv_path) or not self.merchant_id_cache:
            return False

        try:
            updated_rows: list[list[str]] = []
            changed = False
            with open(csv_path, newline="", encoding="utf-8") as file_handle:
                reader = csv.reader(file_handle)
                header = next(reader)
                updated_rows.append(header)
                for row in reader:
                    if not row:
                        continue

                    mutable_row = list(row)
                    while len(mutable_row) < 4:
                        mutable_row.append("")

                    store_name = mutable_row[2].strip() if len(mutable_row) > 2 else mutable_row[0].strip()
                    if not mutable_row[0].strip() and store_name in self.merchant_id_cache:
                        mutable_row[0] = self.merchant_id_cache[store_name]
                        changed = True
                        app_logger.info(f"Filling missing merchant_id in CSV for: {store_name}")
                    updated_rows.append(mutable_row)

            if not changed:
                self._csv_backfill_pending = False
                return False

            csv_buffer = io.StringIO()
            writer = csv.writer(csv_buffer, lineterminator="\n")
            writer.writerows(updated_rows)
            atomic_write_text(csv_path, csv_buffer.getvalue(), encoding="utf-8")
            self._csv_backfill_pending = False
            app_logger.info("urls.csv has been updated with newly discovered IDs.")
            return True
        except Exception as exc:
            app_logger.warning(f"Failed to update urls.csv: {exc}")
            return False


class ScraperState:
    def __init__(self, settings: Settings | None = None, run_id: str | None = None):
        self.settings = settings or load_settings()
        settings = self.settings
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.run_started_at = datetime.now(settings.local_timezone)
        self.run_finished_at: datetime | None = None
        self.job_trigger = os.getenv("GITHUB_EVENT_NAME", "local")
        self.job_status = "running"
        self.job_status_detail = ""
        self.fatal_error_message = ""
        self.auth_state_status = "unknown"
        self.cache_template_available_at_start = False
        self.cache_merchant_ids_at_start = 0
        self.browser_worker_pool_size = 0
        self.form_submitter_count = 0
        self.job_summary_posted = False

        self.progress = ProgressState()
        self.discovery = DiscoveryState()
        self.routing = RoutingState()
        self.submissions = SubmissionState()

        self.run_failures: list[str] = []
        self.failure_events: list[FailureEvent] = []
        self.failure_timestamps: list[float] = []

        self.metrics = MetricsState()
        self.cache = CacheManager(settings)

        self.progress_lock = asyncio.Lock()
        self.failure_lock = asyncio.Lock()
        self.metrics_lock = asyncio.Lock()

        self.chat_batch_count = 0

        self.concurrency_limit = 0
        self.active_workers_count = 0
        self.concurrency_condition = asyncio.Condition()
        self.last_concurrency_change = 0.0
        self.fast_path_semaphore = asyncio.Semaphore(settings.fast_path_max_concurrency)
        self.fast_path_lock = asyncio.Lock()
        self.fast_path_backoff_lock = asyncio.Lock()
        self.fast_path_started_count = 0
        self.fast_path_backoff_until = 0.0

    def finish_run(self):
        self.run_finished_at = datetime.now(self.settings.local_timezone)

    def set_job_status(self, status: str, detail: str = ""):
        self.job_status = status
        self.job_status_detail = detail

    async def init_progress(self, total: int):
        async with self.progress_lock:
            self.progress.total = total
            self.progress.current = 0
            self.progress.last_update = "N/A"

    async def increment_progress(self):
        async with self.progress_lock:
            self.progress.current += 1
            self.progress.last_update = datetime.now(self.settings.local_timezone).strftime("%H:%M:%S")

    async def record_issue(self, failure_msg: str, timestamp: float, category: str = "general"):
        async with self.failure_lock:
            self.failure_events.append(
                FailureEvent(
                    message=failure_msg,
                    category=category,
                    terminal=False,
                    timestamp=datetime.now(self.settings.local_timezone).isoformat(),
                )
            )

    async def add_failure(self, failure_msg: str, timestamp: float, category: str = "general"):
        async with self.failure_lock:
            self.run_failures.append(failure_msg)
            self.failure_timestamps.append(timestamp)
            self.failure_events.append(
                FailureEvent(
                    message=failure_msg,
                    category=category,
                    terminal=True,
                    timestamp=datetime.now(self.settings.local_timezone).isoformat(),
                )
            )

    async def record_metric(self, store_name: str, duration: float, orders: int, units: int, path: str = "ui"):
        async with self.metrics_lock:
            self.metrics.collection_times.append((store_name, duration))
            self.metrics.path_collection_times.setdefault(path, []).append((store_name, duration))
            self.metrics.total_orders += orders
            self.metrics.total_units += units

    async def record_submission_time(self, store_name: str, duration: float):
        async with self.metrics_lock:
            self.metrics.submission_times.append((store_name, duration))

    async def record_retry(self, store_name: str):
        async with self.metrics_lock:
            self.metrics.retries += 1
            self.metrics.retry_stores.add(store_name)

    async def record_fast_path_requeue(self):
        async with self.metrics_lock:
            self.routing.requeued_from_fast_path += 1

    def failure_event_payload(self) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for event in self.failure_events:
            if isinstance(event, FailureEvent):
                payload.append(asdict(event))
            else:
                payload.append(dict(event))
        return payload

    @property
    def previous_live_dropdown_store_names(self) -> list[str]:
        return self.discovery.previous_live_dropdown_store_names

    @previous_live_dropdown_store_names.setter
    def previous_live_dropdown_store_names(self, value: list[str]):
        self.discovery.previous_live_dropdown_store_names = value

    @property
    def current_live_dropdown_store_names(self) -> list[str]:
        return self.discovery.current_live_dropdown_store_names

    @current_live_dropdown_store_names.setter
    def current_live_dropdown_store_names(self, value: list[str]):
        self.discovery.current_live_dropdown_store_names = value

    @property
    def live_dropdown_store_count(self) -> int:
        return self.discovery.live_dropdown_store_count

    @live_dropdown_store_count.setter
    def live_dropdown_store_count(self, value: int):
        self.discovery.live_dropdown_store_count = value

    @property
    def live_dropdown_matched_configured_count(self) -> int:
        return self.discovery.live_dropdown_matched_configured_count

    @live_dropdown_matched_configured_count.setter
    def live_dropdown_matched_configured_count(self, value: int):
        self.discovery.live_dropdown_matched_configured_count = value

    @property
    def live_dropdown_live_only_count(self) -> int:
        return self.discovery.live_dropdown_live_only_count

    @live_dropdown_live_only_count.setter
    def live_dropdown_live_only_count(self, value: int):
        self.discovery.live_dropdown_live_only_count = value

    @property
    def live_dropdown_live_only_store_names(self) -> list[str]:
        return self.discovery.live_dropdown_live_only_store_names

    @live_dropdown_live_only_store_names.setter
    def live_dropdown_live_only_store_names(self, value: list[str]):
        self.discovery.live_dropdown_live_only_store_names = value

    @property
    def live_dropdown_skipped_configured_count(self) -> int:
        return self.discovery.live_dropdown_skipped_configured_count

    @live_dropdown_skipped_configured_count.setter
    def live_dropdown_skipped_configured_count(self, value: int):
        self.discovery.live_dropdown_skipped_configured_count = value

    @property
    def live_dropdown_discovery_attempt(self) -> str:
        return self.discovery.live_dropdown_discovery_attempt

    @live_dropdown_discovery_attempt.setter
    def live_dropdown_discovery_attempt(self, value: str):
        self.discovery.live_dropdown_discovery_attempt = value

    @property
    def live_dropdown_new_stores(self) -> list[str]:
        return self.discovery.live_dropdown_new_stores

    @live_dropdown_new_stores.setter
    def live_dropdown_new_stores(self, value: list[str]):
        self.discovery.live_dropdown_new_stores = value

    @property
    def live_dropdown_missing_stores(self) -> list[str]:
        return self.discovery.live_dropdown_missing_stores

    @live_dropdown_missing_stores.setter
    def live_dropdown_missing_stores(self, value: list[str]):
        self.discovery.live_dropdown_missing_stores = value

    @property
    def live_dropdown_refresh_mode(self) -> str:
        return self.discovery.live_dropdown_refresh_mode

    @live_dropdown_refresh_mode.setter
    def live_dropdown_refresh_mode(self, value: str):
        self.discovery.live_dropdown_refresh_mode = value

    @property
    def live_dropdown_refresh_reason(self) -> str:
        return self.discovery.live_dropdown_refresh_reason

    @live_dropdown_refresh_reason.setter
    def live_dropdown_refresh_reason(self, value: str):
        self.discovery.live_dropdown_refresh_reason = value

    @property
    def fast_path_eligible_at_start(self) -> int:
        return self.routing.fast_path_eligible_at_start

    @fast_path_eligible_at_start.setter
    def fast_path_eligible_at_start(self, value: int):
        self.routing.fast_path_eligible_at_start = value

    @property
    def ui_routed_at_start(self) -> int:
        return self.routing.ui_routed_at_start

    @ui_routed_at_start.setter
    def ui_routed_at_start(self, value: int):
        self.routing.ui_routed_at_start = value

    @property
    def requeued_from_fast_path(self) -> int:
        return self.routing.requeued_from_fast_path

    @requeued_from_fast_path.setter
    def requeued_from_fast_path(self, value: int):
        self.routing.requeued_from_fast_path = value
