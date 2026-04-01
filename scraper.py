# =======================================================================================
#               AMAZON SELLER CENTRAL SCRAPER (CI/CD / COMMAND-LINE VERSION)
# =======================================================================================
# This version is optimized with direct HTTP form submission and robust,
# patient scraping logic for dynamically loaded content.
# =======================================================================================

import logging
import urllib.parse
from datetime import datetime
from pytz import timezone
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError,
    expect,
    Error as PlaywrightError,
)
import os
import csv
import json
import asyncio
from asyncio import Queue
from threading import Lock
from typing import Dict, List, Any
import pyotp
from logging.handlers import RotatingFileHandler
import re
import psutil
import random

import aiohttp
import aiofiles
import ssl
import certifi
import io

# Use UK timezone for log timestamps
LOCAL_TIMEZONE = timezone('Europe/London')


class LocalTimeFormatter(logging.Formatter):
    """Formatter that converts timestamps to ``LOCAL_TIMEZONE``."""

    def converter(self, ts: float):
        dt = datetime.fromtimestamp(ts, LOCAL_TIMEZONE)
        return dt.timetuple()

#######################################################################
#                             APP SETUP & LOGGING
#######################################################################

def setup_logging():
    """Configure application logging to file and console.

    Returns:
        Logger: Configured logger instance used throughout the app.
    """
    app_logger = logging.getLogger('app')
    app_logger.setLevel(logging.INFO)
    app_file = RotatingFileHandler('app.log', maxBytes=10**7, backupCount=5)
    fmt = LocalTimeFormatter('%(asctime)s %(levelname)s %(message)s')
    app_file.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    app_logger.addHandler(app_file)
    app_logger.addHandler(console)
    return app_logger

app_logger = setup_logging()

#######################################################################
#                            CONFIG & CONSTANTS
#######################################################################

try:
    with open('config.json', 'r') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    app_logger.critical("config.json not found. Please create it before running.")
    exit(1)
except json.JSONDecodeError:
    app_logger.critical("config.json is not valid JSON. Please fix it.")
    exit(1)

DEBUG_MODE      = config.get('debug', False)
LOGIN_URL       = config['login_url']
BASE_DASHBOARD_URL = config.get('target_url', '')
CHAT_WEBHOOK_URL = config.get('chat_webhook_url')
CHAT_BATCH_SIZE  = config.get('chat_batch_size', 100)
STORE_PREFIX_RE  = re.compile(r"^morrisons?\s*-?\s*|\s*-?\s*morrisons?$", re.I)

# --- Constants for target-based emojis ---
EMOJI_GREEN_CHECK = "\u2705" # ✅
EMOJI_RED_CROSS = "\u274C"   # ❌
UPH_THRESHOLD = 80
LATES_THRESHOLD = 3.0
INF_THRESHOLD = 2.0

# Clean up store names for matching
def normalize_name(name: str) -> str:
    # 1. Lowercase and remove 'Morrisons'
    n = name.lower().replace("morrisons", "")
    # 2. Replace common separators with spaces
    n = re.sub(r"[-_\.]", " ", n)
    # 3. Trim extra whitespace
    return n.strip()

# Special mappings for stores whose names in CSV don't match the dropdown list easily
SPECIAL_NAME_MAPPINGS = {
    "analby": "anlaby",
    "baglan moor": "baglan",
    "cardiff tygals": "cardiff tyglass",
    "connahs quay": "connahs quays",
    "thornton cleveleys": "thornton-cleveleys",
    "auckland": "bishop aukland",
    "preston riversway": "preston",
    "harrow trident point": "harrow",
    "stevenson": "stevenston",
    "weston super mare": "weston-super-mare",
}

def sanitize_store_name(name: str) -> str:
    """Trim 'Morrisons' prefix or suffix from store names for chat display."""
    return STORE_PREFIX_RE.sub("", name).strip()

FORM_POST_URL = "https://docs.google.com/forms/d/e/1FAIpQLSefktpkvAEYtT8pgYknAdWH_GmopNb-QLrmtTS-ijrBTc1hew/formResponse"
FIELD_MAP = {
    'date':           'entry.1483325020',
    'store':          'entry.117918617',
    'orders':         'entry.128719511',
    'units':          'entry.66444552',
    'fulfilled':      'entry.2093280675',
    'uph':            'entry.316694141',
    'inf':            'entry.909185879',
    'found':          'entry.637588300',
    'cancelled':      'entry.1775576921',
    'lates':          'entry.2130893076',
    'field_11':       'entry.2071609599',
    'time_available': 'entry.1823671734',
}

INITIAL_CONCURRENCY = config.get('initial_concurrency', 30)
NUM_FORM_SUBMITTERS = config.get('num_form_submitters', 2)

AUTO_CONF = config.get('auto_concurrency', {})
AUTO_ENABLED = AUTO_CONF.get('enabled', False)
AUTO_MIN_CONCURRENCY = AUTO_CONF.get('min_concurrency', config.get('min_concurrency', 1))
AUTO_MAX_CONCURRENCY = AUTO_CONF.get('max_concurrency', config.get('max_concurrency', INITIAL_CONCURRENCY))
CPU_UPPER_THRESHOLD = AUTO_CONF.get('cpu_upper_threshold', 90)
CPU_LOWER_THRESHOLD = AUTO_CONF.get('cpu_lower_threshold', 65)
MEM_UPPER_THRESHOLD = AUTO_CONF.get('mem_upper_threshold', 90)
CHECK_INTERVAL = AUTO_CONF.get('check_interval_seconds', 5)
COOLDOWN_SECONDS = AUTO_CONF.get('cooldown_seconds', 15)

LOG_FILE        = os.path.join('output', 'submissions.log')
JSON_LOG_FILE   = os.path.join('output', 'submissions.jsonl')
STORAGE_STATE   = 'state.json'
OUTPUT_DIR      = 'output'
os.makedirs(OUTPUT_DIR, exist_ok=True)

PAGE_TIMEOUT    = config.get('page_timeout_ms', 30000) # Reduced to 30s for fail-fast
WAIT_TIMEOUT    = config.get('element_wait_timeout_ms', 15000) # Reduced to 10s
ACTION_TIMEOUT = int(PAGE_TIMEOUT / 2)
WORKER_RETRY_COUNT = 1

RESOURCE_BLOCKLIST = [
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "adservice.google.com", "facebook.net", "fbcdn.net", "analytics.tiktok.com",
]

#######################################################################
#                      GLOBALS
#######################################################################

log_lock      = asyncio.Lock()
progress_lock = Lock()
urls_data     = []
progress      = {"current": 0, "total": 0, "lastUpdate": "N/A"}
run_failures  = []
start_time    = None
failure_timestamps = [] # List of timestamps of recent failures
failure_lock = asyncio.Lock()

# Metrics for Advanced Reporting
metrics = {
    "collection_times": [], # List of (store_name, duration_seconds)
    "submission_times": [], # List of (store_name, duration_seconds)
    "retries": 0,
    "total_orders": 0,
    "total_units": 0,
    "retry_stores": set()
}
metrics_lock = asyncio.Lock()

pending_chat_entries: List[Dict[str, str]] = []
pending_chat_lock = asyncio.Lock()
chat_batch_count = 0

playwright = None
browser = None

concurrency_limit = INITIAL_CONCURRENCY
active_workers_count = 0
concurrency_condition = asyncio.Condition()

last_concurrency_change = 0.0

# --- API Discovery & Bypassing ---
# Path for persistent cache
DISCOVERY_CACHE_FILE = os.path.join(OUTPUT_DIR, 'discovery_cache.json')
# Captured from the first successful store refresh to bypass UI for others
api_url_template = None
api_discovery_lock = asyncio.Lock()
# Map of store names (as they appear in urls.csv) to their internal merchantIds
merchant_id_cache = {}
merchant_cache_lock = asyncio.Lock()

#######################################################################
#                          UTILITIES
#######################################################################

async def _save_screenshot(page: Page | None, prefix: str):
    if not page or page.is_closed():
        app_logger.warning(f"Cannot save screenshot '{prefix}': Page is closed or unavailable.")
        return
    try:
        safe_prefix = re.sub(r'[\\/*?:"<>|]', "_", prefix)
        timestamp = datetime.now(LOCAL_TIMEZONE).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(OUTPUT_DIR, f"{safe_prefix}_{timestamp}.png")
        await page.screenshot(path=path, full_page=True, timeout=15000)
        app_logger.info(f"Screenshot saved for debugging: {path}")
    except Exception as e:
        app_logger.error(f"Failed to save screenshot with prefix '{prefix}': {e}")

def load_discovery_cache():
    """Load discovered merchant IDs and URL templates from persistent storage."""
    global api_url_template, merchant_id_cache
    if os.path.exists(DISCOVERY_CACHE_FILE):
        try:
            with open(DISCOVERY_CACHE_FILE, 'r') as f:
                data = json.load(f)
                api_url_template = data.get('template')
                merchant_id_cache.update(data.get('merchant_ids', {}))
                app_logger.info(f"Loaded {len(merchant_id_cache)} discovered IDs from cache.")
        except Exception as e:
            app_logger.warning(f"Failed to load discovery cache: {e}")

async def save_discovery_cache():
    """Persist discovered IDs and template to disk."""
    # Note: Caller should NOT hold merchant_cache_lock or api_discovery_lock 
    # to avoid potential re-entrancy issues with non-reentrant Locks.
    async with merchant_cache_lock:
        async with api_discovery_lock:
            data = {
                'template': api_url_template,
                'merchant_ids': merchant_id_cache,
                'last_updated': datetime.now(LOCAL_TIMEZONE).isoformat()
            }
            try:
                os.makedirs(os.path.dirname(DISCOVERY_CACHE_FILE), exist_ok=True)
                with open(DISCOVERY_CACHE_FILE, 'w') as f:
                    json.dump(data, f, indent=4)
                app_logger.info("Discovery cache saved.")
            except Exception as e:
                app_logger.warning(f"Failed to save discovery cache: {e}")

def update_urls_csv_with_cache():
    """Optionally update urls.csv with discovered merchant IDs for next time."""
    if not os.path.exists('urls.csv'): return
    try:
        updated_rows = []
        with open('urls.csv', 'r') as f:
            reader = csv.reader(f)
            header = next(reader)
            updated_rows.append(header)
            for row in reader:
                if not row: continue
                store_name = row[2].strip() if len(row) > 2 else row[0].strip()
                # If merchant_id (index 0) is empty, check cache
                if not row[0].strip() and store_name in merchant_id_cache:
                    row[0] = merchant_id_cache[store_name]
                    app_logger.info(f"Filling missing merchant_id in CSV for: {store_name}")
                updated_rows.append(row)
        
        with open('urls.csv', 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerows(updated_rows)
        app_logger.info("urls.csv has been updated with newly discovered IDs.")
    except Exception as e:
        app_logger.warning(f"Failed to update urls.csv: {e}")

def load_default_data():
    global urls_data
    urls_data.clear()
    
    # First, Load Discovery Cache so we have IDs available
    load_discovery_cache()
    
    try:
        with open('urls.csv', 'r', newline='') as f:
            reader = csv.reader(f)
            header = next(reader)
            for i, row in enumerate(reader):
                if not row:
                    continue
                # 1MMS: Only store_name (column 2) is required.
                # CSV format: merchant_id,new_id,store_name,marketplace_id
                raw_store_name = row[2].strip() if len(row) > 2 and row[2].strip() else (row[0].strip() if row[0].strip() else '')
                if not raw_store_name:
                    app_logger.warning(f"Skipping row {i+2} in urls.csv: no store name found")
                    continue
                
                # Extract formatted name for the Search Bar and Google Chat (strip Morrisons)
                formatted_name = STORE_PREFIX_RE.sub('', raw_store_name).strip()
                formatted_name = re.sub(r'(?i)\s*Morrisons?$', '', formatted_name)
                dropdown_name = formatted_name
                
                urls_data.append({
                    'store_name': raw_store_name,
                    'dropdown_name': dropdown_name,
                    'merchant_id': row[0].strip() if len(row) > 0 else '',
                    'marketplace_id': row[3].strip() if len(row) > 3 else '',
                })
        app_logger.info(f"{len(urls_data)} stores loaded from urls.csv")
    except FileNotFoundError:
        app_logger.error("FATAL: 'urls.csv' not found. Please ensure the file exists and is named correctly (all lowercase).")
        raise
    except Exception:
        app_logger.exception("An error occurred while loading urls.csv")

def ensure_storage_state():
    if not os.path.exists(STORAGE_STATE) or os.path.getsize(STORAGE_STATE) == 0:
        return False
    try:
        with open(STORAGE_STATE) as f:
            data = json.load(f)
        if (
            not isinstance(data, dict)
            or "cookies" not in data
            or not isinstance(data["cookies"], list)
            or not data["cookies"]
        ):
            return False
        return True
    except json.JSONDecodeError:
        return False

async def auto_concurrency_manager():
    global concurrency_limit, last_concurrency_change
    if not AUTO_ENABLED:
        return
    app_logger.info(
        f"Auto-concurrency enabled with range {AUTO_MIN_CONCURRENCY}-{AUTO_MAX_CONCURRENCY}"
    )
    while True:
        now = asyncio.get_event_loop().time()
        
        # 1. Check Failure Rate (Error-Aware Scaling)
        async with failure_lock:
            # Keep only failures from last 60s
            while failure_timestamps and now - failure_timestamps[0] > 60:
                failure_timestamps.pop(0)
            recent_failure_count = len(failure_timestamps)
        
        # Estimate current rate (requests per minute) based on concurrency
        # Assuming ~2s per request per worker -> 30 req/min per worker
        estimated_throughput = concurrency_limit * 30 
        failure_rate = recent_failure_count / max(estimated_throughput, 1)
        
        if failure_rate > 0.05: # >5% failure rate
            if now - last_concurrency_change >= COOLDOWN_SECONDS:
                concurrency_limit = max(AUTO_MIN_CONCURRENCY, int(concurrency_limit * 0.5))
                last_concurrency_change = now
                app_logger.warning(
                    f"Auto-concurrency: THROTTLING DOWN to {concurrency_limit} due to high failure rate ({failure_rate:.1%})"
                )
                async with concurrency_condition:
                    concurrency_condition.notify_all()
                await asyncio.sleep(COOLDOWN_SECONDS * 2) # Wait longer to recover
                continue

        # 2. Standard Resource Scaling
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        
        if now - last_concurrency_change >= COOLDOWN_SECONDS:
            if (cpu > CPU_UPPER_THRESHOLD or mem > MEM_UPPER_THRESHOLD) and concurrency_limit > AUTO_MIN_CONCURRENCY:
                concurrency_limit -= 1
                last_concurrency_change = now
                app_logger.info(
                    f"Auto-concurrency: decreased to {concurrency_limit} (CPU {cpu:.1f}%, MEM {mem:.1f}%)"
                )
            elif cpu < CPU_LOWER_THRESHOLD and mem < MEM_UPPER_THRESHOLD and concurrency_limit < AUTO_MAX_CONCURRENCY:
                concurrency_limit += 1
                last_concurrency_change = now
                app_logger.info(
                    f"Auto-concurrency: increased to {concurrency_limit} (CPU {cpu:.1f}%, MEM {mem:.1f}%)"
                )
            if concurrency_limit > AUTO_MAX_CONCURRENCY:
                concurrency_limit = AUTO_MAX_CONCURRENCY
            if concurrency_limit < AUTO_MIN_CONCURRENCY:
                concurrency_limit = AUTO_MIN_CONCURRENCY
            async with concurrency_condition:
                concurrency_condition.notify_all()
        await asyncio.sleep(CHECK_INTERVAL)

#######################################################################
#                     AUTHENTICATION & SESSION PRIMING
#######################################################################
async def check_if_login_needed(page: Page, test_url: str) -> bool:
    app_logger.info(f"Verifying session status by navigating to: {test_url}")
    try:
        # We don't wait for 'load' event to finish because it might take time.
        # We just want to see if we land on login page or dashboard.
        await page.goto(test_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        
        # Smart wait: Race between Login elements and Dashboard elements
        # If we see login inputs -> Login needed
        # If we see dashboard elements -> Login NOT needed
        
        login_selector = "input#ap_email, input#ap_password, input[name='email']"
        dashboard_selector = "#content > div > div.mainAppContainerExternal"
        
        try:
            # Wait for either to appear
            found = await page.locator(f"{login_selector}, {dashboard_selector}").first.is_visible(timeout=10000)
            if not found:
                # Fallback check on URL if neither appeared quickly
                if "signin" in page.url.lower() or "/ap/" in page.url:
                    return True
                return True # Assume needed if we can't verify dashboard
        except TimeoutError:
             # If timeout, check URL one last time
            if "signin" in page.url.lower() or "/ap/" in page.url:
                return True
            return True

        # If we are here, something is visible. Check what it is.
        if await page.locator(login_selector).first.is_visible():
            app_logger.info("Login form detected.")
            return True
            
        if await page.locator(dashboard_selector).is_visible():
            app_logger.info("Dashboard detected. Session is valid.")
            return False
            
        return True
    except Exception as e:
        app_logger.error(f"Error during session check: {e}", exc_info=DEBUG_MODE)
        return True

async def perform_login_and_otp(page: Page) -> bool:
    app_logger.info(f"Navigating to login page: {LOGIN_URL}")
    try:
        await page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT, wait_until="load")
        app_logger.info("Initial page loaded. Determining login flow...")

        continue_shopping_selector = 'button:has-text("Continue shopping")'
        email_field_selector = 'input#ap_email'

        await page.wait_for_selector(f"{continue_shopping_selector}, {email_field_selector}", state="visible", timeout=15000)
        
        if await page.locator(continue_shopping_selector).is_visible():
            app_logger.info("Flow: Interstitial 'Continue shopping' page detected. Clicking it.")
            await page.locator(continue_shopping_selector).click()
            await expect(page.locator(email_field_selector)).to_be_visible(timeout=15000)
        else:
            app_logger.info("Flow: Login form with email field loaded directly.")
        
        email_locator = page.locator(email_field_selector)
        try:
            await email_locator.fill(config['login_email'], timeout=10000)
        except Exception:
            app_logger.warning(
                "Direct selector for email failed. Falling back to label-based selector.")
            fallback_email_locator = page.get_by_label("Email or mobile phone number")
            await expect(fallback_email_locator).to_be_visible(timeout=10000)
            await fallback_email_locator.fill(config['login_email'])

        continue_locator = page.get_by_label("Continue")
        try:
            await continue_locator.click()
        except TimeoutError:
            app_logger.warning(
                "Continue control not available via label. Using fallback selector.")
            fallback_continue = page.get_by_role("button", name=re.compile("continue", re.I))
            if await fallback_continue.count() == 0:
                fallback_continue = page.locator("input#continue, button#continue, input[name='continue']")
            await expect(fallback_continue.first).to_be_visible(timeout=10000)
            await fallback_continue.first.click()

        password_field = page.get_by_label("Password")
        try:
            await expect(password_field).to_be_visible(timeout=10000)
        except TimeoutError:
            app_logger.warning(
                "Password field not visible after entering email. Attempting to bypass passkey flow.")

            async def _click_if_visible(locator: Any) -> bool:
                try:
                    if locator and await locator.count() > 0:
                        visible_locator = locator.first
                        if await visible_locator.is_visible():
                            await visible_locator.click()
                            return True
                except PlaywrightError as inner_error:
                    app_logger.debug(
                        f"Encountered error while handling alternate sign-in option: {inner_error}",
                        exc_info=DEBUG_MODE,
                    )
                return False

            bypass_attempted = False

            other_ways_button = page.get_by_role("button", name=re.compile("other ways to sign in", re.I))
            if await _click_if_visible(other_ways_button):
                app_logger.info("Clicked 'Other ways to sign in' button to reveal password option.")
                bypass_attempted = True

            if not bypass_attempted:
                passkey_bypass_selectors = [
                    page.get_by_role("button", name=re.compile("use( your)? password", re.I)),
                    page.get_by_role("link", name=re.compile("use( your)? password", re.I)),
                    page.locator("text=/Use (your )?password/i"),
                    page.locator("text=/Sign-in without passkey/i"),
                ]
                for locator in passkey_bypass_selectors:
                    if await _click_if_visible(locator):
                        app_logger.info("Clicked alternate sign-in option to fall back to password entry.")
                        bypass_attempted = True
                        break

            if not bypass_attempted:
                app_logger.warning(
                    "No passkey bypass option detected. Proceeding without additional interaction.")

            await expect(password_field).to_be_visible(timeout=10000)
        await password_field.fill(config['login_password'])
        await page.get_by_label("Sign in").click()
        
        otp_selector = 'input[id*="otp"]'
        dashboard_selector = "#content > div > div.mainAppContainerExternal"
        await page.wait_for_selector(f"{otp_selector}, {dashboard_selector}", timeout=30000)

        otp_field = page.locator(otp_selector)
        if await otp_field.is_visible():
            app_logger.info("Two-Step Verification (OTP) is required.")
            otp_code = pyotp.TOTP(config['otp_secret_key']).now()
            await otp_field.fill(otp_code)
            if await page.locator("input[type='checkbox'][name='rememberDevice']").is_visible():
                await page.locator("input[type='checkbox'][name='rememberDevice']").check()
            await page.get_by_role("button", name="Sign in").click()

        # --- 1MMS Account Picker ---
        account_picker_selector = 'h1:has-text("Select an account")'
        await page.wait_for_selector(f"{dashboard_selector}, {account_picker_selector}", timeout=30000)

        # If we landed on the account picker, select the 1MMS User Store
        account_picker = page.locator(account_picker_selector)
        if await account_picker.is_visible():
            app_logger.info("Account picker detected. Selecting 1MMS User Store...")
            try:
                await page.get_by_role("button", name="1MMS User Store").click(timeout=10000)
                app_logger.info("Selected '1MMS User Store'.")
                await page.get_by_role("button", name="United Kingdom").click(timeout=10000)
                app_logger.info("Selected 'United Kingdom' marketplace.")
                await page.get_by_role("button", name="Select account").click(timeout=10000)
                app_logger.info("Clicked 'Select account'. Waiting for dashboard...")
                await page.wait_for_selector(dashboard_selector, timeout=30000)
            except Exception as picker_err:
                app_logger.warning(f"Account picker interaction issue: {picker_err}")
                await _save_screenshot(page, "account_picker_issue")
        
        app_logger.info("Login process appears fully successful.")
        return True
    except Exception as e:
        app_logger.critical(f"Critical error during login process: {e}", exc_info=DEBUG_MODE)
        await _save_screenshot(page, "login_critical_failure")
        return False

async def prime_master_session() -> bool:
    global browser
    app_logger.info("Priming master session")
    ctx = None
    try:
        if not browser or not browser.is_connected(): return False
        ctx = await browser.new_context()
        ctx.set_default_navigation_timeout(PAGE_TIMEOUT)
        ctx.set_default_timeout(ACTION_TIMEOUT)
        await ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "stylesheet", "font", "media") else route.continue_())
        page = await ctx.new_page()
        if not await perform_login_and_otp(page): return False
        storage = await ctx.storage_state()
        with open(STORAGE_STATE, 'w') as f: json.dump(storage, f)
        app_logger.info(f"Login successful. Auth state saved to '{STORAGE_STATE}'.")
        return True
    except Exception as e:
        app_logger.exception(f"Priming failed with an unexpected error: {e}")
        return False
    finally:
        if ctx: await ctx.close()

#######################################################################
#                  OPTIMIZED ARCHITECTURE: WORKERS & LOGGING
#######################################################################

def _format_metric_with_emoji(value_str: str, threshold: float, is_uph: bool = False) -> str:
    """Applies a pass/fail emoji to a metric string based on a threshold."""
    try:
        numeric_value = float(re.sub(r'[^\d.]', '', value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        emoji = EMOJI_GREEN_CHECK if is_good else EMOJI_RED_CROSS
        return f"{emoji} {value_str}"
    except (ValueError, TypeError):
        return value_str # Return as is if not a number

async def post_to_chat_webhook(entries: List[Dict[str, str]]):
    """Send a table-formatted card message with emoji indicators."""
    if not CHAT_WEBHOOK_URL or not entries:
        return
    try:
        global chat_batch_count
        chat_batch_count += 1
        batch_header_text = datetime.now(LOCAL_TIMEZONE).strftime("%A %d %B, %H:%M")
        card_subtitle = f"{batch_header_text}  Batch {chat_batch_count} ({len(entries)} stores)"

        sorted_entries = sorted(entries, key=lambda e: sanitize_store_name(e.get("store", "")))

        # --- Build the Grid/Table Widget with Emoji Indicators ---
        grid_items = [
            {"title": "Store", "textAlignment": "START"},
            {"title": "UPH", "textAlignment": "CENTER"},
            {"title": "Lates", "textAlignment": "CENTER"},
            {"title": "INF", "textAlignment": "CENTER"},
        ]

        for entry in sorted_entries:
            uph_val = entry.get("uph", "N/A")
            lates_val = entry.get("lates", "0.0 %") or "0.0 %"
            inf_val = entry.get("inf", "0.0 %") or "0.0 %"

            # Apply emoji formatting
            formatted_uph = _format_metric_with_emoji(uph_val, UPH_THRESHOLD, is_uph=True)
            formatted_lates = _format_metric_with_emoji(lates_val, LATES_THRESHOLD)
            formatted_inf = _format_metric_with_emoji(inf_val, INF_THRESHOLD)

            grid_items.extend([
                {"title": sanitize_store_name(entry.get("store", "N/A")), "textAlignment": "START"},
                {"title": formatted_uph, "textAlignment": "CENTER"},
                {"title": formatted_lates, "textAlignment": "CENTER"},
                {"title": formatted_inf, "textAlignment": "CENTER"},
            ])
        
        table_section = {
            "header": "Key Performance Indicators",
            "widgets": [{
                "grid": {
                    "title": "Performance Summary",
                    "columnCount": 4,
                    "borderStyle": {"type": "STROKE", "cornerRadius": 4},
                    "items": grid_items
                }
            }]
        }

        # --- Assemble the final payload ---
        payload = {
            "cardsV2": [{
                "cardId": f"batch-summary-{chat_batch_count}",
                "card": {
                    "header": {
                        "title": "Seller Central Metrics Report (1MMS)",
                        "subtitle": card_subtitle,
                        "imageUrl": "https://i.imgur.com/u0e3d2x.png",
                        "imageType": "CIRCLE"
                    },
                    "sections": [table_section],
                },
            }]
        }
        
        timeout = aiohttp.ClientTimeout(total=30)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.post(CHAT_WEBHOOK_URL, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    app_logger.error(
                        f"Chat webhook post failed. Status: {resp.status}. Response: {error_text}"
                    )
    except Exception as e:
        app_logger.error(f"Error posting to chat webhook: {e}", exc_info=DEBUG_MODE)


async def post_job_summary(total: int, success: int, failures: List[str], duration: float):
    """Send a detailed job summary card to Google Chat with advanced analytics."""
    if not CHAT_WEBHOOK_URL: return
    try:
        status_text = "Job Completed Successfully"
        status_icon = "✅"
        if failures:
            status_text = f"Job Completed with {len(failures)} Failures"
            status_icon = "⚠️"
        
        success_rate = (success / total) * 100 if total > 0 else 0
        throughput_spm = (success / (duration / 60)) if duration > 0 else 0
        
        # Calculate Analytics
        async with metrics_lock:
            coll_times = metrics["collection_times"]
            sub_times = metrics["submission_times"]
            retries = metrics["retries"]
            retry_stores = len(metrics["retry_stores"])
            total_orders = metrics["total_orders"]
            total_units = metrics["total_units"]
            
        avg_coll = sum(t[1] for t in coll_times) / len(coll_times) if coll_times else 0
        avg_sub = sum(t[1] for t in sub_times) / len(sub_times) if sub_times else 0
        
        # P95 Latency
        sorted_coll = sorted([t[1] for t in coll_times])
        p95_coll = sorted_coll[int(len(sorted_coll) * 0.95)] if sorted_coll else 0
        
        fastest_store = min(coll_times, key=lambda x: x[1]) if coll_times else ("N/A", 0)
        slowest_store = max(coll_times, key=lambda x: x[1]) if coll_times else ("N/A", 0)
        
        # Bottleneck Analysis
        bottleneck_msg = "Balanced Flow"
        if avg_coll > 2.0:
            bottleneck_msg = "🐢 Slow Scraping (Browser Lag)"
        elif avg_sub > 1.0:
            bottleneck_msg = "🐢 Slow Submission (Webhook Lag)"
        elif avg_coll < 1.0 and avg_sub < 0.5:
            bottleneck_msg = "🚀 High Speed (No Bottlenecks)"

        # Sections
        stats_section = {
            "header": "High-Level Stats",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Throughput",
                        "text": f"{throughput_spm:.1f} stores/min",
                        "startIcon": {"knownIcon": "FLIGHT_DEPARTURE"}
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Success Rate",
                        "text": f"{success}/{total} ({success_rate:.1f}%)",
                        "startIcon": {"knownIcon": "STAR"}
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Total Duration",
                        "text": f"{duration:.2f}s",
                        "startIcon": {"knownIcon": "CLOCK"}
                    }
                }
            ]
        }
        
        volume_section = {
            "header": "Business Volume 📦",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Total Orders",
                        "text": f"{total_orders:,}",
                        "startIcon": {"knownIcon": "SHOPPING_CART"}
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Total Units",
                        "text": f"{total_units:,}",
                        "startIcon": {"knownIcon": "TICKET"}
                    }
                }
            ]
        }

        resilience_section = {
            "header": "Resilience & Health 🏥",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Total Retries",
                        "text": str(retries),
                        "startIcon": {"knownIcon": "MEMBERSHIP"} # Best fit for 'repeat'
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Stores Retried",
                        "text": str(retry_stores),
                        "startIcon": {"knownIcon": "STORE"}
                    }
                }
            ]
        }
        
        speed_section = {
            "header": "Speed Breakdown ⏱️",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Avg Collection Time",
                        "text": f"{avg_coll:.2f}s (Browser)",
                        "startIcon": {"knownIcon": "DESCRIPTION"}
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "p95 Collection Time",
                        "text": f"{p95_coll:.2f}s",
                        "startIcon": {"knownIcon": "DESCRIPTION"}
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Bottleneck Status",
                        "text": bottleneck_msg,
                        "startIcon": {"knownIcon": "TRAFFIC"}
                    }
                }
            ]
        }
        
        extremes_section = {
            "header": "Extremes 📉📈",
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Fastest Store",
                        "text": f"{fastest_store[0]} ({fastest_store[1]:.2f}s)",
                        "startIcon": {"knownIcon": "BOLT"}
                    }
                },
                {
                    "decoratedText": {
                        "topLabel": "Slowest Store",
                        "text": f"{slowest_store[0]} ({slowest_store[1]:.2f}s)",
                        "startIcon": {"knownIcon": "SNAIL"}
                    }
                }
            ]
        }
        
        sections = [stats_section, volume_section, resilience_section, speed_section, extremes_section]
        
        if failures:
            # Group failures by type
            failure_counts = {}
            for f in failures:
                # Heuristic: Extract the error part in parentheses or the whole string
                msg = f
                if '(' in f and ')' in f:
                    msg = f[f.rfind('(')+1 : f.rfind(')')]
                failure_counts[msg] = failure_counts.get(msg, 0) + 1
            
            failure_summary = "\n".join([f"• {k}: {v}" for k, v in failure_counts.items()])
            
            failure_list = "\n".join([f"• {f}" for f in failures[:5]])
            if len(failures) > 5:
                failure_list += f"\n...and {len(failures) - 5} more"
            
            failures_section = {
                "header": "Failure Analysis",
                "widgets": [
                    {
                        "textParagraph": {
                            "text": f"<b>Breakdown:</b>\n{failure_summary}"
                        }
                    },
                    {
                        "textParagraph": {
                            "text": f"<font color=\"#FF0000\"><b>Recent Failures:</b>\n{failure_list}</font>"
                        }
                    }
                ]
            }
            sections.append(failures_section)

        payload = {
            "cardsV2": [{
                "cardId": f"job-summary-{int(datetime.now().timestamp())}",
                "card": {
                    "header": {
                        "title": f"{status_icon} {status_text} (1MMS)",
                        "subtitle": datetime.now(LOCAL_TIMEZONE).strftime("%A %d %B, %H:%M"),
                        "imageUrl": "https://i.imgur.com/u0e3d2x.png",
                        "imageType": "CIRCLE"
                    },
                    "sections": sections,
                },
            }]
        }
        
        timeout = aiohttp.ClientTimeout(total=30)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.post(CHAT_WEBHOOK_URL, json=payload) as resp:
                if resp.status != 200:
                    app_logger.error(f"Job summary post failed: {resp.status}")

    except Exception as e:
        app_logger.error(f"Error posting job summary: {e}", exc_info=DEBUG_MODE)


async def add_to_pending_chat(entry: Dict[str, str]):
    if not CHAT_WEBHOOK_URL:
        return
    async with pending_chat_lock:
        pending_chat_entries.append(entry)
        if len(pending_chat_entries) >= CHAT_BATCH_SIZE:
            entries_to_send = pending_chat_entries[:CHAT_BATCH_SIZE]
            del pending_chat_entries[:CHAT_BATCH_SIZE]
            await post_to_chat_webhook(entries_to_send)


async def flush_pending_chat_entries():
    if not CHAT_WEBHOOK_URL:
        return
    async with pending_chat_lock:
        if pending_chat_entries:
            entries = pending_chat_entries[:]
            pending_chat_entries.clear()
            await post_to_chat_webhook(entries)


async def log_submission(data: Dict[str,str]):
    async with log_lock:
        current_timestamp = datetime.now(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')
        log_entry = {'timestamp': current_timestamp, **data}
        fieldnames = ['timestamp','date','store','orders','units','fulfilled','uph','inf','found','cancelled','lates','field_11','time_available']
        new_csv = not os.path.exists(LOG_FILE)
        try:
            csv_buffer = io.StringIO()
            writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames, extrasaction='ignore')
            if new_csv:
                writer.writeheader()
            writer.writerow(log_entry)
            async with aiofiles.open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
                await f.write(csv_buffer.getvalue())
        except IOError as e:
            app_logger.error(f"Error writing to CSV log file {LOG_FILE}: {e}")
        try:
            async with aiofiles.open(JSON_LOG_FILE, 'a', encoding='utf-8') as f:
                await f.write(json.dumps(log_entry) + '\n')
        except IOError as e:
            app_logger.error(f"Error writing to JSON log file {JSON_LOG_FILE}: {e}")
        await add_to_pending_chat(log_entry)

async def http_form_submitter_worker(queue: Queue, worker_id: int):
    log_prefix = f"[HTTP-Submitter-{worker_id}]"
    app_logger.info(f"{log_prefix} Starting up...")
    timeout = aiohttp.ClientTimeout(total=20)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_context)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        while True:
            form_data = None
            try:
                form_data = await queue.get()
                store_name = form_data.get('store', 'Unknown')
                
                # Map keys to Google Form entry IDs
                payload = {}
                for key, value in form_data.items():
                    if key in FIELD_MAP:
                        payload[FIELD_MAP[key]] = value
                
                submit_start = asyncio.get_event_loop().time()
                async with session.post(FORM_POST_URL, data=payload, timeout=10) as resp:
                    if resp.status == 200:
                        await log_submission(form_data)
                        app_logger.info(f"{log_prefix} Submitted data for {form_data.get('store', 'Unknown')}")
                        with progress_lock:
                            progress["current"] += 1
                            progress["lastUpdate"] = datetime.now(LOCAL_TIMEZONE).strftime("%H:%M:%S")
                        
                        submit_duration = asyncio.get_event_loop().time() - submit_start
                        async with metrics_lock:
                            metrics["submission_times"].append((form_data.get('store', 'Unknown'), submit_duration))
                    else:
                        error_text = await resp.text()
                        app_logger.error(f"{log_prefix} Submission for {store_name} failed. Status: {resp.status}. Response: {error_text[:200]}")
                        run_failures.append(f"{store_name} (HTTP Submit Fail {resp.status})")
            except asyncio.CancelledError:
                break
            except Exception as e:
                failed_store = form_data.get('store', 'Unknown') if form_data else "Unknown"
                app_logger.error(f"{log_prefix} Unhandled exception for {failed_store}: {e}", exc_info=DEBUG_MODE)
                run_failures.append(f"{failed_store} (Submit Exception)")
            finally:
                if form_data:
                    queue.task_done()
    app_logger.info(f"{log_prefix} Shut down.")

async def select_store_from_dropdown(page, dropdown_name, store_name):
    """
    Amazon 1MMS Dashboard: interact with the Store Selector dropdown and select the specified store.
    """
    app_logger.info(f"[{store_name}] Selecting store from dropdown matching: {dropdown_name}")
    
    # Try triggering dropdown
    dropdown_trigger = page.locator('#store-selector-dropdown')
    try:
        await expect(dropdown_trigger).to_be_visible(timeout=30000)
        # Click multiple times if needed to ensure the popover/modal opens
        for _ in range(3):
            await dropdown_trigger.first.click(force=True)
            await asyncio.sleep(1)
            # Check if any input or the dropdown list is visible
            if await page.locator('kat-popover input, kat-dropdown-menu input, .dropdown-list').first.is_visible():
                break
    except Exception as e:
        app_logger.warning(f"[{store_name}] Failed to click dropdown trigger: {e}")
        await page.get_by_text("Select a store").first.click(force=True)
        
    await asyncio.sleep(2) # Wait for animation

    # Broad locator for dropdown search inputs; we prioritize those in popovers/dropdowns
    # We explicitly EXCLUDE inputs with "Search for shoppers" placeholder to avoid strict mode errors
    search_input = page.locator('kat-popover input:visible, kat-dropdown-menu input:visible, .dropdown-search input, #store-selector-input input, .store-selector-input input, input[id^="katal-id-"]:visible:not(#katal-id-0, [placeholder*="shoppers" i]), kat-input[placeholder*="Search"]:not([placeholder*="shoppers" i]) input')
    try:
        await expect(search_input.first).to_be_visible(timeout=20000)
    except TimeoutError:
        app_logger.info(f"[{store_name}] Search input not found, attempting to find any visible dropdown-related input.")
        search_input = page.locator('input:visible:not(#katal-id-0, [placeholder*="shoppers" i]), .dropdown-list-container input').first
        if not await search_input.is_visible():
            app_logger.warning(f"[{store_name}] No search input found at all. Proceeding to direct option selection.")
            search_input = None

    if search_input:
        await search_input.first.click()
        await search_input.first.fill(dropdown_name)
        await asyncio.sleep(2)  # Wait for filter to process

    # Click the option matching the search name
    try:
        # 1. First try finding by text exactly
        option_locator = page.get_by_text(dropdown_name, exact=False).first
        if not await option_locator.is_visible():
            # Fuzzy Match with NORMALIZATION
            app_logger.info(f"[{store_name}] No direct match for '{dropdown_name}'. Attempting fuzzy normalized match...")
            options = page.locator('.dropdown-option, [role="option"], kat-option, .kat-option')
            all_options = await options.all_text_contents()
            if all_options:
                import difflib
                target_norm = normalize_name(dropdown_name)
                norm_map = {normalize_name(opt): opt for opt in all_options}
                matches = difflib.get_close_matches(target_norm, list(norm_map.keys()), n=1, cutoff=0.3)
                if matches:
                    matched_option = norm_map[matches[0]]
                    app_logger.info(f"[{store_name}] Fuzzy match found: '{dropdown_name}' -> '{matched_option}'")
                    option_locator = options.filter(has_text=re.compile(re.escape(matched_option), re.I)).first
        
        await expect(option_locator).to_be_visible(timeout=5000)
        selected_text = await option_locator.text_content()
        app_logger.info(f"[{store_name}] Clicking dropdown option: '{selected_text.strip()}'")
        await option_locator.click()
    except TimeoutError:
        app_logger.warning(f"[{store_name}] No dropdown options appeared or matched for '{dropdown_name}' after search.")
        raise

    app_logger.info(f"[{store_name}] Store selected from dropdown.")
    return True


async def process_single_store(page: Page, store_info: Dict[str,str], queue: Queue):
    global api_url_template
    start_ts = asyncio.get_event_loop().time()
    store_name  = store_info['store_name']
    formatted_name = normalize_name(store_name)
    dropdown_name = formatted_name
    
    # Apply special mappings for tricky store names
    for key, val in SPECIAL_NAME_MAPPINGS.items():
        if key in dropdown_name:
            dropdown_name = val
            break
    
    # Use merchant_id from CSV if available, otherwise try cache
    merchant_id = store_info.get('merchant_id') or merchant_id_cache.get(store_name)
    METRICS_TIMEOUT = 45_000
    
    for attempt in range(WORKER_RETRY_COUNT):
        try:
            api_data = None
            
            # --- STRATEGY: Try FAST PATH (Direct API) if we have the template and ID ---
            if api_url_template and merchant_id:
                try:
                    target_url = api_url_template.replace("{merchant_id}", merchant_id)
                    app_logger.info(f"[{store_name}] FAST PATH: Fetching data from API directly via navigation...")
                    
                    # Create a temporary page to fetch the JSON directly (avoids CORS issues with fetch)
                    temp_page = await page.context.new_page()
                    try:
                        resp = await temp_page.goto(target_url, timeout=METRICS_TIMEOUT)
                        if resp and resp.status == 200:
                            api_data = await resp.json()
                            app_logger.info(f"[{store_name}] API Data fetched successfully (Fast Path).")
                        else:
                            raise Exception(f"API Fetch failed: {resp.status if resp else 'No response'}")
                    finally:
                        await temp_page.close()
                except Exception as api_err:
                    app_logger.warning(f"[{store_name}] Fast Path failed: {api_err}. Falling back to UI.")
                    api_data = None

            # --- STRATEGY: SLOW PATH (Dropdown + Discovery) ---
            if not api_data:
                # If we don't have the dropdown trigger, we're definitely not on the right page.
                dropdown_trigger = page.locator('#store-selector-dropdown')
                
                # --- 1MMS: Ensure we are at the dashboard ---
                if not page.url.startswith(BASE_DASHBOARD_URL) or not await dropdown_trigger.is_visible():
                    app_logger.info(f"[{store_name}] Dashboard trigger not visible or URL is wrong. Navigating...")
                    await page.goto(BASE_DASHBOARD_URL, timeout=PAGE_TIMEOUT, wait_until="networkidle")

                # --- 1MMS: Select store from the Stores dropdown ---
                await select_store_from_dropdown(page, dropdown_name, store_name)

                # --- Click Refresh and intercept API response ---
                # We'll support both summationMetrics (aggregates) and metrics (shopper breakdown) as valid confirmations
                refresh_button = page.get_by_role("button", name="Refresh")
                
                async with page.expect_response(
                    lambda r: any(k in r.url for k in ["summationMetrics", "api/metrics"]) and r.status == 200,
                    timeout=METRICS_TIMEOUT,
                ) as resp_info:
                    await expect(refresh_button).to_be_visible(timeout=WAIT_TIMEOUT)
                    # Use dispatch_event("click") to ensure we bypass any overlays
                    await refresh_button.first.dispatch_event("click")
                
                response = await resp_info.value
                api_data = await response.json()
                
                # --- Discovery: Capture URL Template and Merchant ID ---
                req_url = response.url
                parsed = urllib.parse.urlparse(req_url)
                params = urllib.parse.parse_qs(parsed.query)
                
                # Capture the template for FAST PATH. We prefer keeping it generic with {merchant_id}
                if "summationMetrics" in req_url and not api_url_template:
                    async with api_discovery_lock:
                        # Replace the specific merchant ID with a placeholder
                        generic_url = re.sub(r'merchantIds%5B%5D=[^&]*', "merchantIds%5B%5D={merchant_id}", req_url)
                        api_url_template = generic_url
                        app_logger.info(f"[{store_name}] Discovery: Captured API Template: {api_url_template[:100]}...")

                # Extract Merchant IDs from the query params (usually merchantIds[]=[ID])
                captured_mids = params.get('merchantIds[]') or params.get('merchantIds')
                if captured_mids and len(captured_mids) > 0:
                    captured_mid = captured_mids[0]
                    # Update local state
                    merchant_id = captured_mid
                    
                    # Update cache if it's new
                    async with merchant_cache_lock:
                        if merchant_id_cache.get(store_name) != captured_mid:
                            merchant_id_cache[store_name] = captured_mid
                            app_logger.info(f"[{store_name}] Discovery: Discovered internal Merchant ID: {captured_mid}")
                            # Save cache outside of this specific lock usage to be safe
                    
                    await save_discovery_cache()

            # --- Extract Metrics directly from API Data (tap into this for speed!) ---
            # Map API fields to our expected metrics.
            # Using Halifax example as a guide:
            # - Lates is mapped from 'UnacceptedRate_V2'
            # - Info like units, orders, upheld are already from API
            
            # --- Scrape Stats from API (instead of DOM) ---
            # api_data can be a single dict (summationMetrics) or a list of dicts (metrics)
            data_to_use = {}
            if isinstance(api_data, list):
                app_logger.info(f"[{store_name}] Detailed metrics list received. Aggregating for store summary...")
                # Aggregate from MASTER type records to avoid double counting DETAIL ones
                masters = [m for m in api_data if m.get('type') == 'MASTER']
                if not masters: masters = api_data # Fallback to everything

                total_orders = sum(float(m.get('metrics', {}).get('OrdersShopped_V2', 0)) for m in masters)
                total_units  = sum(float(m.get('metrics', {}).get('RequestedQuantity_V2', 0)) for m in masters)
                total_fulfilled = sum(float(m.get('metrics', {}).get('PickedUnits_V2', 0)) for m in masters)
                
                # Weighted UPH calculation
                total_time_ms = sum(float(m.get('metrics', {}).get('TimeAvailable_V2', 0)) for m in masters)
                uph = (total_units / (total_time_ms / 3600000)) if total_time_ms > 0 else 0.0

                # Weighted Lates calculation
                total_lates_count = sum(
                    float(m.get('metrics', {}).get('OrdersShopped_V2', 0)) * (float(m.get('metrics', {}).get('UnacceptedRate_V2', 0)) / 100)
                    for m in masters
                )
                lates_rate = (total_lates_count / total_orders * 100) if total_orders > 0 else 0.0
                
                data_to_use = {
                    'OrdersShopped_V2': total_orders,
                    'RequestedQuantity_V2': total_units,
                    'PickedUnits_V2': total_fulfilled,
                    'AverageUPH_V2': uph,
                    'UnacceptedRate_V2': lates_rate,
                    'TimeAvailable_V2': total_time_ms
                }
            else:
                data_to_use = api_data

            lates_val = data_to_use.get('UnacceptedRate_V2', data_to_use.get('metrics', {}).get('UnacceptedRate_V2', 0.0))
            formatted_lates = f"{lates_val:.1f} %"
            app_logger.info(f"[{store_name}] 'Lates' extracted from API JSON: {formatted_lates}")

            # --- Compute Time Available from API ---
            milliseconds_from_api = float(data_to_use.get('TimeAvailable_V2', 0.0))
            total_seconds = int(milliseconds_from_api / 1000)
            total_minutes, _ = divmod(abs(total_seconds), 60)
            total_hours, remaining_minutes = divmod(total_minutes, 60)
            formatted_time_available = f"{total_hours}:{remaining_minutes:02d}"

            # --- Build form data and enqueue ---
            current_date = datetime.now(LOCAL_TIMEZONE).strftime('%Y-%m-%d')
            form_data = {
                'date': current_date, 'store': store_name,
                'orders': str(data_to_use.get('OrdersShopped_V2') or 0),
                'units': str(data_to_use.get('RequestedQuantity_V2') or 0),
                'fulfilled': str(data_to_use.get('PickedUnits_V2') or 0),
                'uph': f"{(data_to_use.get('AverageUPH_V2') or 0.0):.0f}",
                'inf': f"{(data_to_use.get('ItemNotFoundRate_V2') or 0.0):.1f} %",
                'found': f"{(data_to_use.get('ItemFoundRate_V2') or 0.0):.1f} %",
                'cancelled': str(data_to_use.get('ShortedUnits_V2') or data_to_use.get('OrderCancellations') or 0),
                'lates': formatted_lates,
                'time_available': formatted_time_available,
            }
            await queue.put(form_data)
            
            duration = asyncio.get_event_loop().time() - start_ts
            async with metrics_lock:
                metrics["collection_times"].append((store_name, duration))
                metrics["total_orders"] += int(data_to_use.get('OrdersShopped_V2', 0))
                metrics["total_units"] += int(data_to_use.get('RequestedQuantity_V2', 0))
            
            app_logger.info(f"[{store_name}] Data collection complete ({duration:.2f}s).")
            return  # Success, exit loop

        except Exception as e:
            app_logger.warning(f"[{store_name}] Failed attempt {attempt + 1}: {e}")
            if attempt < WORKER_RETRY_COUNT - 1:
                async with metrics_lock:
                    metrics["retries"] += 1
                    metrics["retry_stores"].add(store_name)
                sleep_time = 2 ** attempt
                app_logger.info(f"[{store_name}] Retrying {store_name} on attempt {attempt + 2}...")
                # If it failed, try navigating back to dashboard to be safe
                try:
                    await page.goto(BASE_DASHBOARD_URL, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
                except:
                    pass
                await asyncio.sleep(sleep_time)
            else:
                run_failures.append(f"{store_name} (Fail)")
                await _save_screenshot(page, f"process_fail_{store_name}")
                async with failure_lock:
                    failure_timestamps.append(asyncio.get_event_loop().time())


#######################################################################
#                  MAIN PROCESS LOOP & ORCHESTRATION
#######################################################################

async def worker_task(worker_id: int, browser: Browser, storage_template: Dict, job_queue: Queue, submission_queue: Queue):
    global active_workers_count
    app_logger.info(f"[Worker-{worker_id}] Starting up.")
    context = None
    page = None
    try:
        context = await browser.new_context(storage_state=storage_template)
        context.set_default_navigation_timeout(PAGE_TIMEOUT)
        context.set_default_timeout(ACTION_TIMEOUT)
        
        # Share one page per worker across all assigned stores
        page = await context.new_page()
        
        while True:
            try:
                store_item = job_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            
            # Enforce Concurrency Limit
            async with concurrency_condition:
                while active_workers_count >= concurrency_limit:
                    await concurrency_condition.wait()
                active_workers_count += 1

            try:
                await process_single_store(page, store_item, submission_queue)
            finally:
                async with concurrency_condition:
                    active_workers_count -= 1
                    concurrency_condition.notify_all()
                job_queue.task_done()
            
    except Exception as e:
        app_logger.error(f"[Worker-{worker_id}] Crashed: {e}")
    finally:
        if page: await page.close()
        if context: await context.close()
        app_logger.info(f"[Worker-{worker_id}] Shutting down.")

async def process_urls():
    global progress, start_time, run_failures, browser
    # Use configured concurrency or default to 10
    pool_size = config.get('initial_concurrency', 30)
    app_logger.info(f"Job 'process_urls' started with Worker Pool size: {pool_size}")
    run_failures = []
    
    load_default_data()
    if not urls_data:
        app_logger.error("No URLs to process. Aborting job.")
        return

    login_is_required = True
    if ensure_storage_state():
        app_logger.info("Existing auth state file found. Verifying session is still active...")
        temp_context = None
        try:
            # 1MMS: Use the single base dashboard URL for session checking
            test_dash_url = BASE_DASHBOARD_URL
            with open(STORAGE_STATE) as f: storage_for_check = json.load(f)
            temp_context = await browser.new_context(storage_state=storage_for_check)
            temp_page = await temp_context.new_page()
            if not await check_if_login_needed(temp_page, test_dash_url):
                app_logger.info("Session verification successful. Skipping login.")
                login_is_required = False
            else:
                app_logger.warning("Session has expired or is invalid. A new login is required.")
        except Exception as e:
            app_logger.error(f"An error occurred during session verification. Forcing re-login. Error: {e}", exc_info=DEBUG_MODE)
        finally:
            if temp_context: await temp_context.close()
    else:
        app_logger.info("No existing auth state file found. Login is required.")

    if login_is_required:
        MAX_LOGIN_ATTEMPTS = 3
        login_successful = False
        for attempt in range(MAX_LOGIN_ATTEMPTS):
            app_logger.info(f"Attempting to prime a new master session (Attempt {attempt + 1}/{MAX_LOGIN_ATTEMPTS})...")
            if await prime_master_session():
                login_successful = True
                break
            if attempt < MAX_LOGIN_ATTEMPTS - 1:
                app_logger.warning(f"Session priming failed on attempt {attempt + 1}. Retrying in 5 seconds...")
                await asyncio.sleep(5)
        
        if not login_successful:
            app_logger.critical(f"Critical: Session priming failed after {MAX_LOGIN_ATTEMPTS} attempts. Aborting job.")
            return

    with open(STORAGE_STATE) as f: storage_template = json.load(f)
    
    # Queues
    job_queue = Queue()
    submission_queue = Queue()
    
    # Populate Job Queue
    for store in urls_data:
        job_queue.put_nowait(store)
        
    with progress_lock: 
        progress = {"current": 0, "total": len(urls_data), "lastUpdate": "N/A"}
    
    start_time = datetime.now(LOCAL_TIMEZONE)

    # Start Form Submitters
    app_logger.info(f"Starting {NUM_FORM_SUBMITTERS} HTTP form submitter worker(s).")
    form_submitter_tasks = [
        asyncio.create_task(http_form_submitter_worker(submission_queue, i + 1))
        for i in range(NUM_FORM_SUBMITTERS)
    ]
    
    # Start Worker Pool
    app_logger.info(f"Spinning up {pool_size} browser workers...")
    workers = [
        asyncio.create_task(worker_task(i+1, browser, storage_template, job_queue, submission_queue))
        for i in range(pool_size)
    ]
    
    # Wait for all jobs to be processed
    await asyncio.gather(*workers)
    
    app_logger.info("All workers finished. Waiting for submission queue to empty...")
    await submission_queue.join()
    await flush_pending_chat_entries()
    
    app_logger.info("Cancelling form submitter workers...")
    for task in form_submitter_tasks: task.cancel()
    await asyncio.gather(*form_submitter_tasks, return_exceptions=True)

    elapsed = (datetime.now(LOCAL_TIMEZONE) - start_time).total_seconds()
    app_logger.info(f"Processing finished. Processed {progress['current']}/{progress['total']} in {elapsed:.2f}s")
    
    # Send Job Summary
    await post_job_summary(
        total=progress['total'],
        success=progress['current'],
        failures=run_failures,
        duration=elapsed
    )

    # 1MMS: Update urls.csv with any newly discovered merchant IDs for persistence
    update_urls_csv_with_cache()

    if run_failures:
        app_logger.warning(f"Completed with {len(run_failures)} issue(s): {', '.join(run_failures)}")
    else:
        app_logger.info("Completed successfully.")

#######################################################################
#                         MAIN EXECUTION BLOCK
#######################################################################

async def main():
    global playwright, browser
    app_logger.info("Starting up in single-run mode...")
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=not DEBUG_MODE,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-accelerated-2d-canvas",
                "--disable-gl-drawing-for-tests",
            ]
        )
        app_logger.info("Browser launched successfully.")
        await process_urls()
    except Exception as e:
        app_logger.critical(f"A critical error occurred in the main execution block: {e}", exc_info=True)
    finally:
        app_logger.info("Task finished. Initiating shutdown...")
        if browser and browser.is_connected():
            await browser.close()
            app_logger.info("Browser instance closed.")
        if playwright:
            await playwright.stop()
            app_logger.info("Playwright stopped.")
        await flush_pending_chat_entries()
        app_logger.info("Run complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        app_logger.info("Script interrupted by user. Exiting.")
