import asyncio, json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        with open('state.json') as f:
            state = json.load(f)
        
        c = json.load(open('config.json'))
        context = await browser.new_context(storage_state=state)
        page = await context.new_page()
        
        await page.goto(c['target_url'], timeout=60000, wait_until="networkidle")
        
        # Look for the element with text 'Stores'
        html = await page.locator("body").inner_html()
        with open('body.html', 'w') as f:
            f.write(html)
            
        print("Downloaded DOM.")
        await browser.close()

asyncio.run(main())
