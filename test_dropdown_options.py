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

        trigger = page.locator("#store-selector-dropdown")
        if await trigger.is_visible():
            await trigger.click()
            await asyncio.sleep(1)

            # fill search
            search_inputs = page.locator('input[id^="katal-id-"]')
            count = await search_inputs.count()
            for i in range(count):
                inp = search_inputs.nth(i)
                ph = await inp.get_attribute("placeholder")
                if ph and "shoppers" in ph.lower():
                    continue
                if await inp.is_visible():
                    await inp.fill("Acton Morrisons")
                    break

            await asyncio.sleep(2)

            # Extract options texts
            options = page.locator('.dropdown-content, .dropdown-popover, ul, [role="listbox"]').last
            html = (
                await options.evaluate("el => el.outerHTML")
                if await options.count() > 0
                else (await page.evaluate("document.body.innerHTML"))
            )
            with open("dropdown_dump.html", "w") as f:
                f.write(html)
            print("Dumped dropdown html.")
        else:
            print("store-selector-dropdown not visible.")

        await browser.close()


asyncio.run(main())
