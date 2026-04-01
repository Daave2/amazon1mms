import asyncio
import csv
import io
import json
import os
import ssl
from datetime import datetime

import aiofiles
import aiohttp
import certifi

from core.config import DEBUG_MODE, FIELD_MAP, FORM_POST_URL, JSON_LOG_FILE, LOCAL_TIMEZONE, LOG_FILE
from core.logger import app_logger
from core.state import ScraperState
from services.chat_service import add_to_pending_chat

# Since writing to log files needs coordination:
log_lock = asyncio.Lock()


async def log_submission(data: dict[str, str], state: ScraperState):
    async with log_lock:
        current_timestamp = datetime.now(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        log_entry = {"timestamp": current_timestamp, **data}
        fieldnames = [
            "timestamp",
            "date",
            "store",
            "orders",
            "units",
            "fulfilled",
            "uph",
            "inf",
            "found",
            "cancelled",
            "lates",
            "field_11",
            "time_available",
        ]
        new_csv = not os.path.exists(LOG_FILE)
        try:
            csv_buffer = io.StringIO()
            writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames, extrasaction="ignore")
            if new_csv:
                writer.writeheader()
            writer.writerow(log_entry)
            async with aiofiles.open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
                await f.write(csv_buffer.getvalue())
        except OSError as e:
            app_logger.error(f"Error writing to CSV log file {LOG_FILE}: {e}")
        try:
            async with aiofiles.open(JSON_LOG_FILE, "a", encoding="utf-8") as f:
                await f.write(json.dumps(log_entry) + "\n")
        except OSError as e:
            app_logger.error(f"Error writing to JSON log file {JSON_LOG_FILE}: {e}")
        await add_to_pending_chat(log_entry, state)


async def http_form_submitter_worker(queue: asyncio.Queue, worker_id: int, state: ScraperState):
    log_prefix = f"[HTTP-Submitter-{worker_id}]"
    app_logger.info(f"{log_prefix} Starting up...")
    timeout = aiohttp.ClientTimeout(total=20)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_context)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        while True:
            form_data = None
            try:
                form_data = await queue.get()
                store_name = form_data.get("store", "Unknown")

                payload = {}
                for key, value in form_data.items():
                    if key in FIELD_MAP:
                        payload[FIELD_MAP[key]] = value

                submit_start = asyncio.get_event_loop().time()
                async with session.post(FORM_POST_URL, data=payload, timeout=10) as resp:
                    if resp.status == 200:
                        await log_submission(form_data, state)
                        app_logger.info(f"{log_prefix} Submitted data for {store_name}")
                        await state.increment_progress()

                        submit_duration = asyncio.get_event_loop().time() - submit_start
                        await state.record_submission_time(store_name, submit_duration)
                    else:
                        error_text = await resp.text()
                        app_logger.error(
                            f"{log_prefix} Submission for {store_name} failed. Status: {resp.status}. Response: {error_text[:200]}"
                        )
                        await state.add_failure(
                            f"{store_name} (HTTP Submit Fail {resp.status})", asyncio.get_event_loop().time()
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                failed_store = form_data.get("store", "Unknown") if form_data else "Unknown"
                app_logger.error(f"{log_prefix} Unhandled exception for {failed_store}: {e}", exc_info=DEBUG_MODE)
                await state.add_failure(f"{failed_store} (Submit Exception)", asyncio.get_event_loop().time())
            finally:
                if form_data:
                    queue.task_done()
    app_logger.info(f"{log_prefix} Shut down.")
