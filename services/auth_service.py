from __future__ import annotations

import re
from typing import Any

import pyotp
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, TimeoutError, expect

from core.config import Settings
from core.logger import app_logger
from core.utils import atomic_write_json, optimize_browser_context, safe_close, save_screenshot


async def check_if_login_needed(page: Page, test_url: str, settings: Settings) -> bool:
    app_logger.info(f"Verifying session status by navigating to: {test_url}")
    try:
        await page.goto(test_url, timeout=settings.page_timeout_ms, wait_until="domcontentloaded")
        login_selector = "input#ap_email, input#ap_password, input[name='email']"
        dashboard_selector = "#content > div > div.mainAppContainerExternal"

        try:
            found = await page.locator(f"{login_selector}, {dashboard_selector}").first.is_visible(timeout=10000)
            if not found:
                return True
        except TimeoutError:
            return True

        if await page.locator(login_selector).first.is_visible():
            app_logger.info("Login form detected.")
            return True

        if await page.locator(dashboard_selector).is_visible():
            app_logger.info("Dashboard detected. Session is valid.")
            return False

        return True
    except Exception as exc:
        app_logger.error(f"Error during session check: {exc}", exc_info=settings.debug_mode)
        return True


async def perform_login_and_otp(page: Page, settings: Settings) -> bool:
    app_logger.info(f"Navigating to login page: {settings.login_url}")
    try:
        await page.goto(settings.login_url, timeout=settings.page_timeout_ms, wait_until="load")
        app_logger.info("Initial page loaded. Determining login flow...")

        continue_shopping_selector = 'button:has-text("Continue shopping")'
        email_field_selector = "input#ap_email"

        await page.wait_for_selector(
            f"{continue_shopping_selector}, {email_field_selector}",
            state="visible",
            timeout=15000,
        )

        if await page.locator(continue_shopping_selector).is_visible():
            app_logger.info("Flow: Interstitial 'Continue shopping' page detected. Clicking it.")
            await page.locator(continue_shopping_selector).click()
            await expect(page.locator(email_field_selector)).to_be_visible(timeout=15000)
        else:
            app_logger.info("Flow: Login form with email field loaded directly.")

        email_locator = page.locator(email_field_selector)
        try:
            await email_locator.fill(settings.login_email, timeout=10000)
        except Exception:
            app_logger.warning("Direct selector for email failed. Falling back to label-based selector.")
            fallback_email_locator = page.get_by_label("Email or mobile phone number")
            await expect(fallback_email_locator).to_be_visible(timeout=10000)
            await fallback_email_locator.fill(settings.login_email)

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
                        f"Encountered error while handling alternate sign-in option: {inner_error}",
                        exc_info=settings.debug_mode,
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

        await password_field.fill(settings.login_password)
        await page.get_by_label("Sign in").click()

        otp_selector = 'input[id*="otp"]'
        dashboard_selector = "#content > div > div.mainAppContainerExternal"
        await page.wait_for_selector(f"{otp_selector}, {dashboard_selector}", timeout=30000)

        otp_field = page.locator(otp_selector)
        if await otp_field.is_visible():
            app_logger.info("Two-Step Verification (OTP) is required.")
            otp_code = pyotp.TOTP(settings.otp_secret_key).now()
            await otp_field.fill(otp_code)
            if await page.locator("input[type='checkbox'][name='rememberDevice']").is_visible():
                await page.locator("input[type='checkbox'][name='rememberDevice']").check()
            await page.get_by_role("button", name="Sign in").click()

        account_picker_selector = 'h1:has-text("Select an account")'
        await page.wait_for_selector(f"{dashboard_selector}, {account_picker_selector}", timeout=30000)
        account_picker = page.locator(account_picker_selector)
        if await account_picker.is_visible():
            app_logger.info("Account picker detected. Selecting 1MMS User Store...")
            try:
                await page.get_by_role("button", name="1MMS User Store").click(timeout=10000)
                await page.get_by_role("button", name="United Kingdom").click(timeout=10000)
                await page.get_by_role("button", name="Select account").click(timeout=10000)
                await page.wait_for_selector(dashboard_selector, timeout=30000)
            except Exception as picker_error:
                app_logger.warning(f"Account picker interaction issue: {picker_error}")
                await save_screenshot(page, "account_picker_issue", settings)

        app_logger.info("Login process appears fully successful.")
        return True
    except Exception as exc:
        app_logger.critical(f"Critical error during login process: {exc}", exc_info=settings.debug_mode)
        await save_screenshot(page, "login_critical_failure", settings)
        return False


async def prime_master_session(browser, settings: Settings) -> bool:
    app_logger.info("Priming master session")
    context = None
    try:
        if not browser or not browser.is_connected():
            return False

        context = await browser.new_context()
        await optimize_browser_context(context, settings)
        context.set_default_navigation_timeout(settings.page_timeout_ms)
        context.set_default_timeout(settings.action_timeout_ms)
        page = await context.new_page()
        if not await perform_login_and_otp(page, settings):
            return False

        storage = await context.storage_state()
        atomic_write_json(settings.storage_state_path, storage, indent=2)
        app_logger.info(f"Login successful. Auth state saved to '{settings.storage_state_path}'.")
        return True
    except Exception as exc:
        app_logger.exception(f"Priming failed with an unexpected error: {exc}")
        return False
    finally:
        await safe_close(context, "Master session context")
