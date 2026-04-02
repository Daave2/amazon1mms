import asyncio
import json
import os
from datetime import datetime

import psutil
from playwright.async_api import Browser, async_playwright

from core.config import (
    ACTION_TIMEOUT,
    AUTO_ENABLED,
    AUTO_MAX_CONCURRENCY,
    AUTO_MIN_CONCURRENCY,
    BASE_DASHBOARD_URL,
    CHECK_INTERVAL,
    COOLDOWN_SECONDS,
    CPU_LOWER_THRESHOLD,
    CPU_UPPER_THRESHOLD,
    DEBUG_MODE,
    INITIAL_CONCURRENCY,
    LOCAL_TIMEZONE,
    MEM_UPPER_THRESHOLD,
    NUM_FORM_SUBMITTERS,
    PAGE_TIMEOUT,
    STORAGE_STATE,
)
from core.logger import app_logger
from core.reporting import write_runtime_reports
from core.state import ScraperState
from core.store_loader import load_stores_from_csv
from core.utils import safe_close
from services.auth_service import check_if_login_needed, prime_master_session
from services.chat_service import flush_pending_chat_entries, post_job_summary
from services.forms_service import http_form_submitter_worker
from services.metrics_service import discover_available_dropdown_stores, process_single_store, resolve_dropdown_name


def load_default_data(state: ScraperState) -> list:
    state.cache.load()
    state.cache_template_available_at_start = bool(state.cache.api_url_template)
    state.cache_merchant_ids_at_start = len(state.cache.merchant_id_cache)

    try:
        urls_data = load_stores_from_csv(
            "urls.csv",
            on_skip=lambda row_number, _row: app_logger.warning(
                f"Skipping row {row_number} in urls.csv: no store name found"
            ),
        )
        app_logger.info(f"{len(urls_data)} stores loaded from urls.csv")
    except FileNotFoundError:
        app_logger.error("FATAL: 'urls.csv' not found.")
        raise
    except Exception:
        app_logger.exception("An error occurred while loading urls.csv")
        raise

    return urls_data


def ensure_storage_state():
    if not os.path.exists(STORAGE_STATE) or os.path.getsize(STORAGE_STATE) == 0:
        return False
    try:
        with open(STORAGE_STATE) as f:
            data = json.load(f)
        if (
            not isinstance(data, dict)
            or "cookies" not in data
            or not isinstance(data["cookies"], list)
            or not data["cookies"]
        ):
            return False
        return True
    except json.JSONDecodeError:
        return False


def filter_stores_to_live_dropdown(
    urls_data: list[dict[str, str]],
    available_stores: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    available_by_name: dict[str, dict[str, str]] = {}
    for available_store in available_stores:
        normalized_name = available_store.get("normalized_name", "").strip()
        if normalized_name and normalized_name not in available_by_name:
            available_by_name[normalized_name] = available_store

    filtered_stores: list[dict[str, str]] = []
    skipped_stores: list[dict[str, str]] = []

    for store in urls_data:
        normalized_name = resolve_dropdown_name(store["store_name"])
        matched_store = available_by_name.get(normalized_name)
        if not matched_store:
            skipped_stores.append(store)
            continue

        filtered_store = dict(store)
        merchant_id = matched_store.get("merchant_id", "").strip()
        if merchant_id and not filtered_store.get("merchant_id"):
            filtered_store["merchant_id"] = merchant_id
        filtered_stores.append(filtered_store)

    return filtered_stores, skipped_stores


async def load_live_dropdown_stores(
    browser: Browser,
    storage_template: dict,
    urls_data: list[dict[str, str]],
    state: ScraperState,
) -> list[dict[str, str]]:
    context = None
    page = None
    try:
        app_logger.info("Discovering live store list from the dashboard dropdown before queueing stores.")
        context = await browser.new_context(storage_state=storage_template)
        context.set_default_navigation_timeout(PAGE_TIMEOUT)
        context.set_default_timeout(ACTION_TIMEOUT)
        page = await context.new_page()
        await page.goto(BASE_DASHBOARD_URL, timeout=PAGE_TIMEOUT, wait_until="networkidle")

        available_stores = await discover_available_dropdown_stores(page)
        filtered_stores, skipped_stores = filter_stores_to_live_dropdown(urls_data, available_stores)

        cache_updated = False
        for store in filtered_stores:
            merchant_id = store.get("merchant_id", "").strip()
            if merchant_id and state.cache.merchant_id_cache.get(store["store_name"]) != merchant_id:
                state.cache.merchant_id_cache[store["store_name"]] = merchant_id
                cache_updated = True

        if cache_updated:
            await state.cache.save()

        app_logger.info(
            f"Live dropdown filtering kept {len(filtered_stores)} of {len(urls_data)} configured store(s)."
        )
        if skipped_stores:
            skipped_names = ", ".join(store["store_name"] for store in skipped_stores[:10])
            suffix = "..." if len(skipped_stores) > 10 else ""
            app_logger.warning(
                f"Skipping {len(skipped_stores)} configured store(s) not currently listed in the live dropdown: "
                f"{skipped_names}{suffix}"
            )

        return filtered_stores
    except Exception:
        app_logger.exception("Failed to discover and filter stores from the live dashboard dropdown.")
        raise
    finally:
        await safe_close(page, "Live dropdown discovery page", state.record_issue)
        await safe_close(context, "Live dropdown discovery context", state.record_issue)


async def auto_concurrency_manager(state: ScraperState):
    if not AUTO_ENABLED:
        return
    app_logger.info(f"Auto-concurrency enabled with range {AUTO_MIN_CONCURRENCY}-{AUTO_MAX_CONCURRENCY}")
    while True:
        now = asyncio.get_running_loop().time()

        async with state.failure_lock:
            while state.failure_timestamps and now - state.failure_timestamps[0] > 60:
                state.failure_timestamps.pop(0)
            recent_failure_count = len(state.failure_timestamps)

        estimated_throughput = state.concurrency_limit * 30
        failure_rate = recent_failure_count / max(estimated_throughput, 1)

        if failure_rate > 0.05:
            if now - state.last_concurrency_change >= COOLDOWN_SECONDS:
                state.concurrency_limit = max(AUTO_MIN_CONCURRENCY, int(state.concurrency_limit * 0.5))
                state.last_concurrency_change = now
                app_logger.warning(
                    f"Auto-concurrency: THROTTLING DOWN to {state.concurrency_limit} due to high failure rate ({failure_rate:.1%})"
                )
                async with state.concurrency_condition:
                    state.concurrency_condition.notify_all()
                await asyncio.sleep(COOLDOWN_SECONDS * 2)
                continue

        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        if now - state.last_concurrency_change >= COOLDOWN_SECONDS:
            if (
                cpu > CPU_UPPER_THRESHOLD or mem > MEM_UPPER_THRESHOLD
            ) and state.concurrency_limit > AUTO_MIN_CONCURRENCY:
                state.concurrency_limit -= 1
                state.last_concurrency_change = now
                app_logger.info(
                    f"Auto-concurrency: decreased to {state.concurrency_limit} (CPU {cpu:.1f}%, MEM {mem:.1f}%)"
                )
            elif (
                cpu < CPU_LOWER_THRESHOLD
                and mem < MEM_UPPER_THRESHOLD
                and state.concurrency_limit < AUTO_MAX_CONCURRENCY
            ):
                state.concurrency_limit += 1
                state.last_concurrency_change = now
                app_logger.info(
                    f"Auto-concurrency: increased to {state.concurrency_limit} (CPU {cpu:.1f}%, MEM {mem:.1f}%)"
                )

            if state.concurrency_limit > AUTO_MAX_CONCURRENCY:
                state.concurrency_limit = AUTO_MAX_CONCURRENCY
            if state.concurrency_limit < AUTO_MIN_CONCURRENCY:
                state.concurrency_limit = AUTO_MIN_CONCURRENCY

            async with state.concurrency_condition:
                state.concurrency_condition.notify_all()
        await asyncio.sleep(CHECK_INTERVAL)


async def worker_task(
    worker_id: int,
    browser: Browser,
    storage_template: dict,
    job_queue: asyncio.Queue,
    submission_queue: asyncio.Queue,
    state: ScraperState,
):
    app_logger.info(f"[Worker-{worker_id}] Starting up.")
    context = None
    page = None
    try:
        context = await browser.new_context(storage_state=storage_template)
        context.set_default_navigation_timeout(PAGE_TIMEOUT)
        context.set_default_timeout(ACTION_TIMEOUT)
        page = await context.new_page()

        while True:
            try:
                store_item = job_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            async with state.concurrency_condition:
                while state.active_workers_count >= state.concurrency_limit:
                    await state.concurrency_condition.wait()
                state.active_workers_count += 1

            try:
                await process_single_store(page, store_item, submission_queue, state)
            except Exception as exc:
                store_name = store_item.get("store_name", f"Worker-{worker_id}")
                app_logger.exception(f"[Worker-{worker_id}] Unhandled exception while processing {store_name}: {exc}")
                await state.add_failure(
                    f"{store_name} (Worker Exception)",
                    asyncio.get_running_loop().time(),
                    category="worker",
                )
            finally:
                async with state.concurrency_condition:
                    state.active_workers_count -= 1
                    state.concurrency_condition.notify_all()
                job_queue.task_done()

    except Exception as e:
        app_logger.error(f"[Worker-{worker_id}] Crashed during setup: {e}")
        await state.add_failure(
            f"Worker-{worker_id} (Worker Setup Exception)",
            asyncio.get_running_loop().time(),
            category="worker",
        )
    finally:
        await safe_close(page, f"Worker-{worker_id} page", state.record_issue)
        await safe_close(context, f"Worker-{worker_id} context", state.record_issue)
        app_logger.info(f"[Worker-{worker_id}] Shutting down.")


async def process_urls(browser: Browser, state: ScraperState):
    pool_size = INITIAL_CONCURRENCY
    state.browser_worker_pool_size = pool_size
    state.form_submitter_count = NUM_FORM_SUBMITTERS
    state.concurrency_limit = pool_size
    app_logger.info(f"Job 'process_urls' started with Worker Pool size: {pool_size}")

    urls_data = load_default_data(state)
    if not urls_data:
        app_logger.error("No URLs to process. Aborting job.")
        state.set_job_status("aborted_no_stores", "No usable stores were found in urls.csv")
        return

    login_is_required = True
    if ensure_storage_state():
        app_logger.info("Existing auth state file found. Verifying session is still active...")
        temp_context = None
        try:
            with open(STORAGE_STATE) as f:
                storage_for_check = json.load(f)
            temp_context = await browser.new_context(storage_state=storage_for_check)
            temp_page = await temp_context.new_page()
            if not await check_if_login_needed(temp_page, BASE_DASHBOARD_URL):
                app_logger.info("Session verification successful. Skipping login.")
                login_is_required = False
                state.auth_state_status = "reused"
            else:
                app_logger.warning("Session has expired or is invalid. A new login is required.")
                state.auth_state_status = "refresh_required"
        except Exception as e:
            app_logger.error(f"An error occurred during session verification: {e}", exc_info=DEBUG_MODE)
            state.auth_state_status = "refresh_required"
        finally:
            await safe_close(temp_context, "Session verification context", state.record_issue)
    else:
        app_logger.info("No existing auth state file found. Login is required.")
        state.auth_state_status = "missing"

    if login_is_required:
        MAX_LOGIN_ATTEMPTS = 3
        login_successful = False
        for attempt in range(MAX_LOGIN_ATTEMPTS):
            app_logger.info(f"Attempting to prime a new master session (Attempt {attempt + 1}/{MAX_LOGIN_ATTEMPTS})...")
            if await prime_master_session(browser):
                login_successful = True
                break
            if attempt < MAX_LOGIN_ATTEMPTS - 1:
                await asyncio.sleep(5)

        if not login_successful:
            app_logger.critical(f"Critical: Session priming failed after {MAX_LOGIN_ATTEMPTS} attempts. Aborting job.")
            state.auth_state_status = "refresh_failed"
            state.set_job_status("login_aborted", f"Session priming failed after {MAX_LOGIN_ATTEMPTS} attempts")
            return
        state.auth_state_status = "refreshed"

    with open(STORAGE_STATE) as f:
        storage_template = json.load(f)

    urls_data = await load_live_dropdown_stores(browser, storage_template, urls_data, state)
    if not urls_data:
        app_logger.error("No configured stores are currently listed in the live dropdown. Aborting job.")
        state.set_job_status("aborted_no_stores", "No configured stores were present in the live dropdown")
        return

    job_queue = asyncio.Queue()
    submission_queue = asyncio.Queue()

    for store in urls_data:
        job_queue.put_nowait(store)

    await state.init_progress(len(urls_data))
    start_time = datetime.now(LOCAL_TIMEZONE)

    app_logger.info(f"Starting {NUM_FORM_SUBMITTERS} HTTP form submitter worker(s).")
    form_submitter_tasks = [
        asyncio.create_task(http_form_submitter_worker(submission_queue, i + 1, state))
        for i in range(NUM_FORM_SUBMITTERS)
    ]

    app_logger.info(f"Spinning up {pool_size} browser workers...")
    workers = [
        asyncio.create_task(worker_task(i + 1, browser, storage_template, job_queue, submission_queue, state))
        for i in range(pool_size)
    ]

    # Auto-concurrency task
    auto_task = asyncio.create_task(auto_concurrency_manager(state))

    await asyncio.gather(*workers)

    app_logger.info("All workers finished. Waiting for submission queue to empty...")
    await submission_queue.join()
    await flush_pending_chat_entries(state)

    app_logger.info("Cancelling form submitter and auto-concurrency tasks...")
    for task in form_submitter_tasks:
        task.cancel()
    auto_task.cancel()
    await asyncio.gather(*form_submitter_tasks, auto_task, return_exceptions=True)

    elapsed = (datetime.now(LOCAL_TIMEZONE) - start_time).total_seconds()
    app_logger.info(
        f"Processing finished. Processed {state.progress['current']}/{state.progress['total']} in {elapsed:.2f}s"
    )

    await post_job_summary(state, elapsed)
    state.cache.update_csv_with_cache()

    if state.run_failures:
        state.set_job_status("completed_with_failures", f"{len(state.run_failures)} terminal failure(s)")
        app_logger.warning(f"Completed with {len(state.run_failures)} issue(s): {', '.join(state.run_failures)}")
    else:
        state.set_job_status("completed", "Run completed successfully")
        app_logger.info("Completed successfully.")


async def main():
    app_logger.info("Starting up in single-run mode...")
    playwright = None
    browser = None
    state = ScraperState()
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=not DEBUG_MODE,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-accelerated-2d-canvas",
                "--disable-gl-drawing-for-tests",
            ],
        )
        app_logger.info("Browser launched successfully.")
        await process_urls(browser, state)
    except Exception as e:
        state.fatal_error_message = str(e)
        state.set_job_status("fatal", "Unhandled exception in main execution block")
        app_logger.critical(f"A critical error occurred in the main execution block: {e}", exc_info=DEBUG_MODE)
    finally:
        app_logger.info("Task finished. Initiating shutdown...")
        if browser:
            await safe_close(browser, "Browser", state.record_issue)
            app_logger.info("Browser instance closed.")
        if playwright:
            try:
                await playwright.stop()
                app_logger.info("Playwright stopped.")
            except Exception as exc:
                app_logger.warning(f"Failed to stop Playwright cleanly: {exc}")
                await state.record_issue(
                    "Playwright stop (Cleanup failure)",
                    asyncio.get_running_loop().time(),
                    category="cleanup",
                )
        await flush_pending_chat_entries(state)
        if state.job_status == "running":
            default_status = "completed_with_failures" if state.run_failures else "completed"
            default_detail = "Run exited during shutdown with recorded failures" if state.run_failures else "Run completed during shutdown"
            state.set_job_status(default_status, default_detail)
        state.finish_run()
        try:
            write_runtime_reports(state)
            app_logger.info("Runtime reports written.")
        except Exception as exc:
            app_logger.warning(f"Failed to write runtime reports: {exc}")
        app_logger.info("Run complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        app_logger.info("Script interrupted by user. Exiting.")
