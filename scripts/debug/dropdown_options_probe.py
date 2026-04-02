import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

TARGET_URL = os.getenv("TARGET_URL", "https://sellercentral.amazon.co.uk/snowdash/ref=xx_shopdash_dnav_xx")
DEBUG_OUTPUT_DIR = Path("output/debug")


async def main():
    DEBUG_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        with open("state.json", encoding="utf-8") as file_handle:
            state = json.load(file_handle)

        context = await browser.new_context(storage_state=state)
        page = await context.new_page()
        print("Going to dashboard")
        await page.goto(TARGET_URL, timeout=60_000, wait_until="networkidle")
        await asyncio.sleep(2)

        trigger = page.locator("#store-selector-dropdown")
        if await trigger.is_visible():
            await trigger.click()
            await asyncio.sleep(1)

            search_inputs = page.locator('input[id^="katal-id-"]')
            count = await search_inputs.count()
            for index in range(count):
                input_locator = search_inputs.nth(index)
                placeholder = await input_locator.get_attribute("placeholder")
                if placeholder and "shoppers" in placeholder.lower():
                    continue
                if await input_locator.is_visible():
                    await input_locator.fill("Acton Morrisons")
                    break

            await asyncio.sleep(2)

            options = page.locator('.dropdown-content, .dropdown-popover, ul, [role="listbox"]').last
            html = (
                await options.evaluate("el => el.outerHTML")
                if await options.count() > 0
                else (await page.evaluate("document.body.innerHTML"))
            )
            with open(DEBUG_OUTPUT_DIR / "dropdown_dump.html", "w", encoding="utf-8") as file_handle:
                file_handle.write(html)
            print("Dumped dropdown HTML to output/debug/dropdown_dump.html")
        else:
            print("store-selector-dropdown not visible.")

        await browser.close()


asyncio.run(main())
