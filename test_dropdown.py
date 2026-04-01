import asyncio
import json

from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        with open("state.json") as f:
            state = json.load(f)
        c = json.load(open("config.json"))
        context = await browser.new_context(storage_state=state)
        page = await context.new_page()
        print("Navigating to dashboard...")
        await page.goto(c["target_url"], timeout=60000, wait_until="networkidle")
        print("Page loaded.")

        selectors = [
            "kat-dropdown",
            "#store-selector-dropdown",
            "text=Stores New",
            "div.dropdown-button",
            "[id='store-selector-dropdown']",
            "kat-badge",
        ]

        for sel in selectors:
            try:
                count = await page.locator(sel).count()
                print(f"Selector '{sel}': {count} elements found.")
            except Exception as e:
                print(f"Selector '{sel}': ERROR {e}")

        print("Trying get_by_text('Wellington')")
        print("Count:", await page.get_by_text("Wellington").count())

        await browser.close()


asyncio.run(main())
