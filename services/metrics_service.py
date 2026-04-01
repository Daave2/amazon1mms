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
    start_ts = asyncio.get_event_loop().time()
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
        try:
            api_data = None
            if state.cache.api_url_template and merchant_id:
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
                    api_data = None

            if not api_data:
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

            data_to_use = {}
            if isinstance(api_data, list):
                app_logger.info(
                    f"[{store_name}] Detailed metrics list received ({len(api_data)} records). Aggregating..."
                )
                from core.schemas import AmazonShopperRecord

                # Parse robustly using Pydantic
                records = [AmazonShopperRecord.model_validate(m) for m in api_data]

                all_masters = [m for m in records if m.type == "MASTER"]
                if not all_masters:
                    all_masters = records

                PROFILE_PRIORITY = {"COMBINED": 0, "REGULAR": 1, "RESCUE": 2, "MANAGER": 3}
                by_shopper = {}
                for m in all_masters:
                    name = m.shopperName or m.externalId or "unknown"
                    profile = m.shopperProfile or "NONE"
                    existing_profile = by_shopper[name].shopperProfile if name in by_shopper else "NONE"
                    if name not in by_shopper or PROFILE_PRIORITY.get(profile, 99) < PROFILE_PRIORITY.get(
                        existing_profile, 99
                    ):
                        by_shopper[name] = m

                masters = list(by_shopper.values())

                total_orders = sum(m.metrics.OrdersShopped_V2 for m in masters)
                total_units = sum(m.metrics.RequestedQuantity_V2 for m in masters)
                total_fulfilled = sum(m.metrics.PickedUnits_V2 for m in masters)

                total_pick_time_sec = sum(m.metrics.PickTimeInSec_V2 for m in masters)
                total_time_ms = sum(m.metrics.TimeAvailable_V2 for m in masters)
                uph = (total_fulfilled / (total_pick_time_sec / 3600)) if total_pick_time_sec > 0 else 0.0

                total_late_picks_count = sum(
                    m.metrics.OrdersShopped_V2 * (m.metrics.LatePicksRate / 100) for m in masters
                )
                late_picks_rate = (total_late_picks_count / total_orders * 100) if total_orders > 0 else 0.0

                total_inf_count = sum(
                    m.metrics.RequestedQuantity_V2 * (m.metrics.ItemNotFoundRate_V2 / 100) for m in masters
                )
                inf_rate = (total_inf_count / total_units * 100) if total_units > 0 else 0.0
                found_rate = 100.0 - inf_rate

                total_cancellations = sum(m.metrics.OrderCancellations for m in masters)

                # Assign flattened values mapped for final submission
                data_to_use = {
                    "OrdersShopped_V2": total_orders,
                    "RequestedQuantity_V2": total_units,
                    "PickedUnits_V2": total_fulfilled,
                    "AverageUPH_V2": uph,
                    "LatePicksRate": late_picks_rate,
                    "ItemNotFoundRate_V2": inf_rate,
                    "ItemFoundRate_V2": found_rate,
                    "OrderCancellations": total_cancellations,
                    "TimeAvailable_V2": total_time_ms,
                }
            else:
                from core.schemas import AmazonShopperRecord

                record = AmazonShopperRecord.model_validate(api_data)
                data_to_use = {
                    "OrdersShopped_V2": record.OrdersShopped_V2 or record.metrics.OrdersShopped_V2,
                    "RequestedQuantity_V2": record.RequestedQuantity_V2 or record.metrics.RequestedQuantity_V2,
                    "PickedUnits_V2": record.PickedUnits_V2 or record.metrics.PickedUnits_V2,
                    "AverageUPH_V2": record.AverageUPH_V2 or record.metrics.AverageUPH_V2,
                    "LatePicksRate": record.LatePicksRate or record.metrics.LatePicksRate,
                    "ItemNotFoundRate_V2": record.ItemNotFoundRate_V2 or record.metrics.ItemNotFoundRate_V2,
                    "ItemFoundRate_V2": record.ItemFoundRate_V2 or record.metrics.ItemFoundRate_V2,
                    "OrderCancellations": record.OrderCancellations or record.metrics.OrderCancellations,
                    "TimeAvailable_V2": record.TimeAvailable_V2 or record.metrics.TimeAvailable_V2,
                }

            lates_val = data_to_use.get("LatePicksRate", 0.0)
            formatted_lates = f"{lates_val:.1f} %"
            app_logger.info(f"[{store_name}] 'Late Picks' extracted from API JSON: {formatted_lates}")

            milliseconds_from_api = float(data_to_use.get("TimeAvailable_V2", 0.0))
            total_seconds = int(milliseconds_from_api / 1000)
            total_minutes, _ = divmod(abs(total_seconds), 60)
            total_hours, remaining_minutes = divmod(total_minutes, 60)
            formatted_time_available = f"{total_hours}:{remaining_minutes:02d}"

            current_date = datetime.now(LOCAL_TIMEZONE).strftime("%Y-%m-%d")
            form_data = {
                "date": current_date,
                "store": store_name,
                "orders": str(data_to_use.get("OrdersShopped_V2") or 0),
                "units": str(data_to_use.get("RequestedQuantity_V2") or 0),
                "fulfilled": str(data_to_use.get("PickedUnits_V2") or 0),
                "uph": f"{(data_to_use.get('AverageUPH_V2') or 0.0):.0f}",
                "inf": f"{(data_to_use.get('ItemNotFoundRate_V2') or 0.0):.1f} %",
                "found": f"{(data_to_use.get('ItemFoundRate_V2') or 0.0):.1f} %",
                "cancelled": str(int(data_to_use.get("OrderCancellations") or 0)),
                "lates": formatted_lates,
                "time_available": formatted_time_available,
            }
            await submission_queue.put(form_data)

            duration = asyncio.get_event_loop().time() - start_ts
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
                await state.add_failure(f"{store_name} (Fail)", asyncio.get_event_loop().time())
                await save_screenshot(page, f"process_fail_{store_name}")
