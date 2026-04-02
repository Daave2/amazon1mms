from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import psutil
from playwright.async_api import Browser, async_playwright

from core.config import Settings, load_settings
from core.logger import app_logger, configure_logging
from core.reporting import write_runtime_reports
from core.state import ScraperState
from core.store_loader import load_stores_from_csv
from core.utils import optimize_browser_context, safe_close
from core.work_items import WorkItem
from services.auth_service import check_if_login_needed, prime_master_session
from services.chat_service import chat_dispatcher_worker, post_job_summary
from services.forms_service import SubmissionManager, SubmissionTask, http_form_submitter_worker
from services.metrics_service import (
    _selection_matches_target,
    discover_available_dropdown_stores,
    process_fast_path_store,
    process_ui_store,
    resolve_dropdown_name,
)

DEFAULT_SETTINGS = load_settings()
LOCAL_TIMEZONE = DEFAULT_SETTINGS.local_timezone


@dataclass
class RuntimeServices:
    submission_manager: SubmissionManager
    chat_queue: asyncio.Queue
    form_submitter_tasks: list[asyncio.Task]
    chat_dispatcher_task: asyncio.Task
    auto_concurrency_task: asyncio.Task | None = None


@dataclass
class BootstrapPhaseResult:
    configured_stores: list[dict[str, str]]
    storage_template: dict
    pending_replays: list[SubmissionTask]
    runtime: RuntimeServices


@dataclass
class DiscoveryPhaseResult:
    queued_stores: list[dict[str, str]]


@dataclass
class ExecutionPhaseResult:
    elapsed_seconds: float


def load_default_data(state: ScraperState, csv_path: str = "urls.csv") -> list[dict[str, str]]:
    state.cache.load()
    state.cache_template_available_at_start = bool(state.cache.api_url_template)
    state.cache_merchant_ids_at_start = len(state.cache.merchant_id_cache)
    state.previous_live_dropdown_store_names = list(state.cache.live_dropdown_store_names)

    try:
        urls_data = load_stores_from_csv(
            csv_path,
            on_skip=lambda row_number, _row: app_logger.warning(
                f"Skipping row {row_number} in {csv_path}: no store name found"
            ),
        )
        app_logger.info(f"{len(urls_data)} stores loaded from {csv_path}")
    except FileNotFoundError:
        app_logger.error(f"FATAL: '{csv_path}' not found.")
        raise
    except Exception:
        app_logger.exception(f"An error occurred while loading {csv_path}")
        raise

    return urls_data


def ensure_storage_state(settings: Settings) -> bool:
    if not os.path.exists(settings.storage_state_path) or os.path.getsize(settings.storage_state_path) == 0:
        return False
    try:
        with open(settings.storage_state_path, encoding="utf-8") as file_handle:
            data = json.load(file_handle)
        return (
            isinstance(data, dict)
            and isinstance(data.get("cookies"), list)
            and bool(data["cookies"])
        )
    except json.JSONDecodeError:
        return False


def filter_stores_to_live_dropdown(
    urls_data: list[dict[str, str]],
    available_stores: list[dict[str, str]],
    settings: Settings | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    settings = settings or load_settings()
    indexed_configured_stores = [
        {
            **store,
            "_index": index,
            "_resolved_name": resolve_dropdown_name(store["store_name"], settings),
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
            settings,
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
        {key: value for key, value in configured_store.items() if not key.startswith("_")}
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
    settings: Settings | None = None,
) -> dict[str, str] | None:
    settings = settings or load_settings()
    live_name = available_store["store_name"]
    live_normalized_name = available_store.get("normalized_name", "").strip()
    live_merchant_id = available_store.get("merchant_id", "").strip()

    merchant_candidates = [
        candidate
        for candidate in configured_by_merchant.get(live_merchant_id, [])
        if candidate["_index"] not in matched_configured_indices
    ]
    matched_store = _pick_best_configured_candidate(merchant_candidates, live_name, live_normalized_name, settings)
    if matched_store:
        return matched_store

    name_candidates = [
        candidate
        for candidate in configured_by_name.get(live_normalized_name, [])
        if candidate["_index"] not in matched_configured_indices
    ]
    matched_store = _pick_best_configured_candidate(name_candidates, live_name, live_normalized_name, settings)
    if matched_store:
        return matched_store

    fuzzy_candidates = [
        candidate
        for candidate in configured_stores
        if candidate["_index"] not in matched_configured_indices
        and _selection_matches_target(live_name, candidate["_resolved_name"], candidate["store_name"], settings)
    ]
    return _pick_best_configured_candidate(fuzzy_candidates, live_name, live_normalized_name, settings)


def _pick_best_configured_candidate(
    candidates: list[dict[str, str]],
    live_name: str,
    live_normalized_name: str,
    settings: Settings | None = None,
) -> dict[str, str] | None:
    settings = settings or load_settings()
    if not candidates:
        return None

    exact_name_matches = [candidate for candidate in candidates if candidate["_resolved_name"] == live_normalized_name]
    if exact_name_matches:
        return exact_name_matches[0]

    fuzzy_matches = [
        candidate
        for candidate in candidates
        if _selection_matches_target(live_name, candidate["_resolved_name"], candidate["store_name"], settings)
    ]
    if fuzzy_matches:
        return fuzzy_matches[0]

    if len(candidates) == 1:
        return candidates[0]

    return None


def _attach_local_timezone(value: datetime, settings: Settings) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=settings.local_timezone)


def should_refresh_live_dropdown(
    state: ScraperState,
    *,
    now: datetime | None = None,
    force_refresh: bool | None = None,
) -> tuple[bool, str, bool]:
    settings = state.settings
    if force_refresh is None:
        force_refresh = settings.force_dropdown_discovery
    if now is None:
        now = datetime.now(settings.local_timezone)

    if force_refresh:
        return True, "manual_override", True
    if not state.previous_live_dropdown_store_names:
        return True, "missing_cached_snapshot", True
    if state.cache.last_updated_at is None:
        return True, "missing_cache_timestamp", False

    last_updated_at = _attach_local_timezone(state.cache.last_updated_at, settings)
    cache_age = now - last_updated_at
    if cache_age >= timedelta(days=settings.dropdown_refresh_max_age_days):
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
    live_only_store_names = [store["store_name"] for store in filtered_stores if not store.get("matched_from_configured")]

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
            "normalized_name": resolve_dropdown_name(store_name, state.settings),
            "merchant_id": "",
        }
        for store_name in state.previous_live_dropdown_store_names
    ]
    filtered_stores, skipped_stores = filter_stores_to_live_dropdown(urls_data, cached_available_stores, state.settings)
    _apply_dropdown_selection_state(
        state,
        cached_available_stores,
        filtered_stores,
        skipped_stores,
        refresh_mode="cached",
        refresh_reason=refresh_reason,
        discovery_attempt="cached-snapshot",
    )
    return filtered_stores


async def load_live_dropdown_stores(
    browser: Browser,
    storage_template: dict,
    urls_data: list[dict[str, str]],
    state: ScraperState,
    refresh_reason: str,
) -> list[dict[str, str]]:
    settings = state.settings
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
            app_logger.info(f"Live dropdown discovery attempt '{attempt_name}' with wait_until='{wait_until}'.")
            context = await browser.new_context(storage_state=storage_template)
            if optimize_context:
                await optimize_browser_context(context, settings)
            context.set_default_navigation_timeout(settings.page_timeout_ms)
            context.set_default_timeout(settings.action_timeout_ms)
            page = await context.new_page()
            await page.goto(settings.base_dashboard_url, timeout=settings.page_timeout_ms, wait_until=wait_until)
            available_stores = await discover_available_dropdown_stores(page, settings)
            state.live_dropdown_discovery_attempt = attempt_name
            break
        except Exception as exc:
            last_exc = exc
            app_logger.warning(f"Live dropdown discovery attempt '{attempt_name}' failed: {exc}")
        finally:
            await safe_close(page, f"Live dropdown discovery page ({attempt_name})", state.record_issue)
            await safe_close(context, f"Live dropdown discovery context ({attempt_name})", state.record_issue)

    if available_stores is None:
        if last_exc:
            raise last_exc
        raise RuntimeError("Live dropdown discovery failed without a captured exception")

    filtered_stores, skipped_stores = filter_stores_to_live_dropdown(urls_data, available_stores, settings)
    _apply_dropdown_selection_state(
        state,
        available_stores,
        filtered_stores,
        skipped_stores,
        refresh_mode="live",
        refresh_reason=refresh_reason,
        discovery_attempt=state.live_dropdown_discovery_attempt,
    )

    cache_updated = False
    for store in filtered_stores:
        merchant_id = store.get("merchant_id", "").strip()
        if merchant_id and state.cache.set_merchant_id(store["store_name"], merchant_id):
            cache_updated = True
    if state.cache.set_live_dropdown_store_names(state.current_live_dropdown_store_names):
        cache_updated = True

    if cache_updated:
        await state.cache.save()

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


def allocate_worker_counts(
    fast_path_store_count: int,
    ui_store_count: int,
    settings: Settings | None = None,
) -> tuple[int, int]:
    settings = settings or load_settings()
    api_workers = min(fast_path_store_count, settings.fast_path_max_concurrency, settings.initial_concurrency)

    needs_ui_capacity = ui_store_count > 0 or fast_path_store_count > 0
    if needs_ui_capacity:
        remaining_budget = max(settings.initial_concurrency - api_workers, 0)
        desired_ui_workers = ui_store_count or 1
        ui_workers = min(desired_ui_workers, remaining_budget if remaining_budget > 0 else 1)
    else:
        ui_workers = 0

    total_workers = api_workers + ui_workers
    if total_workers > settings.initial_concurrency:
        api_workers = max(settings.initial_concurrency - ui_workers, 0)

    return api_workers, ui_workers


def should_bypass_auto_concurrency(state: ScraperState) -> bool:
    return state.fast_path_eligible_at_start > 0 and state.ui_routed_at_start == 0


def get_effective_auto_concurrency_bounds(state: ScraperState) -> tuple[int, int]:
    settings = state.settings
    effective_max = max(1, min(settings.auto_max_concurrency, state.browser_worker_pool_size or settings.auto_max_concurrency))
    effective_min = min(settings.auto_min_concurrency, effective_max)
    return effective_min, effective_max


async def auto_concurrency_manager(state: ScraperState):
    settings = state.settings
    if not settings.auto_enabled:
        return
    if should_bypass_auto_concurrency(state):
        app_logger.info("Auto-concurrency bypassed for warm-cache all-fast-path run; worker mix is fixed at startup.")
        return

    effective_min, effective_max = get_effective_auto_concurrency_bounds(state)
    app_logger.info(f"Auto-concurrency enabled with range {effective_min}-{effective_max}")
    while True:
        now = asyncio.get_running_loop().time()

        async with state.failure_lock:
            while state.failure_timestamps and now - state.failure_timestamps[0] > 60:
                state.failure_timestamps.pop(0)
            recent_failure_count = len(state.failure_timestamps)

        estimated_throughput = max(state.concurrency_limit * 30, 1)
        failure_rate = recent_failure_count / estimated_throughput
        if failure_rate > 0.05 and now - state.last_concurrency_change >= settings.cooldown_seconds:
            state.concurrency_limit = max(effective_min, int(state.concurrency_limit * 0.5))
            state.last_concurrency_change = now
            async with state.concurrency_condition:
                state.concurrency_condition.notify_all()
            await asyncio.sleep(settings.cooldown_seconds * 2)
            continue

        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        if now - state.last_concurrency_change >= settings.cooldown_seconds:
            if (cpu > settings.cpu_upper_threshold or mem > settings.mem_upper_threshold) and state.concurrency_limit > effective_min:
                state.concurrency_limit -= 1
                state.last_concurrency_change = now
            elif cpu < settings.cpu_lower_threshold and mem < settings.mem_upper_threshold and state.concurrency_limit < effective_max:
                state.concurrency_limit += 1
                state.last_concurrency_change = now

            state.concurrency_limit = max(min(state.concurrency_limit, effective_max), effective_min)
            async with state.concurrency_condition:
                state.concurrency_condition.notify_all()

        await asyncio.sleep(settings.check_interval_seconds)


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
    state: ScraperState,
    submission_manager: SubmissionManager | None = None,
    submission_queue: asyncio.Queue | None = None,
):
    worker_label = f"FastPathWorker-{worker_id}"
    app_logger.info(f"[{worker_label}] Starting up.")
    context = None
    try:
        context = await browser.new_context(storage_state=storage_template)
        context.set_default_navigation_timeout(state.settings.page_timeout_ms)
        context.set_default_timeout(state.settings.action_timeout_ms)

        while True:
            try:
                work_item = fast_path_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            await _acquire_worker_slot(state)
            try:
                submission_target = submission_manager if submission_manager is not None else submission_queue
                await process_fast_path_store(
                    context.request,
                    work_item,
                    ui_queue,
                    submission_target,
                    state,
                )
            except Exception as exc:
                app_logger.exception(f"[{worker_label}] Unhandled exception while processing {work_item.store_name}: {exc}")
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
    state: ScraperState,
    submission_manager: SubmissionManager | None = None,
    fast_path_done: asyncio.Event | None = None,
    submission_queue: asyncio.Queue | None = None,
):
    worker_label = f"UIWorker-{worker_id}"
    app_logger.info(f"[{worker_label}] Starting up.")
    context = None
    page = None
    try:
        context = await browser.new_context(storage_state=storage_template)
        await optimize_browser_context(context, state.settings)
        context.set_default_navigation_timeout(state.settings.page_timeout_ms)
        context.set_default_timeout(state.settings.action_timeout_ms)

        while True:
            try:
                work_item = ui_queue.get_nowait()
            except asyncio.QueueEmpty:
                if fast_path_done is not None and fast_path_done.is_set():
                    break
                await asyncio.sleep(0.1)
                continue

            await _acquire_worker_slot(state)
            try:
                if page is None or page.is_closed():
                    page = await context.new_page()
                submission_target = submission_manager if submission_manager is not None else submission_queue
                await process_ui_store(page, work_item, submission_target, state)
            except Exception as exc:
                app_logger.exception(f"[{worker_label}] Unhandled exception while processing {work_item.store_name}: {exc}")
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


async def bootstrap_phase(browser: Browser, state: ScraperState) -> BootstrapPhaseResult:
    settings = state.settings
    state.form_submitter_count = settings.num_form_submitters
    configured_stores = load_default_data(state)

    chat_queue: asyncio.Queue = asyncio.Queue()
    submission_manager = SubmissionManager(settings, state, chat_queue)
    form_submitter_tasks = [
        asyncio.create_task(http_form_submitter_worker(submission_manager, worker_id + 1))
        for worker_id in range(settings.num_form_submitters)
    ]
    chat_dispatcher_task = asyncio.create_task(chat_dispatcher_worker(chat_queue, state, settings))
    runtime = RuntimeServices(
        submission_manager=submission_manager,
        chat_queue=chat_queue,
        form_submitter_tasks=form_submitter_tasks,
        chat_dispatcher_task=chat_dispatcher_task,
    )
    pending_replays = submission_manager.load_pending_replays()

    if not configured_stores and not pending_replays:
        state.set_job_status("aborted_no_stores", "No usable stores were found in urls.csv")
        return BootstrapPhaseResult([], {}, pending_replays, runtime)

    storage_template: dict = {}
    if configured_stores:
        login_is_required = True
        if ensure_storage_state(settings):
            temp_context = None
            try:
                with open(settings.storage_state_path, encoding="utf-8") as file_handle:
                    storage_for_check = json.load(file_handle)
                temp_context = await browser.new_context(storage_state=storage_for_check)
                await optimize_browser_context(temp_context, settings)
                temp_page = await temp_context.new_page()
                if not await check_if_login_needed(temp_page, settings.base_dashboard_url, settings):
                    login_is_required = False
                    state.auth_state_status = "reused"
                else:
                    state.auth_state_status = "refresh_required"
            except Exception as exc:
                app_logger.error(f"An error occurred during session verification: {exc}", exc_info=settings.debug_mode)
                state.auth_state_status = "refresh_required"
            finally:
                await safe_close(temp_context, "Session verification context", state.record_issue)
        else:
            state.auth_state_status = "missing"

        if login_is_required:
            login_successful = False
            for attempt in range(settings.max_login_attempts):
                app_logger.info(
                    f"Attempting to prime a new master session (Attempt {attempt + 1}/{settings.max_login_attempts})..."
                )
                if await prime_master_session(browser, settings):
                    login_successful = True
                    break
                if attempt < settings.max_login_attempts - 1:
                    await asyncio.sleep(5)

            if not login_successful:
                state.auth_state_status = "refresh_failed"
                state.set_job_status("login_aborted", f"Session priming failed after {settings.max_login_attempts} attempts")
                return BootstrapPhaseResult(configured_stores, {}, pending_replays, runtime)
            state.auth_state_status = "refreshed"

        with open(settings.storage_state_path, encoding="utf-8") as file_handle:
            storage_template = json.load(file_handle)

    return BootstrapPhaseResult(configured_stores, storage_template, pending_replays, runtime)


async def discover_phase(browser: Browser, bootstrap: BootstrapPhaseResult, state: ScraperState) -> DiscoveryPhaseResult:
    if not bootstrap.configured_stores or state.job_status == "login_aborted":
        return DiscoveryPhaseResult([])

    refresh_live_dropdown, refresh_reason, refresh_required = should_refresh_live_dropdown(state)
    if refresh_live_dropdown:
        try:
            queued_stores = await load_live_dropdown_stores(
                browser,
                bootstrap.storage_template,
                bootstrap.configured_stores,
                state,
                refresh_reason,
            )
        except Exception:
            if refresh_required:
                raise
            await state.record_issue(
                f"Live dropdown refresh failed; used cached snapshot instead ({refresh_reason})",
                asyncio.get_running_loop().time(),
                category="general",
            )
            queued_stores = load_cached_dropdown_stores(
                bootstrap.configured_stores,
                state,
                "refresh_failed_used_cached_snapshot",
            )
    else:
        queued_stores = load_cached_dropdown_stores(bootstrap.configured_stores, state, refresh_reason)

    return DiscoveryPhaseResult(queued_stores)


async def execute_phase(
    browser: Browser,
    bootstrap: BootstrapPhaseResult,
    discovery: DiscoveryPhaseResult,
    state: ScraperState,
) -> ExecutionPhaseResult:
    settings = state.settings
    if bootstrap.pending_replays:
        app_logger.info(f"Replaying {len(bootstrap.pending_replays)} pending submission(s) before scraping.")
        await bootstrap.runtime.submission_manager.enqueue_replay_tasks(bootstrap.pending_replays)
        await bootstrap.runtime.submission_manager.queue.join()

    if not discovery.queued_stores:
        if state.job_status == "running":
            detail = (
                "No configured stores were present in the cached live dropdown snapshot"
                if state.live_dropdown_refresh_mode == "cached"
                else "No configured stores were present in the live dropdown"
            )
            state.set_job_status("aborted_no_stores", detail)
        return ExecutionPhaseResult(elapsed_seconds=0.0)

    fast_path_items, ui_items = route_store_work_items(discovery.queued_stores, state)
    api_workers, ui_workers = allocate_worker_counts(len(fast_path_items), len(ui_items), settings)
    total_workers = api_workers + ui_workers
    state.browser_worker_pool_size = total_workers
    state.concurrency_limit = max(total_workers, 1)

    fast_path_queue: asyncio.Queue = asyncio.Queue()
    ui_queue: asyncio.Queue = asyncio.Queue()
    for work_item in fast_path_items:
        fast_path_queue.put_nowait(work_item)
    for work_item in ui_items:
        ui_queue.put_nowait(work_item)

    await state.init_progress(len(discovery.queued_stores))
    start_time = datetime.now(settings.local_timezone)
    fast_path_done = asyncio.Event()
    if api_workers == 0:
        fast_path_done.set()

    bootstrap.runtime.auto_concurrency_task = asyncio.create_task(auto_concurrency_manager(state))

    fast_path_workers = [
        asyncio.create_task(
            fast_path_worker_task(
                worker_id + 1,
                browser,
                bootstrap.storage_template,
                fast_path_queue,
                ui_queue,
                state,
                bootstrap.runtime.submission_manager,
            )
        )
        for worker_id in range(api_workers)
    ]
    ui_workers_tasks = [
        asyncio.create_task(
            ui_worker_task(
                worker_id + 1,
                browser,
                bootstrap.storage_template,
                ui_queue,
                state,
                bootstrap.runtime.submission_manager,
                fast_path_done,
            )
        )
        for worker_id in range(ui_workers)
    ]

    await asyncio.gather(*fast_path_workers)
    fast_path_done.set()
    await asyncio.gather(*ui_workers_tasks)

    elapsed_seconds = (datetime.now(settings.local_timezone) - start_time).total_seconds()
    return ExecutionPhaseResult(elapsed_seconds=elapsed_seconds)


async def drain_phase(runtime: RuntimeServices):
    await runtime.submission_manager.queue.join()

    for _ in runtime.form_submitter_tasks:
        await runtime.submission_manager.queue.put(None)
    await runtime.submission_manager.queue.join()
    await asyncio.gather(*runtime.form_submitter_tasks, return_exceptions=True)

    await runtime.chat_queue.put(None)
    await runtime.chat_queue.join()
    await runtime.chat_dispatcher_task

    if runtime.auto_concurrency_task:
        runtime.auto_concurrency_task.cancel()
        await asyncio.gather(runtime.auto_concurrency_task, return_exceptions=True)


async def finalize_phase(state: ScraperState, elapsed_seconds: float):
    if state.job_status == "running":
        if state.run_failures:
            state.set_job_status("completed_with_failures", f"{len(state.run_failures)} terminal failure(s)")
        else:
            state.set_job_status("completed", "Run completed successfully")

    await post_job_summary(state, state.settings, elapsed_seconds)
    state.cache.update_csv_with_cache()


async def run_scraper(browser: Browser, state: ScraperState):
    bootstrap: BootstrapPhaseResult | None = None
    elapsed_seconds = 0.0
    try:
        bootstrap = await bootstrap_phase(browser, state)
        discovery = await discover_phase(browser, bootstrap, state)
        execution = await execute_phase(browser, bootstrap, discovery, state)
        elapsed_seconds = execution.elapsed_seconds
    finally:
        if bootstrap is not None:
            await drain_phase(bootstrap.runtime)
    return elapsed_seconds


async def main():
    settings = load_settings()
    configure_logging(settings)

    app_logger.info("Starting up in single-run mode...")
    playwright = None
    browser = None
    state = ScraperState(settings)
    elapsed_seconds = 0.0
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=not settings.debug_mode,
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
        elapsed_seconds = await run_scraper(browser, state)
    except Exception as exc:
        state.fatal_error_message = str(exc)
        state.set_job_status("fatal", "Unhandled exception in main execution block")
        app_logger.critical(f"A critical error occurred in the main execution block: {exc}", exc_info=settings.debug_mode)
    finally:
        app_logger.info("Task finished. Initiating shutdown...")
        if browser:
            await safe_close(browser, "Browser", state.record_issue)
        if playwright:
            try:
                await playwright.stop()
            except Exception as exc:
                await state.record_issue(
                    f"Playwright stop (Cleanup failure: {exc})",
                    asyncio.get_running_loop().time(),
                    category="cleanup",
                )

        if state.job_status == "running":
            default_status = "completed_with_failures" if state.run_failures else "completed"
            default_detail = (
                "Run exited during shutdown with recorded failures"
                if state.run_failures
                else "Run completed during shutdown"
            )
            state.set_job_status(default_status, default_detail)

        state.finish_run()
        await finalize_phase(state, elapsed_seconds)
        try:
            write_runtime_reports(state, settings)
        except Exception as exc:
            app_logger.warning(f"Failed to write runtime reports: {exc}")
        app_logger.info("Run complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        app_logger.info("Script interrupted by user. Exiting.")
