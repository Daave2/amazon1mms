import asyncio
import json
import os
from datetime import datetime, timedelta

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
    DROPDOWN_REFRESH_MAX_AGE_DAYS,
    FAST_PATH_MAX_CONCURRENCY,
    FORCE_DROPDOWN_DISCOVERY,
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
from core.utils import optimize_browser_context, safe_close
from core.work_items import WorkItem
from services.auth_service import check_if_login_needed, prime_master_session
from services.chat_service import flush_pending_chat_entries, post_job_summary
from services.forms_service import http_form_submitter_worker
from services.metrics_service import (
    _selection_matches_target,
    discover_available_dropdown_stores,
    process_fast_path_store,
    process_ui_store,
    resolve_dropdown_name,
)


def load_default_data(state: ScraperState) -> list:
    state.cache.load()
    state.cache_template_available_at_start = bool(state.cache.api_url_template)
    state.cache_merchant_ids_at_start = len(state.cache.merchant_id_cache)
    state.previous_live_dropdown_store_names = list(state.cache.live_dropdown_store_names)

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
    indexed_configured_stores = [
        {
            **store,
            "_index": index,
            "_resolved_name": resolve_dropdown_name(store["store_name"]),
        }
        for index, store in enumerate(urls_data)
    ]
    configured_by_merchant: dict[str, list[dict[str, str]]] = {}
    configured_by_name: dict[str, list[dict[str, str]]] = {}

    for configured_store in indexed_configured_stores:
        merchant_id = configured_store.get("merchant_id", "").strip()
        if merchant_id:
            configured_by_merchant.setdefault(merchant_id, []).append(configured_store)
        configured_by_name.setdefault(configured_store["_resolved_name"], []).append(configured_store)

    matched_configured_indices: set[int] = set()
    queue_stores: list[dict[str, str]] = []

    for available_store in available_stores:
        matched_store = _match_configured_store_for_live_option(
            available_store,
            indexed_configured_stores,
            configured_by_merchant,
            configured_by_name,
            matched_configured_indices,
        )
        if matched_store:
            matched_configured_indices.add(matched_store["_index"])

        queue_stores.append(
            {
                "store_name": matched_store["store_name"] if matched_store else available_store["store_name"],
                "dropdown_name": available_store["store_name"],
                "merchant_id": available_store.get("merchant_id", "").strip()
                or (matched_store.get("merchant_id", "").strip() if matched_store else ""),
                "marketplace_id": matched_store.get("marketplace_id", "").strip() if matched_store else "",
                "matched_from_configured": bool(matched_store),
            }
        )

    skipped_stores = [
        {
            key: value
            for key, value in configured_store.items()
            if not key.startswith("_")
        }
        for configured_store in indexed_configured_stores
        if configured_store["_index"] not in matched_configured_indices
    ]

    return queue_stores, skipped_stores


def _match_configured_store_for_live_option(
    available_store: dict[str, str],
    configured_stores: list[dict[str, str]],
    configured_by_merchant: dict[str, list[dict[str, str]]],
    configured_by_name: dict[str, list[dict[str, str]]],
    matched_configured_indices: set[int],
) -> dict[str, str] | None:
    live_name = available_store["store_name"]
    live_normalized_name = available_store.get("normalized_name", "").strip()
    live_merchant_id = available_store.get("merchant_id", "").strip()

    merchant_candidates = [
        candidate
        for candidate in configured_by_merchant.get(live_merchant_id, [])
        if candidate["_index"] not in matched_configured_indices
    ]
    matched_store = _pick_best_configured_candidate(merchant_candidates, live_name, live_normalized_name)
    if matched_store:
        return matched_store

    name_candidates = [
        candidate
        for candidate in configured_by_name.get(live_normalized_name, [])
        if candidate["_index"] not in matched_configured_indices
    ]
    matched_store = _pick_best_configured_candidate(name_candidates, live_name, live_normalized_name)
    if matched_store:
        return matched_store

    fuzzy_candidates = [
        candidate
        for candidate in configured_stores
        if candidate["_index"] not in matched_configured_indices
        and _selection_matches_target(live_name, candidate["_resolved_name"], candidate["store_name"])
    ]
    return _pick_best_configured_candidate(fuzzy_candidates, live_name, live_normalized_name)


def _pick_best_configured_candidate(
    candidates: list[dict[str, str]],
    live_name: str,
    live_normalized_name: str,
) -> dict[str, str] | None:
    if not candidates:
        return None

    exact_name_matches = [candidate for candidate in candidates if candidate["_resolved_name"] == live_normalized_name]
    if exact_name_matches:
        return exact_name_matches[0]

    fuzzy_matches = [
        candidate
        for candidate in candidates
        if _selection_matches_target(live_name, candidate["_resolved_name"], candidate["store_name"])
    ]
    if fuzzy_matches:
        return fuzzy_matches[0]

    if len(candidates) == 1:
        return candidates[0]

    return None


def _attach_local_timezone(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    if hasattr(LOCAL_TIMEZONE, "localize"):
        return LOCAL_TIMEZONE.localize(value)
    return value.replace(tzinfo=LOCAL_TIMEZONE)


def should_refresh_live_dropdown(
    state: ScraperState,
    *,
    now: datetime | None = None,
    force_refresh: bool | None = None,
) -> tuple[bool, str, bool]:
    if force_refresh is None:
        force_refresh = FORCE_DROPDOWN_DISCOVERY
    if now is None:
        now = datetime.now(LOCAL_TIMEZONE)

    if force_refresh:
        return True, "manual_override", True
    if not state.previous_live_dropdown_store_names:
        return True, "missing_cached_snapshot", True
    if state.cache.last_updated_at is None:
        return True, "missing_cache_timestamp", False

    last_updated_at = _attach_local_timezone(state.cache.last_updated_at)

    cache_age = now - last_updated_at
    if cache_age >= timedelta(days=DROPDOWN_REFRESH_MAX_AGE_DAYS):
        return True, "weekly_refresh_due", False
    return False, "cached_snapshot_fresh", False


def _apply_dropdown_selection_state(
    state: ScraperState,
    available_stores: list[dict[str, str]],
    filtered_stores: list[dict[str, str]],
    skipped_stores: list[dict[str, str]],
    *,
    refresh_mode: str,
    refresh_reason: str,
    discovery_attempt: str,
):
    current_live_store_names = sorted({store["store_name"] for store in available_stores})
    previous_live_store_names = sorted(set(state.previous_live_dropdown_store_names))
    current_live_store_name_set = set(current_live_store_names)
    previous_live_store_name_set = set(previous_live_store_names)

    state.live_dropdown_refresh_mode = refresh_mode
    state.live_dropdown_refresh_reason = refresh_reason
    state.current_live_dropdown_store_names = current_live_store_names
    state.live_dropdown_new_stores = sorted(current_live_store_name_set - previous_live_store_name_set)
    state.live_dropdown_missing_stores = sorted(previous_live_store_name_set - current_live_store_name_set)
    state.live_dropdown_discovery_attempt = discovery_attempt

    matched_configured_count = sum(bool(store.get("matched_from_configured")) for store in filtered_stores)
    live_only_count = len(filtered_stores) - matched_configured_count
    live_only_store_names = [
        store["store_name"]
        for store in filtered_stores
        if not store.get("matched_from_configured")
    ]
    state.live_dropdown_store_count = len(filtered_stores)
    state.live_dropdown_matched_configured_count = matched_configured_count
    state.live_dropdown_live_only_count = live_only_count
    state.live_dropdown_live_only_store_names = live_only_store_names
    state.live_dropdown_skipped_configured_count = len(skipped_stores)


def load_cached_dropdown_stores(
    urls_data: list[dict[str, str]],
    state: ScraperState,
    refresh_reason: str,
) -> list[dict[str, str]]:
    app_logger.info(
        "Skipping live dropdown discovery for this run; using cached live dropdown snapshot "
        f"({len(state.previous_live_dropdown_store_names)} store(s), reason={refresh_reason})."
    )

    cached_available_stores = [
        {
            "store_name": store_name,
            "normalized_name": resolve_dropdown_name(store_name),
            "merchant_id": "",
        }
        for store_name in state.previous_live_dropdown_store_names
    ]
    filtered_stores, skipped_stores = filter_stores_to_live_dropdown(urls_data, cached_available_stores)
    _apply_dropdown_selection_state(
        state,
        cached_available_stores,
        filtered_stores,
        skipped_stores,
        refresh_mode="cached",
        refresh_reason=refresh_reason,
        discovery_attempt="cached-snapshot",
    )

    app_logger.info(
        f"Cached dropdown queue contains {len(filtered_stores)} store(s): "
        f"{state.live_dropdown_matched_configured_count} matched configured row(s), "
        f"{state.live_dropdown_live_only_count} live-only row(s)."
    )
    if skipped_stores:
        skipped_names = ", ".join(store["store_name"] for store in skipped_stores[:10])
        suffix = "..." if len(skipped_stores) > 10 else ""
        app_logger.warning(
            f"Ignoring {len(skipped_stores)} configured store(s) not present in the cached live dropdown snapshot: "
            f"{skipped_names}{suffix}"
        )

    return filtered_stores


async def load_live_dropdown_stores(
    browser: Browser,
    storage_template: dict,
    urls_data: list[dict[str, str]],
    state: ScraperState,
    refresh_reason: str,
) -> list[dict[str, str]]:
    app_logger.info(
        "Refreshing live store list from the dashboard dropdown before queueing stores "
        f"(reason={refresh_reason})."
    )

    discovery_attempts = [
        ("fast-load", True, "load"),
        ("settled-load", True, "networkidle"),
        ("safe-load", False, "networkidle"),
    ]
    available_stores = None
    last_exc = None

    for attempt_name, optimize_context, wait_until in discovery_attempts:
        context = None
        page = None
        try:
            app_logger.info(
                f"Live dropdown discovery attempt '{attempt_name}' with wait_until='{wait_until}'."
            )
            context = await browser.new_context(storage_state=storage_template)
            if optimize_context:
                await optimize_browser_context(context)
            context.set_default_navigation_timeout(PAGE_TIMEOUT)
            context.set_default_timeout(ACTION_TIMEOUT)
            page = await context.new_page()
            await page.goto(BASE_DASHBOARD_URL, timeout=PAGE_TIMEOUT, wait_until=wait_until)
            available_stores = await discover_available_dropdown_stores(page)
            state.live_dropdown_discovery_attempt = attempt_name
            break
        except Exception as exc:
            last_exc = exc
            app_logger.warning(
                f"Live dropdown discovery attempt '{attempt_name}' failed: {exc}"
            )
        finally:
            await safe_close(page, f"Live dropdown discovery page ({attempt_name})", state.record_issue)
            await safe_close(context, f"Live dropdown discovery context ({attempt_name})", state.record_issue)

    if available_stores is None:
        app_logger.error("Failed to discover and filter stores from the live dashboard dropdown.")
        if last_exc:
            raise last_exc
        raise RuntimeError("Live dropdown discovery failed without a captured exception")

    filtered_stores, skipped_stores = filter_stores_to_live_dropdown(urls_data, available_stores)

    _apply_dropdown_selection_state(
        state,
        available_stores,
        filtered_stores,
        skipped_stores,
        refresh_mode="live",
        refresh_reason=refresh_reason,
        discovery_attempt=state.live_dropdown_discovery_attempt,
    )

    cache_updated = True
    for store in filtered_stores:
        merchant_id = store.get("merchant_id", "").strip()
        if merchant_id and state.cache.merchant_id_cache.get(store["store_name"]) != merchant_id:
            state.cache.merchant_id_cache[store["store_name"]] = merchant_id
            cache_updated = True

    if state.current_live_dropdown_store_names != state.cache.live_dropdown_store_names:
        state.cache.live_dropdown_store_names = state.current_live_dropdown_store_names
        cache_updated = True

    if cache_updated:
        await state.cache.save()
    app_logger.info(
        f"Live dropdown queue contains {len(filtered_stores)} store(s): "
        f"{state.live_dropdown_matched_configured_count} matched configured row(s), "
        f"{state.live_dropdown_live_only_count} live-only row(s)."
    )
    if state.live_dropdown_new_stores:
        app_logger.info(
            "Live dropdown added since last run: "
            + ", ".join(state.live_dropdown_new_stores[:10])
            + ("..." if len(state.live_dropdown_new_stores) > 10 else "")
        )
    if state.live_dropdown_missing_stores:
        app_logger.warning(
            "Live dropdown missing since last run: "
            + ", ".join(state.live_dropdown_missing_stores[:10])
            + ("..." if len(state.live_dropdown_missing_stores) > 10 else "")
        )
    if skipped_stores:
        skipped_names = ", ".join(store["store_name"] for store in skipped_stores[:10])
        suffix = "..." if len(skipped_stores) > 10 else ""
        app_logger.warning(
            f"Ignoring {len(skipped_stores)} configured store(s) not currently listed in the live dropdown: "
            f"{skipped_names}{suffix}"
        )

    return filtered_stores


def route_store_work_items(
    urls_data: list[dict[str, str]],
    state: ScraperState,
) -> tuple[list[WorkItem], list[WorkItem]]:
    fast_path_items: list[WorkItem] = []
    ui_items: list[WorkItem] = []
    fast_path_enabled = state.cache_template_available_at_start

    for store in urls_data:
        merchant_id = (store.get("merchant_id") or state.cache.merchant_id_cache.get(store["store_name"], "")).strip()
        work_item = WorkItem.from_store_info(store, merchant_id=merchant_id)
        if fast_path_enabled and merchant_id:
            fast_path_items.append(work_item)
        else:
            ui_items.append(work_item)

    state.fast_path_eligible_at_start = len(fast_path_items)
    state.ui_routed_at_start = len(ui_items)
    return fast_path_items, ui_items


def allocate_worker_counts(fast_path_store_count: int, ui_store_count: int) -> tuple[int, int]:
    api_workers = min(fast_path_store_count, FAST_PATH_MAX_CONCURRENCY, INITIAL_CONCURRENCY)

    needs_ui_capacity = ui_store_count > 0 or fast_path_store_count > 0
    if needs_ui_capacity:
        remaining_budget = max(INITIAL_CONCURRENCY - api_workers, 0)
        desired_ui_workers = ui_store_count or 1
        ui_workers = min(desired_ui_workers, remaining_budget if remaining_budget > 0 else 1)
    else:
        ui_workers = 0

    total_workers = api_workers + ui_workers
    if total_workers > INITIAL_CONCURRENCY:
        api_workers = max(INITIAL_CONCURRENCY - ui_workers, 0)

    return api_workers, ui_workers


def should_bypass_auto_concurrency(state: ScraperState) -> bool:
    return state.fast_path_eligible_at_start > 0 and state.ui_routed_at_start == 0


def get_effective_auto_concurrency_bounds(state: ScraperState) -> tuple[int, int]:
    effective_max = max(1, min(AUTO_MAX_CONCURRENCY, state.browser_worker_pool_size or AUTO_MAX_CONCURRENCY))
    effective_min = min(AUTO_MIN_CONCURRENCY, effective_max)
    return effective_min, effective_max


async def auto_concurrency_manager(state: ScraperState):
    if not AUTO_ENABLED:
        return
    if should_bypass_auto_concurrency(state):
        app_logger.info(
            "Auto-concurrency bypassed for warm-cache all-fast-path run; worker mix is fixed at startup."
        )
        return

    effective_min, effective_max = get_effective_auto_concurrency_bounds(state)
    app_logger.info(f"Auto-concurrency enabled with range {effective_min}-{effective_max}")
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
                state.concurrency_limit = max(effective_min, int(state.concurrency_limit * 0.5))
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
            ) and state.concurrency_limit > effective_min:
                state.concurrency_limit -= 1
                state.last_concurrency_change = now
                app_logger.info(
                    f"Auto-concurrency: decreased to {state.concurrency_limit} (CPU {cpu:.1f}%, MEM {mem:.1f}%)"
                )
            elif (
                cpu < CPU_LOWER_THRESHOLD
                and mem < MEM_UPPER_THRESHOLD
                and state.concurrency_limit < effective_max
            ):
                state.concurrency_limit += 1
                state.last_concurrency_change = now
                app_logger.info(
                    f"Auto-concurrency: increased to {state.concurrency_limit} (CPU {cpu:.1f}%, MEM {mem:.1f}%)"
                )

            if state.concurrency_limit > effective_max:
                state.concurrency_limit = effective_max
            if state.concurrency_limit < effective_min:
                state.concurrency_limit = effective_min

            async with state.concurrency_condition:
                state.concurrency_condition.notify_all()
        await asyncio.sleep(CHECK_INTERVAL)


async def _acquire_worker_slot(state: ScraperState):
    async with state.concurrency_condition:
        while state.active_workers_count >= state.concurrency_limit:
            await state.concurrency_condition.wait()
        state.active_workers_count += 1


async def _release_worker_slot(state: ScraperState):
    async with state.concurrency_condition:
        state.active_workers_count -= 1
        state.concurrency_condition.notify_all()


async def fast_path_worker_task(
    worker_id: int,
    browser: Browser,
    storage_template: dict,
    fast_path_queue: asyncio.Queue,
    ui_queue: asyncio.Queue,
    submission_queue: asyncio.Queue,
    state: ScraperState,
):
    worker_label = f"FastPathWorker-{worker_id}"
    app_logger.info(f"[{worker_label}] Starting up.")
    context = None
    try:
        context = await browser.new_context(storage_state=storage_template)
        context.set_default_navigation_timeout(PAGE_TIMEOUT)
        context.set_default_timeout(ACTION_TIMEOUT)

        while True:
            try:
                work_item = fast_path_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            await _acquire_worker_slot(state)
            try:
                await process_fast_path_store(context.request, work_item, ui_queue, submission_queue, state)
            except Exception as exc:
                app_logger.exception(
                    f"[{worker_label}] Unhandled exception while processing {work_item.store_name}: {exc}"
                )
                await state.add_failure(
                    f"{work_item.store_name} (Worker Exception)",
                    asyncio.get_running_loop().time(),
                    category="worker",
                )
            finally:
                await _release_worker_slot(state)
                fast_path_queue.task_done()

    except Exception as exc:
        app_logger.error(f"[{worker_label}] Crashed during setup: {exc}")
        await state.add_failure(
            f"{worker_label} (Worker Setup Exception)",
            asyncio.get_running_loop().time(),
            category="worker",
        )
    finally:
        await safe_close(context, f"{worker_label} context", state.record_issue)
        app_logger.info(f"[{worker_label}] Shutting down.")


async def ui_worker_task(
    worker_id: int,
    browser: Browser,
    storage_template: dict,
    ui_queue: asyncio.Queue,
    submission_queue: asyncio.Queue,
    fast_path_done: asyncio.Event,
    state: ScraperState,
):
    worker_label = f"UIWorker-{worker_id}"
    app_logger.info(f"[{worker_label}] Starting up.")
    context = None
    page = None
    try:
        context = await browser.new_context(storage_state=storage_template)
        await optimize_browser_context(context)
        context.set_default_navigation_timeout(PAGE_TIMEOUT)
        context.set_default_timeout(ACTION_TIMEOUT)

        while True:
            try:
                work_item = ui_queue.get_nowait()
            except asyncio.QueueEmpty:
                if fast_path_done.is_set():
                    break
                await asyncio.sleep(0.1)
                continue

            await _acquire_worker_slot(state)
            try:
                if page is None or page.is_closed():
                    page = await context.new_page()
                await process_ui_store(page, work_item, submission_queue, state)
            except Exception as exc:
                app_logger.exception(
                    f"[{worker_label}] Unhandled exception while processing {work_item.store_name}: {exc}"
                )
                await state.add_failure(
                    f"{work_item.store_name} (Worker Exception)",
                    asyncio.get_running_loop().time(),
                    category="worker",
                )
            finally:
                await _release_worker_slot(state)
                ui_queue.task_done()

    except Exception as exc:
        app_logger.error(f"[{worker_label}] Crashed during setup: {exc}")
        await state.add_failure(
            f"{worker_label} (Worker Setup Exception)",
            asyncio.get_running_loop().time(),
            category="worker",
        )
    finally:
        await safe_close(page, f"{worker_label} page", state.record_issue)
        await safe_close(context, f"{worker_label} context", state.record_issue)
        app_logger.info(f"[{worker_label}] Shutting down.")


async def process_urls(browser: Browser, state: ScraperState):
    state.form_submitter_count = NUM_FORM_SUBMITTERS
    app_logger.info(f"Job 'process_urls' started with worker budget: {INITIAL_CONCURRENCY}")

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
            await optimize_browser_context(temp_context)
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

    refresh_live_dropdown, refresh_reason, refresh_required = should_refresh_live_dropdown(state)
    if refresh_live_dropdown:
        try:
            urls_data = await load_live_dropdown_stores(
                browser,
                storage_template,
                urls_data,
                state,
                refresh_reason,
            )
        except Exception:
            if refresh_required:
                raise
            app_logger.warning(
                "Live dropdown refresh failed; falling back to cached live dropdown snapshot "
                f"(reason={refresh_reason})."
            )
            await state.record_issue(
                f"Live dropdown refresh failed; used cached snapshot instead ({refresh_reason})",
                asyncio.get_running_loop().time(),
                category="general",
            )
            urls_data = load_cached_dropdown_stores(urls_data, state, "refresh_failed_used_cached_snapshot")
    else:
        urls_data = load_cached_dropdown_stores(urls_data, state, refresh_reason)

    if not urls_data:
        if state.live_dropdown_refresh_mode == "cached":
            app_logger.error("No configured stores are present in the cached live dropdown snapshot. Aborting job.")
            state.set_job_status(
                "aborted_no_stores",
                "No configured stores were present in the cached live dropdown snapshot",
            )
        else:
            app_logger.error("No configured stores are currently listed in the live dropdown. Aborting job.")
            state.set_job_status("aborted_no_stores", "No configured stores were present in the live dropdown")
        return

    fast_path_items, ui_items = route_store_work_items(urls_data, state)
    api_workers, ui_workers = allocate_worker_counts(len(fast_path_items), len(ui_items))
    total_workers = api_workers + ui_workers
    state.browser_worker_pool_size = total_workers
    state.concurrency_limit = max(total_workers, 1)

    app_logger.info(
        "Routing stores for warm-cache run: "
        f"{len(fast_path_items)} fast-path eligible, {len(ui_items)} queued for UI."
    )
    app_logger.info(
        "Worker allocation: "
        f"{api_workers} fast-path worker(s), {ui_workers} UI worker(s), "
        f"{NUM_FORM_SUBMITTERS} form submitter(s)."
    )

    fast_path_queue = asyncio.Queue()
    ui_queue = asyncio.Queue()
    submission_queue = asyncio.Queue()

    for work_item in fast_path_items:
        fast_path_queue.put_nowait(work_item)
    for work_item in ui_items:
        ui_queue.put_nowait(work_item)

    await state.init_progress(len(urls_data))
    start_time = datetime.now(LOCAL_TIMEZONE)

    app_logger.info(f"Starting {NUM_FORM_SUBMITTERS} HTTP form submitter worker(s).")
    form_submitter_tasks = [
        asyncio.create_task(http_form_submitter_worker(submission_queue, i + 1, state))
        for i in range(NUM_FORM_SUBMITTERS)
    ]

    fast_path_done = asyncio.Event()
    if api_workers == 0:
        fast_path_done.set()

    app_logger.info(f"Spinning up {total_workers} browser worker(s)...")
    fast_path_workers = [
        asyncio.create_task(
            fast_path_worker_task(
                i + 1,
                browser,
                storage_template,
                fast_path_queue,
                ui_queue,
                submission_queue,
                state,
            )
        )
        for i in range(api_workers)
    ]
    ui_workers_tasks = [
        asyncio.create_task(
            ui_worker_task(
                i + 1,
                browser,
                storage_template,
                ui_queue,
                submission_queue,
                fast_path_done,
                state,
            )
        )
        for i in range(ui_workers)
    ]

    # Auto-concurrency task
    auto_task = asyncio.create_task(auto_concurrency_manager(state))

    await asyncio.gather(*fast_path_workers)
    fast_path_done.set()
    await asyncio.gather(*ui_workers_tasks)

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

    if state.run_failures:
        state.set_job_status("completed_with_failures", f"{len(state.run_failures)} terminal failure(s)")
        app_logger.warning(f"Completed with {len(state.run_failures)} issue(s): {', '.join(state.run_failures)}")
    else:
        state.set_job_status("completed", "Run completed successfully")
        app_logger.info("Completed successfully.")

    await post_job_summary(state, elapsed)
    state.cache.update_csv_with_cache()


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
        await post_job_summary(state)
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
