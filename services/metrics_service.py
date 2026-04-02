import asyncio
import difflib
import os
import re
import urllib.parse
from datetime import datetime

from playwright.async_api import Page, TimeoutError, expect

from core.config import (
    BASE_DASHBOARD_URL,
    FAST_PATH_MAX_CONCURRENCY,
    FAST_PATH_RETRY_BASE_DELAY_MS,
    FAST_PATH_RETRY_COUNT,
    FAST_PATH_WARMUP_DELAY_MS,
    FAST_PATH_WARMUP_REQUESTS,
    LOCAL_TIMEZONE,
    OUTPUT_DIR,
    PAGE_TIMEOUT,
    SPECIAL_NAME_MAPPINGS,
    WAIT_TIMEOUT,
    WORKER_RETRY_COUNT,
)
from core.logger import app_logger
from core.metrics import build_form_data, normalize_metrics_payload
from core.state import ScraperState
from core.utils import normalize_name, save_screenshot
from core.work_items import WorkItem

METRICS_TIMEOUT = 45_000
TRANSIENT_FAST_PATH_STATUSES = {503, 504}


def resolve_dropdown_name(store_name: str) -> str:
    dropdown_name = normalize_name(store_name)

    for key, val in SPECIAL_NAME_MAPPINGS.items():
        if key in dropdown_name:
            return val

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


def _selection_matches_target(selected_text: str, dropdown_name: str, store_name: str) -> bool:
    selected_norm = normalize_name(selected_text)
    candidate_norms = {normalize_name(dropdown_name), normalize_name(store_name)}

    for candidate in candidate_norms:
        if not candidate:
            continue
        if candidate in selected_norm or selected_norm in candidate:
            return True
        if difflib.SequenceMatcher(None, selected_norm, candidate).ratio() >= 0.72:
            return True
    return False


def _parse_available_store_options(option_rows: list[tuple[str | None, str | None]]) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen: set[str] = set()

    for option_id, option_text in option_rows:
        cleaned_text = re.sub(r"\s+", " ", option_text or "").strip()
        normalized_name = normalize_name(cleaned_text)
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
    selectors = [
        "#store-selector-dropdown",
        "#store-selector-input",
        "kat-dropdown",
    ]
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


async def discover_available_dropdown_stores(page: Page) -> list[dict[str, str]]:
    _dropdown_trigger, overlay = await _open_store_dropdown(page, "Store Discovery")
    option_locator = overlay.locator("li[id^='store-selector-option-'], li.dropdown-option, li.dropdown-option-selected")

    option_rows: list[tuple[str | None, str | None]] = []
    for index in range(await option_locator.count()):
        option = option_locator.nth(index)
        option_rows.append((await option.get_attribute("id"), await option.text_content()))

    stores = _parse_available_store_options(option_rows)
    if not stores:
        raise TimeoutError("Store selector dropdown contained no store options")

    app_logger.info(f"[Store Discovery] Found {len(stores)} live store(s) in the selector dropdown.")
    return stores


async def _select_option_via_overlay_text(page: Page, search_term: str, store_name: str):
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

    target_norm = normalize_name(search_term)
    normalized_map = {normalize_name(text): text for text in candidate_texts if normalize_name(text)}
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


async def _select_option_with_keyboard(page: Page, search_input, dropdown_name: str, store_name: str):
    await search_input.press("ArrowDown")
    await search_input.press("Enter")
    await asyncio.sleep(1)

    selected_text = await _current_store_selector_text(page)
    if _selection_matches_target(selected_text, dropdown_name, store_name):
        app_logger.info(f"[{store_name}] Selected via keyboard fallback: '{selected_text}'")
        return True
    return False


async def _dump_dropdown_debug(page: Page, store_name: str):
    try:
        overlay = await _visible_dropdown_overlay(page)
        if overlay is None:
            return

        os.makedirs(os.path.join(OUTPUT_DIR, "debug"), exist_ok=True)
        safe_name = re.sub(r'[\\/*?:"<>|]', "_", store_name)
        debug_path = os.path.join(OUTPUT_DIR, "debug", f"dropdown_{safe_name}.html")
        html = await overlay.inner_html()
        with open(debug_path, "w", encoding="utf-8") as file_handle:
            file_handle.write(html)
        app_logger.info(f"[{store_name}] Saved dropdown debug HTML to {debug_path}")
    except Exception as exc:
        app_logger.warning(f"[{store_name}] Failed to save dropdown debug HTML: {exc}")


def _build_fast_path_target_url(api_url_template: str, merchant_id: str) -> str:
    detail_template = api_url_template.replace("/summationMetrics?", "/metrics?")
    current_hour = datetime.now(LOCAL_TIMEZONE).hour
    if "endRange%5Bhour%5D=" in detail_template:
        detail_template = re.sub(r"endRange%5Bhour%5D=\d+", f"endRange%5Bhour%5D={current_hour}", detail_template)
    return detail_template.replace("{merchant_id}", merchant_id)


def build_store_submission(store_name: str, api_data: list[dict] | dict) -> tuple[dict[str, str], dict[str, float]]:
    normalized_metrics = normalize_metrics_payload(api_data)
    form_data = build_form_data(store_name, normalized_metrics)
    return form_data, normalized_metrics


def _fast_path_warmup_window(state: ScraperState) -> int:
    worker_pool_size = state.browser_worker_pool_size or FAST_PATH_MAX_CONCURRENCY
    return max(FAST_PATH_WARMUP_REQUESTS, min(worker_pool_size, FAST_PATH_MAX_CONCURRENCY))


async def _wait_for_fast_path_backpressure(state: ScraperState, store_name: str):
    async with state.fast_path_backoff_lock:
        wait_seconds = max(state.fast_path_backoff_until - asyncio.get_running_loop().time(), 0.0)

    if wait_seconds > 0:
        app_logger.info(
            f"[{store_name}] Fast Path transient cooldown {wait_seconds:.2f}s before next API request."
        )
        await asyncio.sleep(wait_seconds)


async def _apply_fast_path_backpressure(state: ScraperState, delay_seconds: float):
    async with state.fast_path_backoff_lock:
        state.fast_path_backoff_until = max(
            state.fast_path_backoff_until,
            asyncio.get_running_loop().time() + delay_seconds,
        )


async def fetch_metrics_fast_path(request_client, target_url: str, store_name: str, state: ScraperState, timeout: int):
    async with state.fast_path_semaphore:
        async with state.fast_path_lock:
            request_index = state.fast_path_started_count
            state.fast_path_started_count += 1

        warmup_window = _fast_path_warmup_window(state)
        if request_index < warmup_window:
            warmup_delay = (request_index * FAST_PATH_WARMUP_DELAY_MS) / 1000
            if warmup_delay > 0:
                app_logger.info(
                    f"[{store_name}] Fast Path warm-up delay {warmup_delay:.2f}s before initial API request."
                )
                await asyncio.sleep(warmup_delay)

        for api_attempt in range(FAST_PATH_RETRY_COUNT):
            await _wait_for_fast_path_backpressure(state, store_name)
            resp = await request_client.get(target_url, timeout=timeout)
            if resp.status == 200:
                return await resp.json()

            if resp.status in TRANSIENT_FAST_PATH_STATUSES and api_attempt < FAST_PATH_RETRY_COUNT - 1:
                delay_seconds = (FAST_PATH_RETRY_BASE_DELAY_MS / 1000) * (2**api_attempt)
                app_logger.warning(
                    f"[{store_name}] API returned {resp.status}. Applying shared fast-path cooldown of {delay_seconds:.1f}s before retry."
                )
                await _apply_fast_path_backpressure(state, delay_seconds)
                continue

            raise Exception(f"API Fetch failed: {resp.status}")


async def _fetch_metrics_fast_path(page: Page, target_url: str, store_name: str, state: ScraperState, timeout: int):
    return await fetch_metrics_fast_path(page.context.request, target_url, store_name, state, timeout)


async def select_store_from_dropdown(page: Page, dropdown_name: str, store_name: str):
    app_logger.info(f"[{store_name}] Selecting store from dropdown matching: {dropdown_name}")

    dropdown_trigger, _overlay = await _open_store_dropdown(page, store_name)

    search_input = page.locator(
        'kat-popover input:visible, kat-dropdown-menu input:visible, .dropdown-search input, #store-selector-input input, .store-selector-input input, input[id^="katal-id-"]:visible:not(#katal-id-0, [placeholder*="shoppers" i]), kat-input[placeholder*="Search"]:not([placeholder*="shoppers" i]) input'
    )
    try:
        await expect(search_input.first).to_be_visible(timeout=20000)
    except TimeoutError:
        app_logger.info(
            f"[{store_name}] Search input not found, attempting to find any visible dropdown-related input."
        )
        search_input = page.locator(
            'input:visible:not(#katal-id-0, [placeholder*="shoppers" i]), .dropdown-list-container input'
        ).first
        if not await search_input.is_visible():
            app_logger.warning(f"[{store_name}] No search input found at all. Proceeding to direct option selection.")
            search_input = None

    if search_input:
        await search_input.first.click()
    search_terms = _build_search_terms(dropdown_name, store_name)
    for index, search_term in enumerate(search_terms):
        if search_input:
            await search_input.first.fill(search_term)
            await asyncio.sleep(1.5)

        try:
            if await _select_option_via_overlay_text(page, search_term, store_name):
                selected_text = await _current_store_selector_text(page)
                if _selection_matches_target(selected_text, dropdown_name, store_name):
                    app_logger.info(f"[{store_name}] Store selected from dropdown.")
                    return True
        except Exception as exc:
            app_logger.debug(f"[{store_name}] Text-based option selection failed for '{search_term}': {exc}")

        if search_input:
            try:
                if await _select_option_with_keyboard(page, search_input.first, dropdown_name, store_name):
                    app_logger.info(f"[{store_name}] Store selected from dropdown.")
                    return True
            except Exception as exc:
                app_logger.debug(f"[{store_name}] Keyboard fallback failed for '{search_term}': {exc}")

        if index < len(search_terms) - 1:
            await dropdown_trigger.first.click(force=True)
            await asyncio.sleep(0.5)

    await _dump_dropdown_debug(page, store_name)
    app_logger.warning(f"[{store_name}] No dropdown options appeared or matched for '{dropdown_name}' after search.")
    raise TimeoutError(f"No dropdown option matched '{dropdown_name}'")


async def collect_metrics_via_ui(page: Page, work_item: WorkItem, state: ScraperState, timeout: int = METRICS_TIMEOUT):
    store_name = work_item.store_name
    dropdown_name = resolve_dropdown_name(work_item.dropdown_name)

    dropdown_trigger = page.locator("#store-selector-dropdown")
    if not page.url.startswith(BASE_DASHBOARD_URL) or not await dropdown_trigger.is_visible():
        app_logger.info(f"[{store_name}] Dashboard trigger not visible or URL is wrong. Navigating...")
        await page.goto(BASE_DASHBOARD_URL, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")

    await select_store_from_dropdown(page, dropdown_name, store_name)

    refresh_button = page.get_by_role("button", name="Refresh")
    async with page.expect_response(
        lambda r: any(k in r.url for k in ["summationMetrics", "api/metrics"]) and r.status == 200,
        timeout=timeout,
    ) as resp_info:
        await expect(refresh_button).to_be_visible(timeout=WAIT_TIMEOUT)
        await refresh_button.first.dispatch_event("click")

    response = await resp_info.value
    api_data = await response.json()

    req_url = response.url
    parsed = urllib.parse.urlparse(req_url)
    params = urllib.parse.parse_qs(parsed.query)
    cache_updated = False

    if "summationMetrics" in req_url or "api/metrics" in req_url:
        generic_url = re.sub(r"merchantIds%5B%5D=[^&]*", "merchantIds%5B%5D={merchant_id}", req_url)
        async with state.cache.lock:
            if not state.cache.api_url_template:
                state.cache.api_url_template = generic_url
                cache_updated = True
                app_logger.info(
                    f"[{store_name}] Discovery: Captured API Template: {state.cache.api_url_template[:100]}..."
                )

    captured_mids = params.get("merchantIds[]") or params.get("merchantIds")
    if captured_mids:
        captured_mid = captured_mids[0]
        work_item.merchant_id = captured_mid
        async with state.cache.lock:
            if state.cache.merchant_id_cache.get(store_name) != captured_mid:
                state.cache.merchant_id_cache[store_name] = captured_mid
                cache_updated = True
                app_logger.info(f"[{store_name}] Discovery: Discovered internal Merchant ID: {captured_mid}")

    if cache_updated:
        await state.cache.save()

    return api_data


async def process_fast_path_store(
    request_client,
    work_item: WorkItem,
    ui_queue: asyncio.Queue,
    submission_queue: asyncio.Queue,
    state: ScraperState,
):
    start_ts = asyncio.get_running_loop().time()
    store_name = work_item.store_name

    try:
        if not state.cache.api_url_template or not work_item.merchant_id:
            raise RuntimeError("Fast-path route requires both an API template and a merchant ID")

        target_url = _build_fast_path_target_url(state.cache.api_url_template, work_item.merchant_id)
        api_data = await fetch_metrics_fast_path(request_client, target_url, store_name, state, METRICS_TIMEOUT)
        app_logger.info(f"[{store_name}] API Data fetched successfully (Fast Path).")

        form_data, normalized_metrics = build_store_submission(store_name, api_data)
        app_logger.info(f"[{store_name}] 'Late Picks' extracted from API JSON: {form_data['lates']}")
        await submission_queue.put(form_data)

        duration = asyncio.get_running_loop().time() - start_ts
        await state.record_metric(
            store_name,
            duration,
            int(normalized_metrics.get("OrdersShopped_V2", 0)),
            int(normalized_metrics.get("RequestedQuantity_V2", 0)),
            path="fast_path",
        )
        app_logger.info(f"[{store_name}] Data collection complete ({duration:.2f}s).")
    except Exception as api_err:
        app_logger.warning(f"[{store_name}] Fast Path failed: {api_err}. Requeueing to UI.")
        await state.record_issue(
            f"{store_name} (API Fast Path Fallback)",
            asyncio.get_running_loop().time(),
            category="api_fast_path",
        )
        if not work_item.force_ui:
            work_item.force_ui = True
            await state.record_fast_path_requeue()
            await ui_queue.put(work_item)


async def process_ui_store(page: Page, work_item: WorkItem, submission_queue: asyncio.Queue, state: ScraperState):
    start_ts = asyncio.get_running_loop().time()
    store_name = work_item.store_name

    for attempt in range(WORKER_RETRY_COUNT):
        current_stage = "ui_fallback"
        try:
            api_data = await collect_metrics_via_ui(page, work_item, state, timeout=METRICS_TIMEOUT)

            current_stage = "general"
            form_data, normalized_metrics = build_store_submission(store_name, api_data)
            app_logger.info(f"[{store_name}] 'Late Picks' extracted from API JSON: {form_data['lates']}")
            await submission_queue.put(form_data)

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
            if attempt < WORKER_RETRY_COUNT - 1:
                await state.record_retry(store_name)
                sleep_time = 2**attempt
                app_logger.info(f"[{store_name}] Retrying {store_name} on attempt {attempt + 2}...")
                try:
                    await page.goto(BASE_DASHBOARD_URL, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
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
                await save_screenshot(page, f"process_fail_{store_name}")


async def process_single_store(
    page: Page, store_info: dict[str, str], submission_queue: asyncio.Queue, state: ScraperState
):
    merchant_id = store_info.get("merchant_id") or state.cache.merchant_id_cache.get(store_info["store_name"], "")
    work_item = WorkItem.from_store_info(store_info, merchant_id=merchant_id)
    await process_ui_store(page, work_item, submission_queue, state)
