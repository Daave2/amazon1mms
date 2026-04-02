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

        html = await page.locator("header").inner_html()
        with open(DEBUG_OUTPUT_DIR / "header_dump.html", "w", encoding="utf-8") as file_handle:
            file_handle.write(html)
        print("Dumped header to output/debug/header_dump.html")

        await browser.close()


asyncio.run(main())
