import asyncio, json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        with open('state.json') as f: state = json.load(f)
        c = json.load(open('config.json'))
        context = await browser.new_context(storage_state=state)
        page = await context.new_page()
        print("Navigating to dashboard...")
        await page.goto(c['target_url'], timeout=60000, wait_until="networkidle")
        print("Page loaded.")

        dropdowns = page.locator("kat-dropdown")
        count = await dropdowns.count()
        print(f"Found {count} kat-dropdown elements.")
        
        for i in range(count):
            loc = dropdowns.nth(i)
            vis = await loc.is_visible()
            html = await loc.evaluate("el => el.outerHTML")
            text = html[:150]
            print(f"kat-dropdown {i}: visible={vis}, snippet={text}")
            
        print("Trying to find element with 'Stores' label.")
        stores_label = page.locator("text=Stores").first
        print("Stores label visible:", await stores_label.is_visible())
        
        await browser.close()

asyncio.run(main())
