"""
Debug probe for a known store metrics endpoint.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright

MERCHANT_ID = "A261C0MAVUD2MX"
TZ = timezone(timedelta(hours=1))

with open("output/discovery_cache.json", encoding="utf-8") as file_handle:
    cache = json.load(file_handle)

template = cache["template"].replace("/summationMetrics?", "/metrics?")
current_hour = datetime.now(TZ).hour
template = re.sub(r"endRange%5Bhour%5D=\d+", f"endRange%5Bhour%5D={current_hour}", template)
url = template.replace("{merchant_id}", MERCHANT_ID)

EXPECTED = {
    "Tasks Completed": 437,
    "Units Requested": 11127,
    "Units Fulfilled": 10953,
    "UPH": 69,
    "INF": 3.3,
    "Item Found Rate": 96.7,
    "Order Cancellations": 12,
    "Late Picks": 1.7,
}


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(storage_state="state.json")

        print(f"End hour set to: {current_hour}")
        response = await context.request.get(url, timeout=45_000)
        if response.status != 200:
            print(f"ERROR: API returned {response.status}")
            await browser.close()
            return

        data = await response.json()
        all_masters = [metric for metric in data if metric.get("type") == "MASTER"]
        print(f"Total records: {len(data)}, MASTER: {len(all_masters)}")

        priority = {"COMBINED": 0, "REGULAR": 1, "RESCUE": 2, "MANAGER": 3}
        by_shopper = {}
        for metric in all_masters:
            name = metric.get("shopperName") or metric.get("externalId", "unknown")
            profile = metric.get("shopperProfile") or "NONE"
            if name not in by_shopper or priority.get(profile, 99) < priority.get(
                by_shopper[name].get("shopperProfile", "NONE"), 99
            ):
                by_shopper[name] = metric

        masters = list(by_shopper.values())
        print(f"After COMBINED dedup: {len(masters)} unique shoppers")

        total_orders = sum(float(metric.get("metrics", {}).get("OrdersShopped_V2", 0)) for metric in masters)
        total_units = sum(float(metric.get("metrics", {}).get("RequestedQuantity_V2", 0)) for metric in masters)
        total_fulfilled = sum(float(metric.get("metrics", {}).get("PickedUnits_V2", 0)) for metric in masters)
        total_pick_time_sec = sum(float(metric.get("metrics", {}).get("PickTimeInSec_V2", 0)) for metric in masters)
        uph = (total_fulfilled / (total_pick_time_sec / 3600)) if total_pick_time_sec > 0 else 0.0

        total_late = sum(
            float(metric.get("metrics", {}).get("OrdersShopped_V2", 0))
            * (float(metric.get("metrics", {}).get("LatePicksRate", 0)) / 100)
            for metric in masters
        )
        late_rate = (total_late / total_orders * 100) if total_orders > 0 else 0.0

        total_inf = sum(
            float(metric.get("metrics", {}).get("RequestedQuantity_V2", 0))
            * (float(metric.get("metrics", {}).get("ItemNotFoundRate_V2", 0)) / 100)
            for metric in masters
        )
        inf_rate = (total_inf / total_units * 100) if total_units > 0 else 0.0
        found_rate = 100.0 - inf_rate

        total_cancellations = sum(float(metric.get("metrics", {}).get("OrderCancellations", 0)) for metric in masters)

        computed = {
            "Tasks Completed": int(total_orders),
            "Units Requested": int(total_units),
            "Units Fulfilled": int(total_fulfilled),
            "UPH": round(uph),
            "INF": round(inf_rate, 1),
            "Item Found Rate": round(found_rate, 1),
            "Order Cancellations": int(total_cancellations),
            "Late Picks": round(late_rate, 1),
        }

        print(f"\n{'Metric':<22} {'Dashboard':>12} {'Scraper':>12} {'Match?':>8}")
        print("-" * 58)
        all_match = True
        for key in EXPECTED:
            expected = EXPECTED[key]
            actual = computed[key]
            match = "yes" if expected == actual else "no"
            if expected != actual:
                all_match = False
            print(f"{key:<22} {expected:>12} {actual:>12} {match:>8}")

        print("\nALL MATCH!" if all_match else "\nSome mismatches remain.")
        await browser.close()


asyncio.run(main())
