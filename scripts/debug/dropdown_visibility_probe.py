import asyncio
import json
import os

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

TARGET_URL = os.getenv("TARGET_URL", "https://sellercentral.amazon.co.uk/snowdash/ref=xx_shopdash_dnav_xx")


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        with open("state.json", encoding="utf-8") as file_handle:
            state = json.load(file_handle)

        context = await browser.new_context(storage_state=state)
        page = await context.new_page()
        print("Navigating to dashboard...")
        await page.goto(TARGET_URL, timeout=60_000, wait_until="networkidle")
        print("Page loaded.")

        dropdowns = page.locator("kat-dropdown")
        count = await dropdowns.count()
        print(f"Found {count} kat-dropdown elements.")

        for index in range(count):
            locator = dropdowns.nth(index)
            visible = await locator.is_visible()
            html = await locator.evaluate("el => el.outerHTML")
            print(f"kat-dropdown {index}: visible={visible}, snippet={html[:150]}")

        print("Trying to find element with 'Stores' label.")
        stores_label = page.locator("text=Stores").first
        print("Stores label visible:", await stores_label.is_visible())

        await browser.close()


asyncio.run(main())
