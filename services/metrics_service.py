from __future__ import annotations

import asyncio
import difflib
import os
import re
import urllib.parse
from datetime import datetime

from playwright.async_api import Page, TimeoutError, expect

from core.config import SPECIAL_NAME_MAPPINGS, Settings
from core.logger import app_logger
from core.metrics import build_form_data, normalize_metrics_payload
from core.state import ScraperState
from core.utils import normalize_name, save_screenshot
from core.work_items import WorkItem
from services.forms_service import SubmissionManager

METRICS_TIMEOUT = 45_000
TRANSIENT_FAST_PATH_STATUSES = {503, 504}


class _QueueSubmissionAdapter:
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue

    async def enqueue_submission(self, form_data: dict[str, str]):
        await self.queue.put(form_data)


def resolve_dropdown_name(store_name: str, settings: Settings | None = None) -> str:
    dropdown_name = normalize_name(store_name)
    special_name_mappings = settings.special_name_mappings if settings else SPECIAL_NAME_MAPPINGS

    for key, value in special_name_mappings.items():
        if key in dropdown_name:
            return value

    return dropdown_name


def _build_search_terms(dropdown_name: str, store_name: str) -> list[str]:
    raw_terms = [
        dropdown_name,
        dropdown_name.replace("-", " "),
        dropdown_name.replace(" ", "-"),
        normalize_name(store_name),
        store_name,
        re.sub(r"(?i)\s*morrisons?$", "", store_name).strip(),
        re.sub(r"(?i)^morrisons?\s*", "", store_name).strip(),
    ]

    search_terms: list[str] = []
    seen: set[str] = set()
    for term in raw_terms:
        cleaned_term = re.sub(r"\s+", " ", term).strip()
        if not cleaned_term:
            continue
        key = cleaned_term.lower()
        if key in seen:
            continue
        seen.add(key)
        search_terms.append(cleaned_term)
    return search_terms


def _selection_matches_target(
    selected_text: str,
    dropdown_name: str,
    store_name: str,
    settings: Settings | None = None,
) -> bool:
    def _normalized_tokens(value: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", value.lower())

    def _is_token_subsequence(shorter_tokens: list[str], longer_tokens: list[str]) -> bool:
        if not shorter_tokens or len(shorter_tokens) > len(longer_tokens):
            return False
        for index in range(len(longer_tokens) - len(shorter_tokens) + 1):
            if longer_tokens[index : index + len(shorter_tokens)] == shorter_tokens:
                return True
        return False

    selected_norm = resolve_dropdown_name(selected_text, settings)
    selected_tokens = _normalized_tokens(selected_norm)
    candidate_norms = {
        resolve_dropdown_name(dropdown_name, settings),
        resolve_dropdown_name(store_name, settings),
    }

    for candidate in candidate_norms:
        if not candidate:
            continue
        candidate_tokens = _normalized_tokens(candidate)
        if selected_norm == candidate:
            return True
        if _is_token_subsequence(candidate_tokens, selected_tokens) or _is_token_subsequence(
            selected_tokens,
            candidate_tokens,
        ):
            return True
        if (len(selected_tokens) > 1 or len(candidate_tokens) > 1) and difflib.SequenceMatcher(
            None,
            selected_norm,
            candidate,
        ).ratio() >= 0.72:
            return True
    return False


def _parse_available_store_options(
    option_rows: list[tuple[str | None, str | None]],
    settings: Settings | None = None,
) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen: set[str] = set()

    for option_id, option_text in option_rows:
        cleaned_text = re.sub(r"\s+", " ", option_text or "").strip()
        normalized_name = resolve_dropdown_name(cleaned_text, settings)
        if not normalized_name or normalized_name in seen:
            continue

        seen.add(normalized_name)
        merchant_id = ""
        if option_id and option_id.startswith("store-selector-option-"):
            merchant_id = option_id.replace("store-selector-option-", "", 1)

        options.append(
            {
                "store_name": cleaned_text,
                "normalized_name": normalized_name,
                "merchant_id": merchant_id,
            }
        )

    return options


async def _visible_dropdown_overlay(page: Page):
    selectors = [
        "kat-popover:visible",
        "kat-dropdown-menu:visible",
        "[role='listbox']:visible",
        ".dropdown-content:visible",
        ".dropdown-popover:visible",
        ".dropdown-list:visible",
    ]
    for selector in selectors:
        overlay = page.locator(selector).first
        try:
            if await overlay.is_visible():
                return overlay
        except Exception:
            continue
    return None


async def _open_store_dropdown(page: Page, store_name: str):
    dropdown_trigger = page.locator("#store-selector-dropdown")
    try:
        await expect(dropdown_trigger).to_be_visible(timeout=30000)
        for _ in range(3):
            await dropdown_trigger.first.click(force=True)
            await asyncio.sleep(1)
            overlay = await _visible_dropdown_overlay(page)
            if overlay is not None:
                return dropdown_trigger, overlay
    except Exception as exc:
        app_logger.warning(f"[{store_name}] Failed to click dropdown trigger: {exc}")
        await page.get_by_text("Select a store").first.click(force=True)

    await asyncio.sleep(2)
    overlay = await _visible_dropdown_overlay(page)
    if overlay is None:
        raise TimeoutError("Store selector dropdown did not open")

    return dropdown_trigger, overlay


async def _current_store_selector_text(page: Page) -> str:
    selectors = ["#store-selector-dropdown", "#store-selector-input", "kat-dropdown"]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.is_visible():
                text = await locator.text_content()
                if text and text.strip():
                    return text.strip()
        except Exception:
            continue
    return ""


async def discover_available_dropdown_stores(page: Page, settings: Settings | None = None) -> list[dict[str, str]]:
    _dropdown_trigger, overlay = await _open_store_dropdown(page, "Store Discovery")
    option_locator = overlay.locator("li[id^='store-selector-option-'], li.dropdown-option, li.dropdown-option-selected")

    option_rows: list[tuple[str | None, str | None]] = []
    for index in range(await option_locator.count()):
        option = option_locator.nth(index)
        option_rows.append((await option.get_attribute("id"), await option.text_content()))

    stores = _parse_available_store_options(option_rows, settings)
    if not stores:
        raise TimeoutError("Store selector dropdown contained no store options")

    app_logger.info(f"[Store Discovery] Found {len(stores)} live store(s) in the selector dropdown.")
    return stores


async def _select_option_via_overlay_text(page: Page, search_term: str, store_name: str, settings: Settings):
    overlay = await _visible_dropdown_overlay(page)
    if overlay is None:
        return False

    option_locator = overlay.get_by_text(search_term, exact=False).first
    if await option_locator.is_visible():
        selected_text = await option_locator.text_content()
        app_logger.info(f"[{store_name}] Clicking dropdown option: '{selected_text.strip()}'")
        await option_locator.click()
        return True

    overlay_texts = await overlay.locator("*").all_text_contents()
    candidate_texts: list[str] = []
    seen: set[str] = set()
    for text in overlay_texts:
        for part in re.split(r"[\n\r]+", text):
            cleaned = re.sub(r"\s+", " ", part).strip()
            if len(cleaned) < 3:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            candidate_texts.append(cleaned)

    target_norm = resolve_dropdown_name(search_term, settings)
    normalized_map = {resolve_dropdown_name(text, settings): text for text in candidate_texts if resolve_dropdown_name(text, settings)}
    matches = difflib.get_close_matches(target_norm, list(normalized_map.keys()), n=1, cutoff=0.55)
    if not matches:
        return False

    matched_option = normalized_map[matches[0]]
    app_logger.info(f"[{store_name}] Fuzzy match found: '{search_term}' -> '{matched_option}'")
    option_locator = overlay.get_by_text(matched_option, exact=False).first
    if await option_locator.is_visible():
        selected_text = await option_locator.text_content()
        app_logger.info(f"[{store_name}] Clicking dropdown option: '{selected_text.strip()}'")
        await option_locator.click()
        return True

    return False


async def _select_option_with_keyboard(
    page: Page,
    search_input,
    dropdown_name: str,
    store_name: str,
    settings: Settings,
):
    await search_input.press("ArrowDown")
    await search_input.press("Enter")
    await asyncio.sleep(1)

    selected_text = await _current_store_selector_text(page)
    if _selection_matches_target(selected_text, dropdown_name, store_name, settings):
        app_logger.info(f"[{store_name}] Selected via keyboard fallback: '{selected_text}'")
        return True
    return False


async def _select_option_without_search_input(
    page: Page,
    dropdown_name: str,
    store_name: str,
    settings: Settings,
) -> bool:
    for search_term in _build_search_terms(dropdown_name, store_name):
        try:
            if await _select_option_via_overlay_text(page, search_term, store_name, settings):
                selected_text = await _current_store_selector_text(page)
                if _selection_matches_target(selected_text, dropdown_name, store_name, settings):
                    app_logger.info(f"[{store_name}] Store selected from dropdown without search input.")
                    return True
        except Exception as exc:
            app_logger.debug(f"[{store_name}] Direct option selection failed for '{search_term}': {exc}")
    return False


async def _dump_dropdown_debug(page: Page, store_name: str, settings: Settings):
    try:
        overlay = await _visible_dropdown_overlay(page)
        if overlay is None:
            return

        os.makedirs(settings.output_path("debug"), exist_ok=True)
        safe_name = re.sub(r'[\\/*?:"<>|]', "_", store_name)
        debug_path = settings.output_path("debug", f"dropdown_{safe_name}.html")
        html = await overlay.inner_html()
        with open(debug_path, "w", encoding="utf-8") as file_handle:
            file_handle.write(html)
        app_logger.info(f"[{store_name}] Saved dropdown debug HTML to {debug_path}")
    except Exception as exc:
        app_logger.warning(f"[{store_name}] Failed to save dropdown debug HTML: {exc}")


def _canonicalize_summation_metrics_url(api_url: str) -> str:
    canonical_url = api_url.replace("/api/metrics?", "/api/summationMetrics?")
    canonical_url = canonical_url.replace("/metrics?", "/summationMetrics?")
    return canonical_url


def _build_generic_api_template(request_url: str) -> str:
    canonical_url = _canonicalize_summation_metrics_url(request_url)
    return re.sub(
        r"merchantIds(?:%5B%5D|\[\])=[^&]*",
        "merchantIds%5B%5D={merchant_id}",
        canonical_url,
    )


def _extract_merchant_ids_from_url(request_url: str) -> list[str]:
    parsed = urllib.parse.urlparse(request_url)
    params = urllib.parse.parse_qs(parsed.query)
    return [merchant_id.strip() for merchant_id in (params.get("merchantIds[]") or params.get("merchantIds") or [])]


def _is_metrics_response_for_merchant(response, expected_merchant_id: str = "") -> bool:
    if response.status != 200 or not any(key in response.url for key in ["summationMetrics", "api/metrics"]):
        return False

    if not expected_merchant_id:
        return True

    merchant_ids = _extract_merchant_ids_from_url(response.url)
    return not merchant_ids or expected_merchant_id in merchant_ids


def _build_fast_path_target_url(api_url_template: str, merchant_id: str, settings: Settings) -> str:
    detail_template = _canonicalize_summation_metrics_url(api_url_template)
    current_hour = datetime.now(settings.local_timezone).hour
    if "endRange%5Bhour%5D=" in detail_template:
        detail_template = re.sub(r"endRange%5Bhour%5D=\d+", f"endRange%5Bhour%5D={current_hour}", detail_template)
    return detail_template.replace("{merchant_id}", merchant_id)


def build_store_submission(store_name: str, api_data: list[dict] | dict, settings: Settings) -> tuple[dict[str, str], dict[str, float]]:
    normalized_metrics = normalize_metrics_payload(api_data)
    form_data = build_form_data(store_name, normalized_metrics, local_timezone=settings.local_timezone)
    return form_data, normalized_metrics


def _fast_path_warmup_window(state: ScraperState) -> int:
    settings = state.settings
    worker_pool_size = state.browser_worker_pool_size or settings.fast_path_max_concurrency
    scaled_window = min((worker_pool_size + 1) // 2, 6)
    return max(settings.fast_path_warmup_requests, scaled_window)


async def _wait_for_fast_path_backpressure(state: ScraperState, store_name: str):
    async with state.fast_path_backoff_lock:
        wait_seconds = max(state.fast_path_backoff_until - asyncio.get_running_loop().time(), 0.0)

    if wait_seconds > 0:
        app_logger.info(f"[{store_name}] Fast Path transient cooldown {wait_seconds:.2f}s before next API request.")
        await asyncio.sleep(wait_seconds)


async def _apply_fast_path_backpressure(state: ScraperState, delay_seconds: float):
    async with state.fast_path_backoff_lock:
        state.fast_path_backoff_until = max(
            state.fast_path_backoff_until,
            asyncio.get_running_loop().time() + delay_seconds,
        )


async def fetch_metrics_fast_path(request_client, target_url: str, store_name: str, state: ScraperState, timeout: int):
    settings = state.settings
    async with state.fast_path_semaphore:
        async with state.fast_path_lock:
            request_index = state.fast_path_started_count
            state.fast_path_started_count += 1

        warmup_window = _fast_path_warmup_window(state)
        if request_index < warmup_window:
            warmup_delay = (request_index * settings.fast_path_warmup_delay_ms) / 1000
            if warmup_delay > 0:
                app_logger.info(
                    f"[{store_name}] Fast Path warm-up delay {warmup_delay:.2f}s before initial API request."
                )
                await asyncio.sleep(warmup_delay)

        for api_attempt in range(settings.fast_path_retry_count):
            await _wait_for_fast_path_backpressure(state, store_name)
            resp = await request_client.get(target_url, timeout=timeout)
            if resp.status == 200:
                return await resp.json()

            if resp.status in TRANSIENT_FAST_PATH_STATUSES and api_attempt < settings.fast_path_retry_count - 1:
                delay_seconds = (settings.fast_path_retry_base_delay_ms / 1000) * (2**api_attempt)
                app_logger.warning(
                    f"[{store_name}] API returned {resp.status}. Applying shared fast-path cooldown of {delay_seconds:.1f}s before retry."
                )
                await _apply_fast_path_backpressure(state, delay_seconds)
                continue

            raise RuntimeError(f"API Fetch failed: {resp.status}")


async def _fetch_metrics_fast_path(page: Page, target_url: str, store_name: str, state: ScraperState, timeout: int):
    return await fetch_metrics_fast_path(page.context.request, target_url, store_name, state, timeout)


async def select_store_from_dropdown(page: Page, dropdown_name: str, store_name: str, settings: Settings):
    app_logger.info(f"[{store_name}] Selecting store from dropdown matching: {dropdown_name}")

    dropdown_trigger, _overlay = await _open_store_dropdown(page, store_name)

    search_input = page.locator(
        'kat-popover input:visible, kat-dropdown-menu input:visible, .dropdown-search input, #store-selector-input input, .store-selector-input input, input[id^="katal-id-"]:visible:not(#katal-id-0, [placeholder*="shoppers" i]), kat-input[placeholder*="Search"]:not([placeholder*="shoppers" i]) input'
    )
    try:
        await expect(search_input.first).to_be_visible(timeout=20000)
    except Exception:
        app_logger.info(f"[{store_name}] Search input not found, attempting to find any visible dropdown-related input.")
        search_input = page.locator(
            'input:visible:not(#katal-id-0, [placeholder*="shoppers" i]), .dropdown-list-container input'
        ).first
        try:
            if not await search_input.is_visible():
                app_logger.warning(f"[{store_name}] No search input found at all. Proceeding to direct option selection.")
                search_input = None
        except Exception:
            app_logger.warning(f"[{store_name}] No search input found at all. Proceeding to direct option selection.")
            search_input = None

    if search_input is None and await _select_option_without_search_input(page, dropdown_name, store_name, settings):
        return True

    if search_input:
        await search_input.first.click()
    search_terms = _build_search_terms(dropdown_name, store_name)
    for index, search_term in enumerate(search_terms):
        if search_input:
            await search_input.first.fill(search_term)
            await asyncio.sleep(1.5)

        try:
            if await _select_option_via_overlay_text(page, search_term, store_name, settings):
                selected_text = await _current_store_selector_text(page)
                if _selection_matches_target(selected_text, dropdown_name, store_name, settings):
                    app_logger.info(f"[{store_name}] Store selected from dropdown.")
                    return True
        except Exception as exc:
            app_logger.debug(f"[{store_name}] Text-based option selection failed for '{search_term}': {exc}")

        if search_input:
            try:
                if await _select_option_with_keyboard(page, search_input.first, dropdown_name, store_name, settings):
                    app_logger.info(f"[{store_name}] Store selected from dropdown.")
                    return True
            except Exception as exc:
                app_logger.debug(f"[{store_name}] Keyboard fallback failed for '{search_term}': {exc}")

        if index < len(search_terms) - 1:
            await dropdown_trigger.first.click(force=True)
            await asyncio.sleep(0.5)

    await _dump_dropdown_debug(page, store_name, settings)
    app_logger.warning(f"[{store_name}] No dropdown options appeared or matched for '{dropdown_name}' after search.")
    raise TimeoutError(f"No dropdown option matched '{dropdown_name}'")


async def collect_metrics_via_ui(page: Page, work_item: WorkItem, state: ScraperState, timeout: int = METRICS_TIMEOUT):
    settings = state.settings
    store_name = work_item.store_name
    dropdown_name = resolve_dropdown_name(work_item.dropdown_name, settings)
    expected_merchant_id = work_item.merchant_id.strip()

    dropdown_trigger = page.locator("#store-selector-dropdown")
    if not page.url.startswith(settings.base_dashboard_url) or not await dropdown_trigger.is_visible():
        app_logger.info(f"[{store_name}] Dashboard trigger not visible or URL is wrong. Navigating...")
        await page.goto(settings.base_dashboard_url, timeout=settings.page_timeout_ms, wait_until="domcontentloaded")

    await select_store_from_dropdown(page, dropdown_name, store_name, settings)

    refresh_button = page.get_by_role("button", name="Refresh")
    async with page.expect_response(
        lambda response: _is_metrics_response_for_merchant(response, expected_merchant_id),
        timeout=timeout,
    ) as response_info:
        await expect(refresh_button).to_be_visible(timeout=settings.wait_timeout_ms)
        await refresh_button.first.dispatch_event("click")

    response = await response_info.value
    api_data = await response.json()
    request_url = response.url
    summary_url = _canonicalize_summation_metrics_url(request_url)
    if summary_url != request_url:
        try:
            summary_response = await page.context.request.get(summary_url, timeout=timeout)
            if summary_response.status == 200:
                api_data = await summary_response.json()
                request_url = summary_response.url
                app_logger.info(f"[{store_name}] Refetched summationMetrics for canonical store totals.")
            else:
                app_logger.warning(
                    f"[{store_name}] Summation metrics refetch returned {summary_response.status}; using original response."
                )
        except Exception as exc:
            app_logger.warning(f"[{store_name}] Failed to refetch summation metrics: {exc}")

    parsed = urllib.parse.urlparse(request_url)
    params = urllib.parse.parse_qs(parsed.query)
    cache_updated = False

    if "summationMetrics" in request_url or "api/metrics" in request_url:
        generic_url = _build_generic_api_template(request_url)
        if state.cache.set_api_url_template(generic_url):
            cache_updated = True
            app_logger.info(f"[{store_name}] Discovery: Captured API Template: {generic_url[:100]}...")

    captured_mids = params.get("merchantIds[]") or params.get("merchantIds")
    if captured_mids:
        captured_mid = captured_mids[0]
        work_item.merchant_id = captured_mid
        if state.cache.set_merchant_id(store_name, captured_mid):
            cache_updated = True
            app_logger.info(f"[{store_name}] Discovery: Discovered internal Merchant ID: {captured_mid}")

    if cache_updated:
        await state.cache.save()

    return api_data


async def process_fast_path_store(
    request_client,
    work_item: WorkItem,
    ui_queue: asyncio.Queue,
    submission_manager: SubmissionManager | None,
    state: ScraperState,
    submission_queue: asyncio.Queue | None = None,
):
    start_ts = asyncio.get_running_loop().time()
    store_name = work_item.store_name

    try:
        if submission_queue is None and submission_manager is not None and not hasattr(submission_manager, "enqueue_submission"):
            submission_queue = submission_manager
            submission_manager = None
        submission_target = submission_manager or (_QueueSubmissionAdapter(submission_queue) if submission_queue else None)
        if submission_target is None:
            raise RuntimeError("A submission manager or submission queue is required")

        if not state.cache.api_url_template or not work_item.merchant_id:
            raise RuntimeError("Fast-path route requires both an API template and a merchant ID")

        target_url = _build_fast_path_target_url(state.cache.api_url_template, work_item.merchant_id, state.settings)
        api_data = await fetch_metrics_fast_path(request_client, target_url, store_name, state, METRICS_TIMEOUT)
        app_logger.info(f"[{store_name}] API data fetched successfully (Fast Path).")

        form_data, normalized_metrics = build_store_submission(store_name, api_data, state.settings)
        await submission_target.enqueue_submission(form_data)

        duration = asyncio.get_running_loop().time() - start_ts
        await state.record_metric(
            store_name,
            duration,
            int(normalized_metrics.get("OrdersShopped_V2", 0)),
            int(normalized_metrics.get("RequestedQuantity_V2", 0)),
            path="fast_path",
        )
        app_logger.info(f"[{store_name}] Data collection complete ({duration:.2f}s).")
    except Exception as api_error:
        app_logger.warning(f"[{store_name}] Fast Path failed: {api_error}. Requeueing to UI.")
        await state.record_issue(
            f"{store_name} (API Fast Path Fallback)",
            asyncio.get_running_loop().time(),
            category="api_fast_path",
        )
        if not work_item.force_ui:
            work_item.force_ui = True
            await state.record_fast_path_requeue()
            await ui_queue.put(work_item)


async def process_ui_store(
    page: Page,
    work_item: WorkItem,
    submission_manager: SubmissionManager | None,
    state: ScraperState,
    submission_queue: asyncio.Queue | None = None,
):
    start_ts = asyncio.get_running_loop().time()
    store_name = work_item.store_name

    for attempt in range(state.settings.worker_retry_count):
        current_stage = "ui_fallback"
        try:
            if submission_queue is None and submission_manager is not None and not hasattr(submission_manager, "enqueue_submission"):
                submission_queue = submission_manager
                submission_manager = None
            submission_target = submission_manager or (_QueueSubmissionAdapter(submission_queue) if submission_queue else None)
            if submission_target is None:
                raise RuntimeError("A submission manager or submission queue is required")

            api_data = await collect_metrics_via_ui(page, work_item, state, timeout=METRICS_TIMEOUT)

            current_stage = "general"
            form_data, normalized_metrics = build_store_submission(store_name, api_data, state.settings)
            await submission_target.enqueue_submission(form_data)

            duration = asyncio.get_running_loop().time() - start_ts
            await state.record_metric(
                store_name,
                duration,
                int(normalized_metrics.get("OrdersShopped_V2", 0)),
                int(normalized_metrics.get("RequestedQuantity_V2", 0)),
                path="ui",
            )

            app_logger.info(f"[{store_name}] Data collection complete ({duration:.2f}s).")
            return
        except Exception as exc:
            app_logger.warning(f"[{store_name}] Failed attempt {attempt + 1}: {exc}")
            if attempt < state.settings.worker_retry_count - 1:
                await state.record_retry(store_name)
                sleep_time = 2**attempt
                try:
                    await page.goto(
                        state.settings.base_dashboard_url,
                        timeout=state.settings.page_timeout_ms,
                        wait_until="domcontentloaded",
                    )
                except Exception:
                    pass
                await asyncio.sleep(sleep_time)
            else:
                failure_reason_by_stage = {
                    "ui_fallback": "UI Fallback Failure",
                    "general": "Store Processing Failure",
                }
                await state.add_failure(
                    f"{store_name} ({failure_reason_by_stage.get(current_stage, 'Store Processing Failure')})",
                    asyncio.get_running_loop().time(),
                    category=current_stage,
                )
                await save_screenshot(page, f"process_fail_{store_name}", state.settings)


async def process_single_store(
    page: Page,
    store_info: dict[str, str],
    submission_manager: SubmissionManager | None,
    state: ScraperState,
    submission_queue: asyncio.Queue | None = None,
):
    merchant_id = store_info.get("merchant_id") or state.cache.merchant_id_cache.get(store_info["store_name"], "")
    work_item = WorkItem.from_store_info(store_info, merchant_id=merchant_id)
    await process_ui_store(page, work_item, submission_manager, state, submission_queue=submission_queue)
