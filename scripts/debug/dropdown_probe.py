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

        selectors = [
            "kat-dropdown",
            "#store-selector-dropdown",
            "text=Stores New",
            "div.dropdown-button",
            "[id='store-selector-dropdown']",
            "kat-badge",
        ]

        for selector in selectors:
            try:
                count = await page.locator(selector).count()
                print(f"Selector '{selector}': {count} elements found.")
            except Exception as exc:
                print(f"Selector '{selector}': ERROR {exc}")

        print("Trying get_by_text('Wellington')")
        print("Count:", await page.get_by_text("Wellington").count())

        await browser.close()


asyncio.run(main())
