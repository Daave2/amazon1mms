import json
import re
from typing import Any

import pyotp
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, TimeoutError, expect

from core.config import (
    ACTION_TIMEOUT,
    DEBUG_MODE,
    LOGIN_EMAIL,
    LOGIN_PASSWORD,
    LOGIN_URL,
    OTP_SECRET_KEY,
    PAGE_TIMEOUT,
    STORAGE_STATE,
)
from core.logger import app_logger
from core.utils import safe_close, save_screenshot


async def check_if_login_needed(page: Page, test_url: str) -> bool:
    app_logger.info(f"Verifying session status by navigating to: {test_url}")
    try:
        await page.goto(test_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        login_selector = "input#ap_email, input#ap_password, input[name='email']"
        dashboard_selector = "#content > div > div.mainAppContainerExternal"

        try:
            found = await page.locator(f"{login_selector}, {dashboard_selector}").first.is_visible(timeout=10000)
            if not found:
                if "signin" in page.url.lower() or "/ap/" in page.url:
                    return True
                return True
        except TimeoutError:
            if "signin" in page.url.lower() or "/ap/" in page.url:
                return True
            return True

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
        email_field_selector = "input#ap_email"

        await page.wait_for_selector(
            f"{continue_shopping_selector}, {email_field_selector}", state="visible", timeout=15000
        )

        if await page.locator(continue_shopping_selector).is_visible():
            app_logger.info("Flow: Interstitial 'Continue shopping' page detected. Clicking it.")
            await page.locator(continue_shopping_selector).click()
            await expect(page.locator(email_field_selector)).to_be_visible(timeout=15000)
        else:
            app_logger.info("Flow: Login form with email field loaded directly.")

        email_locator = page.locator(email_field_selector)
        try:
            await email_locator.fill(LOGIN_EMAIL, timeout=10000)
        except Exception:
            app_logger.warning("Direct selector for email failed. Falling back to label-based selector.")
            fallback_email_locator = page.get_by_label("Email or mobile phone number")
            await expect(fallback_email_locator).to_be_visible(timeout=10000)
            await fallback_email_locator.fill(LOGIN_EMAIL)

        continue_locator = page.get_by_label("Continue")
        try:
            await continue_locator.click()
        except TimeoutError:
            app_logger.warning("Continue control not available via label. Using fallback selector.")
            fallback_continue = page.get_by_role("button", name=re.compile("continue", re.I))
            if await fallback_continue.count() == 0:
                fallback_continue = page.locator("input#continue, button#continue, input[name='continue']")
            await expect(fallback_continue.first).to_be_visible(timeout=10000)
            await fallback_continue.first.click()

        password_field = page.get_by_label("Password")
        try:
            await expect(password_field).to_be_visible(timeout=10000)
        except TimeoutError:
            app_logger.warning("Password field not visible after entering email. Attempting to bypass passkey flow.")

            async def _click_if_visible(locator: Any) -> bool:
                try:
                    if locator and await locator.count() > 0:
                        visible_locator = locator.first
                        if await visible_locator.is_visible():
                            await visible_locator.click()
                            return True
                except PlaywrightError as inner_error:
                    app_logger.debug(
                        f"Encountered error while handling alternate sign-in option: {inner_error}", exc_info=DEBUG_MODE
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
                app_logger.warning("No passkey bypass option detected. Proceeding without additional interaction.")

            await expect(password_field).to_be_visible(timeout=10000)
        await password_field.fill(LOGIN_PASSWORD)
        await page.get_by_label("Sign in").click()

        otp_selector = 'input[id*="otp"]'
        dashboard_selector = "#content > div > div.mainAppContainerExternal"
        await page.wait_for_selector(f"{otp_selector}, {dashboard_selector}", timeout=30000)

        otp_field = page.locator(otp_selector)
        if await otp_field.is_visible():
            app_logger.info("Two-Step Verification (OTP) is required.")
            otp_code = pyotp.TOTP(OTP_SECRET_KEY).now()
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
                await save_screenshot(page, "account_picker_issue")

        app_logger.info("Login process appears fully successful.")
        return True
    except Exception as e:
        app_logger.critical(f"Critical error during login process: {e}", exc_info=DEBUG_MODE)
        await save_screenshot(page, "login_critical_failure")
        return False


async def prime_master_session(browser) -> bool:
    app_logger.info("Priming master session")
    ctx = None
    try:
        if not browser or not browser.is_connected():
            return False
        ctx = await browser.new_context()
        ctx.set_default_navigation_timeout(PAGE_TIMEOUT)
        ctx.set_default_timeout(ACTION_TIMEOUT)
        await ctx.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ("image", "stylesheet", "font", "media")
                else route.continue_()
            ),
        )
        page = await ctx.new_page()
        if not await perform_login_and_otp(page):
            return False
        storage = await ctx.storage_state()
        with open(STORAGE_STATE, "w") as f:
            json.dump(storage, f)
        app_logger.info(f"Login successful. Auth state saved to '{STORAGE_STATE}'.")
        return True
    except Exception as e:
        app_logger.exception(f"Priming failed with an unexpected error: {e}")
        return False
    finally:
        await safe_close(ctx, "Master session context")
