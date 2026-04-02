from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import ssl
from dataclasses import dataclass
from datetime import datetime

import aiohttp
import certifi

from core.config import Settings
from core.forms import LOG_FIELDNAMES, build_form_payload, build_submission_log_entry
from core.logger import app_logger
from core.state import ScraperState
from core.utils import ensure_directory, normalize_name

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class SubmissionTask:
    submission_id: str
    run_id: str
    form_data: dict[str, str]
    replayed: bool = False

    @property
    def store_name(self) -> str:
        return self.form_data.get("store", "Unknown")


def build_submission_id(run_id: str, store_name: str) -> str:
    normalized_store_name = normalize_name(store_name).replace(" ", "_")
    return f"{run_id}:{normalized_store_name}"


def is_retryable_submission_status(status: int) -> bool:
    return status in RETRYABLE_STATUSES


def is_retryable_submission_exception(exc: BaseException) -> bool:
    retryable_types = (
        asyncio.TimeoutError,
        TimeoutError,
        aiohttp.ClientConnectionError,
        aiohttp.ClientError,
    )
    return isinstance(exc, retryable_types)


class SubmissionLedger:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = asyncio.Lock()

    async def append_event(self, event: dict[str, object]):
        async with self.lock:
            ensure_directory(self.settings.output_dir)
            with open(self.settings.submission_events_file, "a", encoding="utf-8") as file_handle:
                file_handle.write(json.dumps(event) + "\n")

    def load_pending_tasks(self) -> list[SubmissionTask]:
        if not os.path.exists(self.settings.submission_events_file):
            return []

        latest_by_submission: dict[str, dict[str, object]] = {}
        with open(self.settings.submission_events_file, encoding="utf-8") as file_handle:
            for line_number, line in enumerate(file_handle, start=1):
                cleaned_line = line.strip()
                if not cleaned_line:
                    continue
                try:
                    event = json.loads(cleaned_line)
                except json.JSONDecodeError as exc:
                    app_logger.warning(
                        f"Skipping malformed submission ledger entry at line {line_number}: {exc}"
                    )
                    continue

                submission_id = str(event.get("submission_id", "")).strip()
                if not submission_id:
                    continue
                latest_by_submission[submission_id] = event

        pending_tasks: list[SubmissionTask] = []
        for event in latest_by_submission.values():
            status = str(event.get("status", ""))
            form_data = event.get("form_data")
            if status not in {"queued", "retryable_failure"} or not isinstance(form_data, dict):
                continue

            pending_tasks.append(
                SubmissionTask(
                    submission_id=str(event["submission_id"]),
                    run_id=str(event.get("run_id", "")),
                    form_data={str(key): str(value) for key, value in form_data.items()},
                    replayed=True,
                )
            )

        pending_tasks.sort(key=lambda task: task.submission_id)
        return pending_tasks


class SubmissionManager:
    def __init__(self, settings: Settings, state: ScraperState, chat_queue: asyncio.Queue | None = None):
        self.settings = settings
        self.state = state
        self.chat_queue = chat_queue
        self.queue: asyncio.Queue[SubmissionTask | None] = asyncio.Queue()
        self.ledger = SubmissionLedger(settings)
        self._log_lock = asyncio.Lock()

    async def enqueue_submission(self, form_data: dict[str, str]) -> SubmissionTask:
        task = SubmissionTask(
            submission_id=build_submission_id(self.state.run_id, form_data.get("store", "Unknown")),
            run_id=self.state.run_id,
            form_data=dict(form_data),
        )
        await self.ledger.append_event(self._build_event(task, status="queued"))
        self.state.submissions.queued += 1
        await self.queue.put(task)
        return task

    async def enqueue_replay_tasks(self, tasks: list[SubmissionTask]):
        self.state.submissions.replayed += len(tasks)
        for task in tasks:
            await self.queue.put(task)

    async def log_submission(self, task: SubmissionTask):
        log_entry = build_submission_log_entry(
            {
                "run_id": task.run_id,
                "submission_id": task.submission_id,
                **task.form_data,
            },
            current_dt=datetime.now(self.settings.local_timezone),
        )

        async with self._log_lock:
            new_csv = not os.path.exists(self.settings.log_file)
            csv_buffer = io.StringIO()
            writer = csv.DictWriter(csv_buffer, fieldnames=LOG_FIELDNAMES, extrasaction="ignore")
            if new_csv:
                writer.writeheader()
            writer.writerow(log_entry)

            ensure_directory(self.settings.output_dir)
            with open(self.settings.log_file, "a", newline="", encoding="utf-8") as file_handle:
                file_handle.write(csv_buffer.getvalue())

            with open(self.settings.json_log_file, "a", encoding="utf-8") as file_handle:
                file_handle.write(json.dumps(log_entry) + "\n")

        if self.chat_queue is not None and self.settings.chat_webhook_url:
            await self.chat_queue.put(log_entry)

    def load_pending_replays(self) -> list[SubmissionTask]:
        return self.ledger.load_pending_tasks()

    def _build_event(
        self,
        task: SubmissionTask,
        *,
        status: str,
        attempt: int = 1,
        http_status: int | None = None,
        error: str = "",
    ) -> dict[str, object]:
        return {
            "recorded_at": datetime.now(self.settings.local_timezone).isoformat(),
            "run_id": task.run_id,
            "submission_id": task.submission_id,
            "store": task.store_name,
            "status": status,
            "attempt": attempt,
            "http_status": http_status,
            "error": error,
            "replayed": task.replayed,
            "form_data": task.form_data,
        }


async def http_form_submitter_worker(manager: SubmissionManager, worker_id: int):
    log_prefix = f"[HTTP-Submitter-{worker_id}]"
    app_logger.info(f"{log_prefix} Starting up...")

    timeout = aiohttp.ClientTimeout(total=20)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_context)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        while True:
            task = await manager.queue.get()
            try:
                if task is None:
                    return

                payload = build_form_payload(task.form_data, manager.settings.field_map)
                submit_start = asyncio.get_running_loop().time()

                try:
                    async with session.post(manager.settings.form_post_url, data=payload, timeout=10) as resp:
                        if resp.status == 200:
                            await manager.ledger.append_event(
                                manager._build_event(task, status="sent", http_status=resp.status)
                            )
                            await manager.log_submission(task)
                            if not task.replayed:
                                await manager.state.increment_progress()
                            manager.state.submissions.sent += 1
                            await manager.state.record_submission_time(
                                task.store_name,
                                asyncio.get_running_loop().time() - submit_start,
                            )
                            app_logger.info(f"{log_prefix} Submitted data for {task.store_name}")
                            continue

                        error_text = await resp.text()
                        if is_retryable_submission_status(resp.status):
                            manager.state.submissions.retryable_failures += 1
                            await manager.ledger.append_event(
                                manager._build_event(
                                    task,
                                    status="retryable_failure",
                                    http_status=resp.status,
                                    error=error_text[:200],
                                )
                            )
                            await manager.state.add_failure(
                                f"{task.store_name} (HTTP Submit Retry Deferred {resp.status})",
                                asyncio.get_running_loop().time(),
                                category="submission",
                            )
                        else:
                            manager.state.submissions.terminal_failures += 1
                            await manager.ledger.append_event(
                                manager._build_event(
                                    task,
                                    status="terminal_failure",
                                    http_status=resp.status,
                                    error=error_text[:200],
                                )
                            )
                            await manager.state.add_failure(
                                f"{task.store_name} (HTTP Submit Fail {resp.status})",
                                asyncio.get_running_loop().time(),
                                category="submission",
                            )
                except Exception as exc:
                    if is_retryable_submission_exception(exc):
                        manager.state.submissions.retryable_failures += 1
                        await manager.ledger.append_event(
                            manager._build_event(task, status="retryable_failure", error=str(exc))
                        )
                        await manager.state.add_failure(
                            f"{task.store_name} (Submit Retry Deferred)",
                            asyncio.get_running_loop().time(),
                            category="submission",
                        )
                    else:
                        manager.state.submissions.terminal_failures += 1
                        await manager.ledger.append_event(
                            manager._build_event(task, status="terminal_failure", error=str(exc))
                        )
                        await manager.state.add_failure(
                            f"{task.store_name} (Submit Exception)",
                            asyncio.get_running_loop().time(),
                            category="submission",
                        )
                        app_logger.error(f"{log_prefix} Unhandled exception for {task.store_name}: {exc}")
            finally:
                manager.queue.task_done()

    app_logger.info(f"{log_prefix} Shut down.")
