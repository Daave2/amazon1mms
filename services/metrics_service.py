import asyncio
import re
import urllib.parse
from datetime import datetime

from playwright.async_api import Page, TimeoutError, expect

from core.config import (
    BASE_DASHBOARD_URL,
    LOCAL_TIMEZONE,
    PAGE_TIMEOUT,
    SPECIAL_NAME_MAPPINGS,
    WAIT_TIMEOUT,
    WORKER_RETRY_COUNT,
)
from core.logger import app_logger
from core.metrics import build_form_data, normalize_metrics_payload
from core.state import ScraperState
from core.utils import normalize_name, save_screenshot


async def select_store_from_dropdown(page: Page, dropdown_name: str, store_name: str):
    app_logger.info(f"[{store_name}] Selecting store from dropdown matching: {dropdown_name}")

    dropdown_trigger = page.locator("#store-selector-dropdown")
    try:
        await expect(dropdown_trigger).to_be_visible(timeout=30000)
        for _ in range(3):
            await dropdown_trigger.first.click(force=True)
            await asyncio.sleep(1)
            if await page.locator("kat-popover input, kat-dropdown-menu input, .dropdown-list").first.is_visible():
                break
    except Exception as e:
        app_logger.warning(f"[{store_name}] Failed to click dropdown trigger: {e}")
        await page.get_by_text("Select a store").first.click(force=True)

    await asyncio.sleep(2)

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
        await search_input.first.fill(dropdown_name)
        await asyncio.sleep(2)

    try:
        option_locator = page.get_by_text(dropdown_name, exact=False).first
        if not await option_locator.is_visible():
            app_logger.info(
                f"[{store_name}] No direct match for '{dropdown_name}'. Attempting fuzzy normalized match..."
            )
            options = page.locator('.dropdown-option, [role="option"], kat-option, .kat-option')
            all_options = await options.all_text_contents()
            if all_options:
                import difflib

                target_norm = normalize_name(dropdown_name)
                norm_map = {normalize_name(opt): opt for opt in all_options}
                matches = difflib.get_close_matches(target_norm, list(norm_map.keys()), n=1, cutoff=0.3)
                if matches:
                    matched_option = norm_map[matches[0]]
                    app_logger.info(f"[{store_name}] Fuzzy match found: '{dropdown_name}' -> '{matched_option}'")
                    option_locator = options.filter(has_text=re.compile(re.escape(matched_option), re.I)).first

        await expect(option_locator).to_be_visible(timeout=5000)
        selected_text = await option_locator.text_content()
        app_logger.info(f"[{store_name}] Clicking dropdown option: '{selected_text.strip()}'")
        await option_locator.click()
    except TimeoutError:
        app_logger.warning(
            f"[{store_name}] No dropdown options appeared or matched for '{dropdown_name}' after search."
        )
        raise

    app_logger.info(f"[{store_name}] Store selected from dropdown.")
    return True


async def process_single_store(
    page: Page, store_info: dict[str, str], submission_queue: asyncio.Queue, state: ScraperState
):
    start_ts = asyncio.get_running_loop().time()
    store_name = store_info["store_name"]
    formatted_name = normalize_name(store_name)
    dropdown_name = formatted_name

    for key, val in SPECIAL_NAME_MAPPINGS.items():
        if key in dropdown_name:
            dropdown_name = val
            break

    merchant_id = store_info.get("merchant_id") or state.cache.merchant_id_cache.get(store_name)
    METRICS_TIMEOUT = 45_000

    for attempt in range(WORKER_RETRY_COUNT):
        current_stage = "general"
        try:
            api_data = None
            if state.cache.api_url_template and merchant_id:
                current_stage = "api_fast_path"
                try:
                    detail_template = state.cache.api_url_template.replace("/summationMetrics?", "/metrics?")
                    current_hour = datetime.now(LOCAL_TIMEZONE).hour
                    detail_template = re.sub(
                        r"endRange%5Bhour%5D=\d+", f"endRange%5Bhour%5D={current_hour}", detail_template
                    )
                    target_url = detail_template.replace("{merchant_id}", merchant_id)

                    for api_attempt in range(2):
                        resp = await page.context.request.get(target_url, timeout=METRICS_TIMEOUT)
                        if resp.status == 200:
                            api_data = await resp.json()
                            app_logger.info(f"[{store_name}] API Data fetched successfully (Fast Path).")
                            break
                        elif resp.status == 504 and api_attempt == 0:
                            app_logger.warning(f"[{store_name}] API returned 504. Retrying in 2s...")
                            await asyncio.sleep(2)
                            continue
                        else:
                            raise Exception(f"API Fetch failed: {resp.status}")
                except Exception as api_err:
                    app_logger.warning(f"[{store_name}] Fast Path failed: {api_err}. Falling back to UI.")
                    await state.record_issue(
                        f"{store_name} (API Fast Path Fallback)",
                        asyncio.get_running_loop().time(),
                        category="api_fast_path",
                    )
                    api_data = None

            if not api_data:
                current_stage = "ui_fallback"
                dropdown_trigger = page.locator("#store-selector-dropdown")
                if not page.url.startswith(BASE_DASHBOARD_URL) or not await dropdown_trigger.is_visible():
                    app_logger.info(f"[{store_name}] Dashboard trigger not visible or URL is wrong. Navigating...")
                    await page.goto(BASE_DASHBOARD_URL, timeout=PAGE_TIMEOUT, wait_until="networkidle")

                await select_store_from_dropdown(page, dropdown_name, store_name)

                refresh_button = page.get_by_role("button", name="Refresh")
                async with page.expect_response(
                    lambda r: any(k in r.url for k in ["summationMetrics", "api/metrics"]) and r.status == 200,
                    timeout=METRICS_TIMEOUT,
                ) as resp_info:
                    await expect(refresh_button).to_be_visible(timeout=WAIT_TIMEOUT)
                    await refresh_button.first.dispatch_event("click")

                response = await resp_info.value
                api_data = await response.json()

                req_url = response.url
                parsed = urllib.parse.urlparse(req_url)
                params = urllib.parse.parse_qs(parsed.query)

                if ("summationMetrics" in req_url or "api/metrics" in req_url) and not state.cache.api_url_template:
                    async with state.cache.lock:
                        generic_url = re.sub(r"merchantIds%5B%5D=[^&]*", "merchantIds%5B%5D={merchant_id}", req_url)
                        state.cache.api_url_template = generic_url
                        app_logger.info(
                            f"[{store_name}] Discovery: Captured API Template: {state.cache.api_url_template[:100]}..."
                        )

                captured_mids = params.get("merchantIds[]") or params.get("merchantIds")
                if captured_mids and len(captured_mids) > 0:
                    captured_mid = captured_mids[0]
                    merchant_id = captured_mid

                    if state.cache.merchant_id_cache.get(store_name) != captured_mid:
                        async with state.cache.lock:
                            state.cache.merchant_id_cache[store_name] = captured_mid
                        app_logger.info(f"[{store_name}] Discovery: Discovered internal Merchant ID: {captured_mid}")

                    await state.cache.save()

            current_stage = "general"
            data_to_use = normalize_metrics_payload(api_data)
            form_data = build_form_data(store_name, data_to_use)
            app_logger.info(f"[{store_name}] 'Late Picks' extracted from API JSON: {form_data['lates']}")
            await submission_queue.put(form_data)

            duration = asyncio.get_running_loop().time() - start_ts
            await state.record_metric(
                store_name,
                duration,
                int(data_to_use.get("OrdersShopped_V2", 0)),
                int(data_to_use.get("RequestedQuantity_V2", 0)),
            )

            app_logger.info(f"[{store_name}] Data collection complete ({duration:.2f}s).")
            return

        except Exception as e:
            app_logger.warning(f"[{store_name}] Failed attempt {attempt + 1}: {e}")
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
                    "api_fast_path": "API Fast Path Failure",
                    "ui_fallback": "UI Fallback Failure",
                    "general": "Store Processing Failure",
                }
                await state.add_failure(
                    f"{store_name} ({failure_reason_by_stage.get(current_stage, 'Store Processing Failure')})",
                    asyncio.get_running_loop().time(),
                    category=current_stage,
                )
                await save_screenshot(page, f"process_fail_{store_name}")
