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
        print("Going to snowdash")
        await page.goto(c["target_url"], timeout=60000, wait_until="networkidle")
        await asyncio.sleep(2)

        # Dump the top nav bar (header) to look for the account picker
        html = await page.locator("header").inner_html()
        with open("header_dump.html", "w") as f:
            f.write(html)
        print("Dumped header.")

        await browser.close()


asyncio.run(main())
